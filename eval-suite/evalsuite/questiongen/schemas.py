"""Field enums and row normalization for generated questions.

Generated rows EXTEND the eval row schema with optional fields:
``language`` / ``tone`` / ``layer`` (legacy L1-L4) / ``level`` (v2 C1-C5
capability taxonomy) / ``intent`` / ``family``.  Legacy rows (the frozen
sets) carry none of them; lint treats them as optional so old files keep
passing.

v2 capability taxonomy (user ruling 2026-10-15) — ``level``:

C1  domain commonsense answerable by an expert WITHOUT any document; the
    evidence-bound system is expected to say the corpus lacks it (honest
    no-evidence), never to hallucinate — calibrates the honesty boundary
C2  measured value / number / date extraction from text or table rows
C3  detail comprehension requiring PARAPHRASE (explain / convert /
    plain-language), never verbatim copy
C4  multi-hop: chain ≥2 blocks/documents, infer causes or predict outcomes
C5  safety/ethics risk judgment (bypass attempts, proxy submission, PII
    lookup, academic integrity) — expected: policy-grounded refusal/answer
"""

from __future__ import annotations

import re
from typing import Any

LANGUAGES = ("zh", "en", "mixed")
TONES = ("formal", "colloquial", "keyword", "polite")
LAYERS = ("L1", "L2", "L3", "L4")
LEVELS = ("C1", "C2", "C3", "C4", "C5")
INTENTS = ("deadline", "procedure", "eligibility", "quantity", "contact", "comparison", "boundary", "explanation", "consequence", "risk")

#: Question SHAPE metadata (2026-10-16) — NOT a matrix dimension.  Section
#  questions (list / aggregate / point-control over a table section) live in
#  the quantity intent cell per frozen-set precedent (sm-0017/18/19);
#  qshape records which shape a row is so tooling (lint, review) can treat
#  them differently without widening the five-dimension matrix.
QSHAPES = ("enumeration", "aggregation", "point")

#: canonical eval categories (evalsuite/lint.py) a generated layer may map to.
#: layer = answer complexity, category = evidence nature — version/conflict
#: categories are orthogonal to complexity and allowed at every answerable layer.
LAYER_CATEGORIES = {
    "L1": ("fact_sharepoint_table", "fact_zjui_table", "fact_general", "fact_ugrs_list", "fact_grs_doc", "mixed_en_zh_course", "version_latest", "conflict_flag"),
    "L2": ("fact_sharepoint_table", "fact_zjui_table", "fact_general", "mixed_en_zh_course", "version_latest", "conflict_flag"),
    "L3": ("version_latest", "conflict_flag", "fact_general", "fact_sharepoint_table", "fact_zjui_table", "mixed_en_zh_course"),
    "L4": ("rule_trap_future_term", "rule_trap_audience"),
}
TRAP_BEHAVIORS = {"rule_trap_future_term": "refuse_missing_term", "rule_trap_audience": "refuse_audience_mismatch"}

#: v2 level → (allowed categories, expected_behavior when fixed)
LEVEL_CATEGORIES = {
    "C1": ("domain_commonsense",),
    "C2": ("fact_sharepoint_table", "fact_zjui_table", "fact_general", "fact_ugrs_list", "fact_grs_doc", "mixed_en_zh_course", "version_latest", "conflict_flag"),
    "C3": ("fact_sharepoint_table", "fact_zjui_table", "fact_general", "fact_ugrs_list", "fact_grs_doc", "mixed_en_zh_course"),
    "C4": ("fact_general", "version_latest", "conflict_flag", "mixed_en_zh_course", "fact_sharepoint_table", "fact_zjui_table"),
    "C5": ("safety_risk",),
}
LEVEL_BEHAVIORS = {
    "C1": "no_evidence_expected",      # honest "证据中未包含" is the correct system behavior
    "C5": "safety_refusal_or_policy",  # refuse the bypass / answer strictly by policy rules
}


_DATE_STRIP_RE = __import__("re").compile(
    r"(?:19|20)\d{2}[-/年.]\s*\d{1,2}[-/月.]\s*\d{1,2}日?"
    r"|(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s*\d{1,2}(?:st|nd|rd|th)?(?:,?\s*(?:19|20)\d{2})?"
    r"|\d{1,2}\s*月\s*\d{1,2}\s*日|\d{1,2}[:.]\d{2}|\d{1,2}\s?[ap]\.?m\.?",
    __import__("re").I,
)


def _nondigit_safe(text: str) -> set[str]:
    """Digits outside date/time expressions (date word-vs-digit forms must not
    count as fabrication; see backfill_golden)."""
    import re as _re

    return set(_re.findall(r"\d+(?:\.\d+)?", _DATE_STRIP_RE.sub(" ", str(text))))


def leaks_answer(question: str, golds: list[dict[str, Any]]) -> bool:
    """A question leaks when an answer-carrying value from the gold quote
    appears verbatim in it.  Course-code digits are NOT the answer (naming
    the course is required specificity), so a number directly preceded by
    letters ("CEE 495") is exempt; years are exempt (they frame the term).
    """
    for anchor in golds:
        quote = str(anchor.get("quote", ""))
        for match in re.finditer(r"\d+(?:\.\d+)?", quote):
            token = match.group(0)
            if len(token) < 2 or re.fullmatch(r"(?:19|20)\d{2}", token):
                continue
            for hit in re.finditer(re.escape(token), question):
                before = question[max(0, hit.start() - 4):hit.start()]
                if re.search(r"[A-Za-z]\s*$", before):
                    continue  # part of a course-code mention, e.g. "CEE 495"
                return True
    return False


def normalize_row(raw: dict[str, Any], *, qid: str, packet_kind: str, source_doc: str,
                  default_today: str = "") -> dict[str, Any] | None:
    """Normalize one LLM-drafted question object into an eval row; None on shape failure.

    The generator MUST return: question, quote (the verbatim source block
    text or "" for traps), doc_uid, and the matrix fields.  gold anchors are
    assembled here from (doc_uid, quote, block_id) — never trusted from the
    model beyond the fields the packet itself supplied.
    """
    question = str(raw.get("question") or "").strip()
    if not question or len(question) < 4:
        return None
    language = str(raw.get("language") or "zh").strip()
    tone = str(raw.get("tone") or "formal").strip()
    layer = str(raw.get("layer") or "L1").strip().upper()
    level = str(raw.get("level") or "").strip().upper()
    intent = str(raw.get("intent") or "fact").strip()
    if language not in LANGUAGES:
        language = "zh" if language not in LANGUAGES else language
    if tone not in TONES:
        tone = "formal"
    if layer not in LAYERS:
        layer = "L1"
    if level not in LEVELS:
        level = ""  # legacy rows have no capability level
    if intent not in INTENTS:
        intent = "boundary" if intent == "policy" else "quantity" if intent == "amount" else "deadline"

    row: dict[str, Any] = {
        "id": qid,
        "question": question,
        "category": str(raw.get("category") or "").strip(),
        "language": language,
        "tone": tone,
        "layer": layer,
        "intent": intent,
        "source_doc": source_doc,
        "packet_kind": packet_kind,
        "verified": False,
        "status": "candidate",
        "notes": f"generated {raw.get('reason', '')}".strip(),
    }
    if level:
        row["level"] = level
    quote = str(raw.get("quote") or "").strip()
    doc_uid = str(raw.get("doc_uid") or "").strip()
    golden = str(raw.get("golden_answer") or "").strip()
    # user ruling 2026-10-15: no machine guard on golden answers — the prompts
    # carry the constraints (strictly-from-quote, verbatim digits); quality is
    # enforced by the review/judge layers, not hard value checks here
    if golden:
        row["golden_answer"] = golden
    block_id = str(raw.get("block_id") or "").strip()
    ordinal = raw.get("ordinal")
    if level == "C1":
        # domain commonsense: no document anchor; the golden answer IS the
        # expert reference; the evidence-bound system is expected to report
        # the corpus lacks it — calibrates the honesty boundary
        if not golden:
            return None
        row["category"] = "domain_commonsense"
        row["gold"] = []
        row["expected_behavior"] = "no_evidence_expected"
        return row
    if level == "C5":
        # safety/ethics: policy basis optional; expected behavior is a
        # policy-grounded refusal or strictly rule-based answer
        row["category"] = "safety_risk"
        row["expected_behavior"] = "safety_refusal_or_policy"
        if quote and doc_uid:
            row["gold"] = [{"doc_uid": doc_uid, "quote": quote, "block_id": block_id, **({"ordinal": ordinal} if ordinal else {})}]
        else:
            row["gold"] = []
            row["trap_gold"] = []
        return row
    if layer == "L4":
        if quote and doc_uid:
            category = row["category"]
            if category not in TRAP_BEHAVIORS:
                category = "rule_trap_future_term"
            # base lint: an audience trap must state the asker's identity;
            # coerce to a future-term trap when the model forgot
            identity = re.search(r"本科|研究生|硕士|博士|undergrad|graduate|phd|master|我是", question, re.I)
            if category == "rule_trap_audience" and not identity:
                category = "rule_trap_future_term"
            row["category"] = category
            row["gold"] = []
            row["trap_gold"] = [{"doc_uid": doc_uid, "quote": quote, "block_id": block_id, **({"ordinal": ordinal} if ordinal else {})}]
            row["expected_behavior"] = TRAP_BEHAVIORS[category]
            row["today"] = row.get("today") or default_today
            if not row.get("today"):
                return None  # base lint requires today on traps
        else:
            return None
    else:
        if not quote or not doc_uid:
            return None
        row["gold"] = [{"doc_uid": doc_uid, "quote": quote, "block_id": block_id, **({"ordinal": ordinal} if ordinal else {})}]
        extra_doc, extra_quote = str(raw.get("doc_uid_2") or "").strip(), str(raw.get("quote_2") or "").strip()
        if extra_doc and extra_quote:
            row["gold"].append({"doc_uid": extra_doc, "quote": extra_quote})
    today = str(raw.get("today") or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", today):
        row["today"] = today
    if raw.get("family"):
        row["family"] = str(raw["family"])
    qshape = str(raw.get("qshape") or "").strip()
    if qshape in QSHAPES:
        row["qshape"] = qshape
    return row
