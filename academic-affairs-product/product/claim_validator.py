"""Answer-level claim/citation validation (P1).

Every claim the product publishes must (a) cite at least one evidence item
that exists in the pack, and (b) keep every atomic fact — dates, numbers with
units, bare years, course codes — inside the union of its cited evidence
texts.  A conflict pack additionally requires both parties to be cited.  A
failing answer is never published as-is: the caller falls back to the
deterministic template.

The extractors here are deliberately compact and answer-layer-specific; the
corpus side keeps its own (richer) fact-slot machinery — this module must
stay independent of the builder.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from product.domain_profile import load_profile

ISO_DATE_RE = load_profile().pattern("claim_validation", "iso_date")
ZH_DATE_RE = load_profile().pattern("claim_validation", "zh_date")
#: "11月6日" without a year — degrades to MM-DD like the English form
ZH_DATE_NOYEAR_RE = load_profile().pattern("claim_validation", "zh_date_noyear")
#: An English date may carry a time-of-day between day and year
#: ("Sept 18 5pm 2026") — capture the whole span so the year attaches.
EN_DATE_RE = load_profile().pattern("claim_validation", "en_date")
#: Clock times are their own fact kind and never leak into numbers
#: ("17:00" must not produce 17@ / 00@).
TIME_RE = load_profile().pattern("claim_validation", "time")
YEAR_RE = load_profile().pattern("claim_validation", "year")
COURSE_CODE_RE = load_profile().pattern("claim_validation", "course_code")

# Numeric facts are keyed as "<value>@<unit-class>" so a unit translation is
# not treated as a fabrication: 8周 == 8 weeks == 8th week == 第8周 (user
# ruling 2026-10-14).  Different unit classes (8周 vs 8天) stay incompatible.
# Unit vocabulary (incl. AU≈credit aliasing) lives in the domain profile;
# values are normalized to tuples for immutability.
_UNIT_WORDS: dict[str, tuple[str, ...]] = {
    cls: tuple(words)
    for cls, words in load_profile().mapping("claim_validation", "unit_words").items()
}
_ALL_UNIT_WORDS = sorted((w for words in _UNIT_WORDS.values() for w in words), key=len, reverse=True)
_UNIT_ALT = "|".join(re.escape(w) + ("(?![A-Za-z0-9])" if w.isascii() and not w[0].isdigit() else "") for w in _ALL_UNIT_WORDS)
# A Chinese measure word may sit between the number and the unit ("128个学分",
# "3门课程学分"), and trailing ".0" must not split equal values ("4.0 credit"
# vs "4 学分" — seen on live 2026-10-15 answers).
NUMBER_UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[个項项]\s*(" + _UNIT_ALT + r")?|(\d+(?:\.\d+)?)\s*(" + _UNIT_ALT + r")?", re.I)
# Table cells put the unit BEFORE the number across a separator:
# "最低总学分 | 26 | 公共学位课最低学分 | 7" — capture 学分…26 / 学分…7.
NUMBER_PREUNIT_RE = re.compile(r"(" + _UNIT_ALT + r")[\s:：|，,]{0,6}(\d+(?:\.\d+)?)")
ORDINAL_EN_RE = re.compile(r"(\d+)(?:st|nd|rd|th)\s+(" + _UNIT_ALT + r")", re.I)
ORDINAL_ZH_RE = re.compile(r"第\s*(\d+(?:\.\d+)?)\s*(" + _UNIT_ALT + r")")


def _unit_class(word: str) -> str:
    lowered = word.strip().lower()
    for cls, words in _UNIT_WORDS.items():
        if lowered in words:
            return cls
    return ""


def _number_facts(text: str) -> set[str]:
    facts: set[str] = set()
    for group1, unit1, group2, unit2 in NUMBER_UNIT_RE.findall(text):
        value, unit = (group1, unit1) if group1 else (group2, unit2)
        unit_cls = _unit_class(unit)
        if unit_cls:
            facts.add(f"{_num(value)}@{unit_cls}")
        else:
            # "N个" with no known unit is a generic measure word ("3个选项"),
            # and a bare 19xx/20xx year is term/cohort context (2025届) — the
            # date and term machinery owns both; neither is a quantity claim.
            if unit1 or unit2:
                continue
            normalized = _normalize(value)
            if re.fullmatch(r"(?:19|20)\d{2}", normalized):
                continue
            if len(normalized) > 1 or "." in normalized:  # skip bare single digits
                facts.add(f"{normalized}@")
    for unit, value in NUMBER_PREUNIT_RE.findall(text):
        unit_cls = _unit_class(unit)
        if unit_cls:
            facts.add(f"{_num(value)}@{unit_cls}")
    for value, unit in ORDINAL_EN_RE.findall(text) + ORDINAL_ZH_RE.findall(text):
        unit_cls = _unit_class(unit)
        if unit_cls:
            facts.add(f"{value}@{unit_cls}")
    return facts

_MONTHS = {name: index for index, name in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def _normalize(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def _num(value: str) -> str:
    """Canonical numeric string: '4.0' and '4' must compare equal."""
    return str(float(value)).rstrip("0").rstrip(".") if "." in value else value


def _canonical_date(value: str) -> str:
    value = value.strip()
    match = re.fullmatch(r"((?:19|20)\d{2})[-/](\d{1,2})[-/](\d{1,2})", value)
    if match:
        return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    match = re.fullmatch(r"((?:19|20)\d{2})年(\d{1,2})月(\d{1,2})日", value)
    if match:
        return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    match = re.fullmatch(r"(\d{1,2})月(\d{1,2})日", value)
    if match:
        return f"{int(match.group(1)):02d}-{int(match.group(2)):02d}"
    # optional time-of-day between day and year ("Sept 18 5pm 2026")
    match = re.fullmatch(r"([A-Za-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s+(?:\d{1,2}[:.]\d{2}|\d{1,2}\s?[ap]\.?m\.?))?(?:,?\s+((?:19|20)\d{2}))?", value)
    if match:
        month = _MONTHS.get(match.group(1)[:3].casefold())
        if month:
            year = match.group(3) or ""
            return f"{year}-{month:02d}-{int(match.group(2)):02d}" if year else f"{month:02d}-{int(match.group(2)):02d}"
    return _normalize(value)


def _canonical_time(raw: str) -> str:
    """'5pm'/'5 pm'/'17:00' → 24h 'HH:MM' so cross-form times compare equal."""
    text = raw.strip().casefold().replace(" ", "")
    match = re.fullmatch(r"(\d{1,2})[:.](\d{2})", text)
    if match:
        return f"{int(match.group(1)):02d}:{match.group(2)}"
    match = re.fullmatch(r"(\d{1,2})a\.?m\.?", text)
    if match:
        return f"{int(match.group(1)):02d}:00"
    match = re.fullmatch(r"(\d{1,2})p\.?m\.?", text)
    if match:
        return f"{int(match.group(1)) + 12:02d}:00"
    return text


def _mask_spans(text: str, spans: list[tuple[int, int]]) -> str:
    chars = list(text)
    for start, end in spans:
        for i in range(start, end):
            chars[i] = " "
    return "".join(chars)


#: Semester keys and "class of N" look exactly like course codes to the regex
#: but are term spellings, not facts ("FA 2026" vs evidence "Fall 2026" must
#: not flag; the pack's applicability layer owns term matching).  ZJUI has no
#: course prefix in this set (ECE/CS/CEE/MATH/RHET/…).
_TERM_CODE_PREFIXES = frozenset(load_profile().words("claim_validation", "term_code_prefixes"))
#: Month/day fragments left after full-date masking ("11月初" with evidence
#: "November", "6日" alone) are date constituents, not quantities.
_MONTH_FRAG_RE = load_profile().pattern("claim_validation", "month_fragment")


def extract_facts(text: str) -> dict[str, set[str]]:
    """Atomic facts of one text, by kind (dates / times / numbers / course_codes).

    Dates are extracted first and their spans MASKED OUT before the number
    scan runs: date constituents ("11月" of 11月6日, the "2026" of
    Sept 18 2026) and clock times (17:00, 5pm) must never surface as
    phantom unitless numbers (2026-10-15 label-noise fix).  Residual
    month/day fragments are masked the same way (2026-10-16).
    """
    text = str(text or "")
    masked = text
    dates: set[str] = set()
    for regex in (ISO_DATE_RE, ZH_DATE_RE, ZH_DATE_NOYEAR_RE, EN_DATE_RE):
        spans: list[tuple[int, int]] = []
        for match in regex.finditer(masked):
            span = match.span()
            canonical = _canonical_date(match.group(0))
            if canonical != _normalize(match.group(0)):
                dates.add(canonical)
            spans.append(span)
        masked = _mask_spans(masked, spans)
    times = {_canonical_time(m) for m in TIME_RE.findall(masked)}
    masked = _mask_spans(masked, [m.span() for m in TIME_RE.finditer(masked)])
    masked = _mask_spans(masked, [m.span() for m in _MONTH_FRAG_RE.finditer(masked)])
    codes: set[str] = set()
    for item in COURSE_CODE_RE.findall(text):
        letters = re.match(r"[A-Za-z]+", item)
        if letters and letters.group(0).casefold() in _TERM_CODE_PREFIXES:
            continue
        codes.add(_normalize(item))
    return {"dates": {d for d in dates if d}, "times": times, "numbers": _number_facts(masked), "course_codes": codes}


def _merge_facts(texts: Iterable[str]) -> dict[str, set[str]]:
    merged = {"dates": set(), "times": set(), "numbers": set(), "course_codes": set()}
    for text in texts:
        for kind, values in extract_facts(text).items():
            merged[kind] |= values
    return merged


def _date_supported(value: str, allowed: set[str]) -> bool:
    """Exact match, or a bare MM-DD covered by any allowed YYYY-MM-DD."""
    if value in allowed:
        return True
    if len(value) == 5:  # bare MM-DD claim ← full date evidence
        if any(item.endswith("-" + value) for item in allowed):
            return True
    if len(value) == 10:  # full-date claim ← bare MM-DD evidence ("11月6日" vs 2026年11月6日)
        if any(len(item) == 5 and value.endswith("-" + item) for item in allowed):
            return True
    return False


def validate_answer(claims: list[dict[str, Any]], pack: Any, *, question: str = "") -> dict[str, Any]:
    """Validate a structured answer's claims against the pack.

    ``claims``: [{text, evidence_ids}] as produced by a provider.
    ``pack``: contracts.EvidencePackModel (or any object with .evidence and
    .fact_conflicts).  Returns {ok, problems}.
    """
    problems: list[str] = []
    evidence_by_id = {item.evidence_id: item for item in pack.evidence}
    usable_ids = set(pack.usable_evidence_ids or [])
    if not claims:
        problems.append("answer has no claims")
    question_facts = extract_facts(question)
    for index, claim in enumerate(claims, 1):
        ids = [str(value) for value in claim.get("evidence_ids") or []]
        if not ids:
            problems.append(f"claim {index} cites no evidence")
            continue
        missing = [eid for eid in ids if eid not in evidence_by_id]
        if missing:
            problems.append(f"claim {index} cites unknown evidence {missing[:2]}")
            continue
        unusable = [eid for eid in ids if eid not in usable_ids or evidence_by_id[eid].blocking]
        if unusable:
            problems.append(f"claim {index} cites non-usable evidence {unusable[:2]}")
            continue
        claim_facts = extract_facts(str(claim.get("text", "")))
        allowed = _merge_facts([evidence_by_id[eid].text for eid in ids] + [question])
        for kind, values in claim_facts.items():
            if kind == "numbers":
                # digits that merely restate the question are allowed, and a
                # unit-translation gap is not a fabrication: claim "18" vs
                # evidence/question "18 AU" (or the reverse) shares the VALUE;
                # a different value (9 vs 3 credits) stays unsupported.
                pool = allowed[kind] | question_facts[kind]
                unsupported = set()
                for value in values:
                    if value in pool:
                        continue
                    head = value.partition("@")[0]
                    if any(item == head + "@" or item.startswith(head + "@") for item in pool):
                        continue
                    unsupported.add(value)
            elif kind == "dates":
                # a bare MM-DD ("10-20") is supported by any full date with
                # that month/day ("October 20, 2026" → 2026-10-20): the
                # no-year and full-year canonical forms must compare.
                unsupported = {v for v in values if not _date_supported(v, allowed[kind])}
            else:
                unsupported = values - allowed[kind]
            if unsupported:
                problems.append(f"claim {index} has unsupported {kind}: {sorted(unsupported)[:3]}")
    # Conflict packs: enforce "both sides" at FACT granularity.  The E2E run
    # on the 2026-10-15 generated set showed 108/110 unverified answers were
    # flagged by ONE known conflict on a ubiquitous calendar document —
    # doc-granular enforcement (cite the party doc for anything → demand the
    # counterpart) mislabels every answer that uses the doc for an unrelated
    # fact.  Enforcement fires only when the answer (a) cites a party AND
    # (b) actually states one of the conflict's fact values (value/span);
    # entries without values fall back to party-citation alone.
    pack_docs = {item.doc_id for item in pack.evidence}
    cited_evidence = {str(eid) for c in claims for eid in (c.get("evidence_ids") or [])}
    cited_docs = {evidence_by_id[eid].doc_id for eid in cited_evidence if eid in evidence_by_id}
    claims_blob = _normalize(" ".join(str(c.get("text", "")) for c in claims))
    for conflict in pack.fact_conflicts or []:
        raw_parties = conflict.get("parties", []) or []
        # parties arrive as doc_uid dicts (known_conflict) or evidence_id
        # strings (builder fact_conflict) — accept both shapes
        doc_parties = {str(side.get("doc_uid", "")) for side in raw_parties if isinstance(side, dict)} - {""}
        ev_parties = {str(side) for side in raw_parties if isinstance(side, str)}
        enforceable_docs = {doc for doc in doc_parties if doc in pack_docs}
        enforceable_evs = {eid for eid in ev_parties if eid in evidence_by_id}
        if not enforceable_docs and not enforceable_evs:
            continue
        if not (enforceable_docs & cited_docs) and not (enforceable_evs & cited_evidence):
            continue  # the answer is not about this conflict
        conflict_values = {
            _normalize(str(side.get(field, "")))
            for side in raw_parties if isinstance(side, dict)
            for field in ("value", "span")
        } - {""}
        conflict_values = {v for v in conflict_values if len(v) >= 4}
        if conflict_values and not any(value in claims_blob for value in conflict_values):
            continue  # cites the party document but not the conflicted fact itself
        for doc in sorted(enforceable_docs - cited_docs):
            problems.append(f"conflict party {doc[:18]} not cited — both sides must be presented")
        for eid in sorted(enforceable_evs - cited_evidence):
            problems.append(f"conflict party evidence {eid[:18]} not cited — both sides must be presented")
    return {"ok": not problems, "problems": problems}
