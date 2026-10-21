"""Answer orchestration (P1): structured generation, validation, fallback.

Pipeline: evidence refs from the pack → provider (API if enabled and the
breaker allows, else template) → map E-number citations onto real evidence
ids → claim/citation validation → publish only validated answers; anything
else falls back to the deterministic template with an explicit generation
mode so logs and the UI can tell exactly what happened.
"""

from __future__ import annotations

import re
from typing import Any

from product.claim_validator import extract_facts, validate_answer
from product.config import RuntimeConfig, llm_enabled
from product.contracts import AnswerStatus
from product.providers import ApiProvider, Breaker, EvidenceRef, TemplateProvider

ANSWER_SCHEMA_VERSION = 2


def compute_status(*, decision_status: str, abstain: bool, disposition: str, mode: str, validated: bool, has_claims: bool = True, requirements_missing: bool = False) -> AnswerStatus:
    """The ONE public answer status from the generation bundle's own fields.

    Callers (API, UI, judge) read this instead of recombining
    decision/generation booleans; those stay visible as diagnostics.
    ``partial`` fires when the provider itself scoped the answer (its
    ``answer_disposition`` is partial_answer, or it refused part of the
    question while still publishing claims for the rest) or when the pack's
    atomic requirements are not all covered by evidence.
    """
    if disposition == "policy_refusal" or mode == "policy_refusal":
        return "refused_policy"
    if abstain or mode == "refusal":
        return "refused_evidence"
    if disposition == "evidence_refusal" and not has_claims:
        return "refused_evidence"  # pure refusal: nothing was asserted
    if not validated:
        return "unverified"
    if decision_status == "conflict":
        return "conflict"
    if disposition in ("partial_answer", "evidence_refusal"):
        return "partial"
    if requirements_missing:
        return "partial"
    return "answered"


def _missing_requirements(payload: Any) -> list[str]:
    """Requirement ids the evidence pack could not cover (step 8).

    The runtime derives one requirement per demanded fact slot; a missing one
    means the corpus has no evidence for that part of the question — the
    answer cannot honestly be labeled fully answered."""
    return [str(req.get("requirement_id", "")) for req in (payload.evidence_pack.requirements or [])
            if req.get("status") == "missing"]

#: Inline citation markers in schema-v2 answers: [E1], [e 2], [3] — bracketed
#: so course codes like ECE374 can never false-match.
_INLINE_CITE_RE = re.compile(r"\[\s*[Ee]?\s*(\d{1,2})\s*\]")

#: Sentence boundaries for claim extraction: CJK terminal punctuation, an
#: English period followed by whitespace + sentence start (Latin or CJK — the
#: model freely mixes languages mid-answer), or newlines.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?；;])\s*|(?<=\.)\s+(?=[A-Z0-9\u4e00-\u9fff])|\n+")

#: Honest-miss phrasings the prompt red line pins down (zjui_academic.yaml
#: prompt.persona): the generator must SAY the evidence lacks the fact instead
#: of fabricating.  A sentence matching this AND carrying no atomic facts is a
#: no-evidence STATEMENT, not a claim — the 2026-10-17 reviewer case had such
#: sentences extracted as claims, vacuously validated (nothing to check) and
#: the answer badged 有据可依 · 引用已核对 while its text said the opposite.
_NO_EVIDENCE_RE = re.compile(
    r"证据[中里]?未(包含|提及|提到|提供|覆盖)|未在证据[中里](找到|包含|提及)|证据[中里]没有"
    r"|未(提及|提到)任何|没有(提及|提到|找到)|未提供"
    r"|not\s+(?:be\s+)?(?:found|mentioned|contained|included|provided|covered)\s+in\s+(?:the\s+)?(?:evidence|documents|materials|retrieved)"
    r"|(?:evidence|documents|materials)\s+(?:do|does)\s+not\s+(?:contain|mention|include|provide|cover)"
    r"|not\s+mention(?:ed)?\s+(?:any|in)\b|no\s+information\s+(?:on|about)\b",
    re.IGNORECASE,
)


def _is_no_evidence_statement(text: str) -> bool:
    """True when a sentence only SAYS the evidence lacks the fact.

    A sentence that also asserts a date/number/course code is a mixed
    statement (「证据中未包含学费；报名费为 200 元 [E1]」) — its factual half
    is a real claim and must survive the filter."""
    if not _NO_EVIDENCE_RE.search(text):
        return False
    facts = extract_facts(text)
    return not (facts["dates"] or facts["times"] or facts["numbers"] or facts["course_codes"])

REFUSALS = {
    "no_material": "当前知识库中没有覆盖该问题的充分证据，暂不作出确定回答。",
    "wrong_term": "所问的学期/学年暂无适用证据（可能尚未发布或不在本知识库覆盖范围内），暂不作出确定回答。",
    "wrong_audience": "现有证据面向其他适用对象，无法回答你的问题。",
    "stale_or_uncertain": "证据可能已过时或存在不确定性，需要以更新来源确认，暂不作出确定回答。",
    "sufficient": "",  # unreachable: sufficient never abstains
    "conflict": "",  # answered — but both sides must be presented
}
REFUSALS_EN = {
    "no_material": "The knowledge base has no sufficient evidence covering this question, so no definitive answer is given.",
    "wrong_term": "No applicable evidence exists for the term / academic year asked about (it may not be published yet or is outside this knowledge base), so no definitive answer is given.",
    "wrong_audience": "The available evidence applies to a different audience and cannot answer your question.",
    "stale_or_uncertain": "The evidence may be outdated or uncertain and needs confirmation from a newer source, so no definitive answer is given.",
    "sufficient": "",
    "conflict": "",
}
NO_USABLE_EVIDENCE = {
    "zh": "当前检索结果中没有可用于支持确定回答的证据，暂不作出确定回答。",
    "en": "The retrieved results contain no evidence that can support a definitive answer, so none is given.",
}

# Process-wide provider state (the breaker survives across requests).
_BREAKER = Breaker()


def breaker_state() -> dict[str, Any]:
    return _BREAKER.snapshot()


def _evidence_refs(payload: Any, *, table_context: bool = True) -> list[EvidenceRef]:
    usable_ids = set(payload.evidence_pack.usable_evidence_ids or [])
    items = [item for item in payload.evidence_pack.evidence if item.evidence_id in usable_ids and not item.blocking]
    return [
        EvidenceRef(index=index, evidence_id=item.evidence_id, text=item.text, title=item.title or "",
                    source_url=item.provenance.source_url or "", doc_id=item.doc_id or "",
                    context=_evidence_context(item) if table_context else "")
        for index, item in enumerate(items, 1)
    ]


def _evidence_context(item: Any) -> str:
    """One provenance line for table rows: 位置/表题/列头 (2026-10-16).

    A bare "MENG Zhang/TA1 E-205 (20)" is unreadable; with the column header
    and the table title it becomes "Junior schedule, Wednesday, capacity 20".
    Empty string for plain paragraphs keeps the prompt unchanged.
    """
    parts: list[str] = []
    heading = str(getattr(item, "heading", "") or "").strip()
    if heading:
        parts.append(f"位置:{heading}")
    table = getattr(item, "table_context", None)
    if isinstance(table, dict):
        table_title = str(table.get("table_title") or "").strip()
        if table_title and table_title != heading:
            parts.append(f"表:{table_title}")
        header = " | ".join(str(cell) for cell in (table.get("header") or []) if str(cell).strip())
        if header:
            parts.append(f"列头:{header[:140]}")
    return f"〔{'；'.join(parts)}〕\n" if parts else ""


def _id_map(refs: list[EvidenceRef]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for ref in refs:
        mapping[ref.evidence_id] = ref.evidence_id
        mapping[f"E{ref.index}"] = ref.evidence_id
        mapping[f"e{ref.index}"] = ref.evidence_id
        mapping[str(ref.index)] = ref.evidence_id
        mapping[f"[{ref.index}]"] = ref.evidence_id
    return mapping


def _map_claim_ids(claims: list[dict[str, Any]], mapping: dict[str, str]) -> list[dict[str, Any]]:
    mapped = []
    for claim in claims:
        ids = []
        for raw in claim.get("evidence_ids") or []:
            key = str(raw).strip()
            ids.append(mapping.get(key, key))  # unknown ids survive -> validator flags them
        mapped.append({"text": claim.get("text", ""), "evidence_ids": ids})
    return mapped


def _extract_inline_claims(answer: str, mapping: dict[str, str]) -> list[dict[str, Any]]:
    """Schema v2: the model cites inline with bracketed markers; claims are the
    sentences that carry at least one marker.

    Markers are stripped from the claim text (the digits inside would pollute
    number validation), and unmapped numbers survive verbatim so the validator
    flags them — same contract as provider-supplied evidence_ids.  Sentences
    without markers are dropped: they are connective prose, not checkable
    facts.
    """
    claims: list[dict[str, Any]] = []
    for sentence in _SENTENCE_SPLIT_RE.split(str(answer)):
        sentence = sentence.strip()
        if not sentence:
            continue
        markers = _INLINE_CITE_RE.findall(sentence)
        if not markers:
            continue
        ids = list(dict.fromkeys(mapping.get(f"E{int(n)}", f"E{n}") for n in markers))
        text = _INLINE_CITE_RE.sub("", sentence).strip(" \t。，．,；;.")
        if _is_no_evidence_statement(text):
            continue  # says what's absent, asserts nothing — not a claim
        claims.append({"text": text or sentence, "evidence_ids": ids})
    return claims


def _citations(claims: list[dict[str, Any]], refs: list[EvidenceRef]) -> list[dict[str, str]]:
    by_id = {ref.evidence_id: ref for ref in refs}
    seen: list[str] = []
    for claim in claims:
        for evidence_id in claim.get("evidence_ids") or []:
            if evidence_id in by_id and evidence_id not in seen:
                seen.append(evidence_id)
    return [
        {"id": by_id[eid].evidence_id, "title": by_id[eid].title, "source_url": by_id[eid].source_url, "doc_uid": by_id[eid].doc_id, "block_id": ""}
        for eid in seen
    ]


def _render_claim(claim: dict[str, Any], refs: list[EvidenceRef]) -> dict[str, Any]:
    by_id = {ref.evidence_id: ref for ref in refs}
    sources = [
        {"id": by_id[eid].evidence_id, "title": by_id[eid].title, "source_url": by_id[eid].source_url, "doc_uid": by_id[eid].doc_id}
        for eid in claim.get("evidence_ids") or [] if eid in by_id
    ]
    return {"text": claim["text"], "evidence_ids": claim.get("evidence_ids") or [], "sources": sources}


async def generate_answer(payload: Any, config: RuntimeConfig, *, request_id: str = "", api_provider: Any | None = None, on_delta: Any = None, language: str = "") -> dict[str, Any]:
    """Return the publishable answer bundle for one contract-validated payload."""
    decision = payload.decision
    if decision.abstain:
        disposition = "evidence_refusal"
        return {
            "answer": (REFUSALS_EN if language == "en" else REFUSALS).get(decision.status, (REFUSALS_EN if language == "en" else REFUSALS)["no_material"]),
            "provider": "evidence-template",
            "citations": [],
            "claims": [],
            "follow_ups": [],
            "status": compute_status(decision_status=decision.status, abstain=True, disposition=disposition, mode="refusal", validated=True),
            "generation": {"mode": "refusal", "validated": True, "problems": []},
            "answer_disposition": disposition,
            "pack_hash": payload.pack_hash,
            "citation_map": {},
        }

    refs = _evidence_refs(payload, table_context=getattr(config, "table_context", True))
    if not refs:
        # A non-abstaining decision with no usable evidence is a runtime
        # contract inconsistency. Do not expose a blocking or unrelated item
        # to the provider as if it supported the answer.
        disposition = "evidence_refusal"
        return {
            "answer": NO_USABLE_EVIDENCE["en" if language == "en" else "zh"],
            "provider": "evidence-template",
            "citations": [],
            "claims": [],
            "follow_ups": [],
            "status": compute_status(decision_status=decision.status, abstain=False, disposition=disposition, mode="refusal", validated=True),
            "generation": {
                "mode": "refusal",
                "validated": True,
                "problems": ["no_usable_evidence_for_non_abstaining_decision"],
                "schema_version": ANSWER_SCHEMA_VERSION,
            },
            "answer_disposition": disposition,
            "pack_hash": payload.pack_hash,
            "citation_map": {},
        }
    mapping = _id_map(refs)
    fact_conflicts = list(payload.evidence_pack.fact_conflicts or [])
    conflict = decision.status == "conflict" or bool(fact_conflicts)

    provider = api_provider
    problems: list[str] = []
    mode = "template"
    if provider is None and llm_enabled(config):
        provider = ApiProvider(config, breaker=_BREAKER)

    raw: dict[str, Any] | None = None
    if provider is not None:
        try:
            raw = await provider.complete_structured(
                payload.question, refs, conflict=conflict, fact_conflicts=fact_conflicts, request_id=request_id, on_delta=on_delta,
                # only when set: injected test providers predate the parameter
                **({"language": language} if language else {}),
            )
        except Exception as error:  # a provider bug must never break the answer path
            problems = [f"provider_error: {type(error).__name__}"]
            raw = None

    if raw is not None:
        claims = _map_claim_ids(raw.get("claims") or [], mapping)
        if not claims:  # schema v2: citations live inline in the answer text
            claims = _extract_inline_claims(str(raw.get("answer", "")), mapping)
        claims = [claim for claim in claims if not _is_no_evidence_statement(str(claim.get("text", "")))]
        refusal = bool(raw.get("refusal"))
        # Text-level honest-miss guard (2026-10-17): the red line tells the
        # model to write 证据中未包含… instead of fabricating, but nothing
        # reconciled that phrasing with the structured flags — when the whole
        # answer text says "not in the evidence" it must label as an honest
        # refusal even if the model left refusal=false or pasted markers onto
        # the miss sentence.
        no_evidence_text = bool(_NO_EVIDENCE_RE.search(str(raw.get("answer", ""))))
        nothing_asserted = not claims and (refusal or no_evidence_text)
        if nothing_asserted:
            # Nothing was asserted, so there is nothing to validate —
            # publishing it as "unverified" would mislabel an honest
            # no-answer.
            verdict = {"ok": True, "problems": []}
        else:
            verdict = validate_answer(claims, payload.evidence_pack, question=payload.question)
        # Non-blocking validation (user ruling 2026-10-15): the model's answer
        # ALWAYS publishes; the verdict only labels trust.  The prompt carries
        # the red line ("say 证据中未包含该信息 instead of fabricating"), and
        # problems stay visible here + in the UI for audit.
        mode = "refusal" if nothing_asserted else ("validated" if verdict["ok"] else "unverified")
        disposition = raw.get("answer_disposition") or ("evidence_refusal" if (refusal or no_evidence_text) else "answer")
        if no_evidence_text and claims and disposition == "answer":
            disposition = "partial_answer"  # answered part + admitted gap
        missing_reqs = _missing_requirements(payload) if verdict["ok"] else []
        return {
            "answer": raw.get("answer", ""),
            "status": compute_status(
                # Under a text-level miss with surviving adjacent claims, the
                # decision layer's conflict flag is about NEIGHBOUR facts, not
                # the asked one — reporting 证据存在分歧 for a fact the answer
                # says is absent would be incoherent (reg-ne-04).
                # check_consistency.derived_status mirrors this rule.
                decision_status="sufficient" if no_evidence_text and claims else decision.status,
                abstain=False, disposition=disposition, mode=mode,
                validated=bool(verdict["ok"]), has_claims=bool(claims), requirements_missing=bool(missing_reqs)),
            "missing_requirements": missing_reqs,
            "answer_disposition": disposition,
            "pack_hash": payload.pack_hash,
            "citation_map": {f"E{ref.index}": ref.evidence_id for ref in refs},
            "provider": getattr(provider, "model", None) or provider.name,
            "citations": _citations(claims, refs),
            "claims": [_render_claim(claim, refs) for claim in claims],
            "follow_ups": list(raw.get("follow_ups") or []),
            "generation": {
                "mode": mode,
                "validated": verdict["ok"],
                "problems": verdict["problems"],
                "schema_version": ANSWER_SCHEMA_VERSION,
            },
        }

    # Fallback: deterministic template (provider failed / breaker open / no LLM).
    if not _BREAKER.allow():
        mode = "breaker_open"
    elif provider is not None and raw is None:
        mode = "provider_fallback"
    else:
        mode = "template"
    template = TemplateProvider()
    t_raw = await template.complete_structured(payload.question, refs, conflict=conflict, fact_conflicts=fact_conflicts, on_delta=on_delta, language=language)
    t_claims = _map_claim_ids(t_raw["claims"], mapping)
    verdict = validate_answer(t_claims, payload.evidence_pack, question=payload.question)
    # The template copies evidence verbatim, so this must hold; if it ever
    # fails we still publish it (it cannot hallucinate) but mark it.
    missing_reqs = _missing_requirements(payload) if verdict["ok"] else []
    return {
        "answer": t_raw["answer"],
        "status": compute_status(decision_status=decision.status, abstain=False, disposition="template", mode=mode,
                                validated=bool(verdict["ok"]), has_claims=bool(t_claims), requirements_missing=bool(missing_reqs)),
        "missing_requirements": missing_reqs,
        "answer_disposition": "template",
        "pack_hash": payload.pack_hash,
        "citation_map": {f"E{ref.index}": ref.evidence_id for ref in refs},
        "provider": template.name,
        "citations": _citations(t_claims, refs),
        "claims": [_render_claim(claim, refs) for claim in t_claims],
        "follow_ups": [],
        "generation": {"mode": mode, "validated": verdict["ok"], "problems": problems, "schema_version": ANSWER_SCHEMA_VERSION},
    }
