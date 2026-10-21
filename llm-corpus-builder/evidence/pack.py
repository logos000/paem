"""Evidence normalization and deterministic pre-generation checks.

The module is intentionally independent of any answer model.  It turns
retrieval hits into a stable, page/region-addressable EvidencePack and marks
version or applicability conflicts before a generator is allowed to see them.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
import json
import re
from typing import Any, Iterable

from decision.status import decide
from evidence.query_ir import AUDIENCE_COMPATIBLE, QueryIR, applicability_mismatches, extract_terms, identity_audience, parse_query


YEAR_RE = re.compile(r"(?:19|20)\d{2}")
DATE_RE = re.compile(r"(?:19|20)\d{2}[年/-]\s*\d{1,2}[月/-]\s*\d{1,2}日?")
ISO_DATE_RE = re.compile(r"(?:19|20)\d{2}[-/]\d{1,2}[-/]\d{1,2}")
ENGLISH_DATE_RE = re.compile(r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2}(?:,?\s+(?:19|20)\d{2})?", re.I)
NUMERIC_FACT_RE = re.compile(r"(?<![A-Za-z0-9])\d+(?:\.\d+)?\s*(?:credits?|points?|weeks?|days?|hours?|minutes?|pm|am|%)\b", re.I)
COURSE_CODE_RE = re.compile(r"\b[A-Z]{2,}[ -]?\d{2,}[A-Za-z0-9-]*\b")
MONTHS = {name: index for index, name in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
NUMBER_FACT_RE = re.compile(r"(?<![A-Za-z0-9])\d+(?:\.\d+)?\s*(?:学分|分数|分|门|周|天|小时|分钟|次|credits?|points?|weeks?|days?|hours?|minutes?|%)\b", re.I)

STOP_WORDS = {
    "a", "an", "the", "is", "are", "was", "were", "what", "when", "where",
    "which", "who", "how", "can", "could", "的", "是", "吗", "呢", "请", "帮我",
    "一下", "什么", "哪个", "哪里", "谁", "如何", "怎样",
}

# Broad-scope questions need a corpus-level answer.  A course syllabus can
# support a course requirement, but it cannot establish a university-wide
# graduation rule.  Keep this detector conservative; a later query planner
# can replace it with an explicit scope ontology.
SCOPE_MARKERS = (
    "all", "entire", "any", "unconditional", "whole", "overall",
    "graduation", "degree", "program-wide", "全部", "所有", "无条件",
    "全校", "毕业", "学位",
)
COURSE_SCOPE_MARKERS = (
    "syllabus", "course", "st4001", "st 4001", "课程",
    "course-specific", "specific course",
)
BROAD_SCOPES = {"university", "institute", "school", "program"}

# Applicability mismatches block evidence from coverage; premise mismatches
# (the question presumes 4 credits, the evidence says 3) do not, because that
# evidence is exactly what an answer needs to correct the premise.
PREMISE_KINDS = {"date", "course_code", "number"}


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def _text_tokens(value: str) -> set[str]:
    value = str(value or "").casefold()
    parts = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", value)
    words: set[str] = set()
    for part in parts:
        if re.fullmatch(r"[\u4e00-\u9fff]+", part):
            words.add(part)
            words.update(part[index:index + 2] for index in range(len(part) - 1))
        else:
            words.add(part)
    return {word for word in words if len(word) > 1 or word.isascii()}


def _required_terms(question: str, query_pool: Iterable[str] = ()) -> set[str]:
    """Terms the evidence must address: question tokens UNION variant tokens.

    The retrieval variants (LLM-optimized queries) are the system's own
    restatement of the same information need.  For a Chinese question against
    an English corpus the question's own tokens can never be matched — without
    the union, coverage is structurally ~0 and every such pack is refused as
    ``no_material`` even when the right documents sit inside it (2026-10-16
    cross-lingual fix).  Pooling keeps the gate conservative: junk that only
    covers the original tokens still fails the diluted ratio.
    """
    words = _text_tokens(question)
    for variant in query_pool or ():
        words |= _text_tokens(str(variant))
    return {word for word in words if word not in STOP_WORDS}


def _paraphrase_groups(question: str, query_pool: Iterable[str] = ()) -> list[set[str]]:
    """The information need restated once per query (question + variants).

    Each group is a paraphrase of the same need, not an additional demand, so
    a requirement is satisfied when ANY one group is fully covered — the
    English restatement covered by English evidence counts even when the
    Chinese original is unmatchable.  With no pool this degenerates to the
    pre-2026-10-16 single-group behaviour.
    """
    groups: list[set[str]] = []
    for text in [question, *(query_pool or ())]:
        tokens = {token for token in _text_tokens(str(text)) if token not in STOP_WORDS}
        if tokens:
            groups.append(tokens)
    return groups


def _years(value: str) -> set[str]:
    return set(YEAR_RE.findall(str(value or "")))


def _canonical_date(value: str) -> str:
    value = str(value or "").strip()
    match = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", value)
    if match:
        return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    match = re.fullmatch(r"([A-Za-z]+)\s+(\d{1,2})(?:,?\s+(\d{4}))?", value)
    if match:
        month = MONTHS.get(match.group(1)[:3].casefold())
        if month and match.group(3):
            return f"{int(match.group(3)):04d}-{month:02d}-{int(match.group(2)):02d}"
    return value


def _fact_slots(value: str) -> dict[str, set[str]]:
    text = str(value or "")
    dates = {_canonical_date(item) for item in ISO_DATE_RE.findall(text)}
    dates.update(_canonical_date(item) for item in DATE_RE.findall(text))
    dates.update(_canonical_date(item) for item in ENGLISH_DATE_RE.findall(text))
    return {
        "years": _years(text),
        "dates": dates,
        "course_codes": {item.casefold().replace(" ", "") for item in COURSE_CODE_RE.findall(text)},
        "number_facts": ({re.sub(r"\s+", "", item).casefold() for item in NUMBER_FACT_RE.findall(text)} | {re.sub(r"\s+", "", item).casefold() for item in NUMERIC_FACT_RE.findall(text)}),
    }


def _structured(metadata: dict[str, Any]) -> bool:
    return bool(metadata.get("terms") or metadata.get("academic_years") or metadata.get("audience"))


def _conflicts(metadata: dict[str, Any], context: dict[str, Any], question: str, text: str, ir: QueryIR | None = None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if ir is not None and _structured(metadata):
        # Corpus documents carry normalized terms/audience/cohorts; compare
        # them with the question instead of raw year strings in the text.
        result.extend(applicability_mismatches(ir, metadata))
    else:
        question_years = _years(question)
        evidence_years = _years(" ".join([text, str(metadata.get("valid_from", "")), str(metadata.get("valid_to", ""))]))
        if question_years and evidence_years and question_years.isdisjoint(evidence_years):
            result.append({"kind": "year", "reason": "question and evidence mention disjoint years"})
    requested_facts = _fact_slots(question)
    evidence_facts = _fact_slots(text)
    for key, label in (("dates", "date"), ("course_codes", "course_code"), ("number_facts", "number")):
        if requested_facts[key] and evidence_facts[key] and requested_facts[key].isdisjoint(evidence_facts[key]):
            result.append({"kind": label, "reason": f"question and evidence mention disjoint {label} facts"})
    for key in ("program", "cohort", "term"):
        expected = str(metadata.get(key) or "").strip().casefold()
        actual = str(context.get(key) or "").strip().casefold()
        if expected and actual and expected not in actual and actual not in expected:
            if not any(item["kind"] == key for item in result):
                result.append({"kind": key, "reason": f"evidence {key} does not match requested context"})
    for item in result:
        item["blocking"] = item["kind"] not in PREMISE_KINDS
    return result


def _score(question: str, text: str, retrieval_score: Any, semantic_score: Any, rerank_score: Any = None,
           query_pool: Iterable[str] = ()) -> float:
    # The question group keeps the historical unfiltered tokenization (frozen
    # support scores); variant groups are keyword lists, so stopword filtering
    # only removes prompt filler there.
    groups = [_text_tokens(question)]
    groups += [{t for t in _text_tokens(str(v)) if t not in STOP_WORDS}
               for v in (query_pool or ()) if str(v).strip()]
    body = _text_tokens(text)
    # Best-paraphrase overlap: an English block restating the English variant
    # of a Chinese question scores 1.0, not a diluted union ratio.
    lexical = max((len(group & body) / max(1, len(group)) for group in groups), default=0.0)
    values = [float(value) for value in (retrieval_score, semantic_score, rerank_score) if isinstance(value, (int, float))]
    base = max(values, default=0.0)
    # Scores from different retrieval channels are not calibrated.  Keep the
    # lexical component explicit and bounded instead of pretending they are
    # probabilities.
    return round(min(1.0, 0.55 * lexical + 0.45 * min(1.0, max(0.0, base))), 4)


# Table fact taxonomy (step 8): what a numeric cell MEANS depends on its
# column header — course capacity, credits, headcount and dates must never be
# interchangeable just because they are all digits.
_TABLE_FACT_TYPES = (
    ("credit", ("credit", "学分")),
    ("capacity", ("seat", "capacity", "容量", "名额", "enrollment limit", "招收")),
    ("gpa", ("gpa", "绩点", "平均分")),
    ("date", ("date", "deadline", "day", "时间", "日期", "截止")),
    ("count", ("count", "number", "人数", "数量", "max", "上限", "total", "总计")),
)


def _table_fact_types(header: list[str]) -> list[str]:
    joined = " ".join(str(cell) for cell in header or []).casefold()
    return [name for name, markers in _TABLE_FACT_TYPES if any(marker in joined for marker in markers)]


def _table_context(row_metadata: dict[str, Any], heading: str) -> dict[str, Any]:
    """Bind a table-row hit to its table identity (step 8): table id, column
    header, row index, cells and the fact types the header implies."""
    if not (row_metadata.get("table_id") or row_metadata.get("kind") == "table_row"):
        return {}
    header = row_metadata.get("column_header") or []
    return {
        "table_id": str(row_metadata.get("table_id", "")),
        "table_title": heading.split(" / ")[0] if heading else "",
        "header": list(header) if isinstance(header, list) else [],
        "row_index": row_metadata.get("row_index"),
        "cells": list(row_metadata.get("row_cells") or []),
        "fact_types": _table_fact_types(header),
    }


def _item(question: str, hit: dict[str, Any], rank: int, context: dict[str, Any], ir: QueryIR | None = None,
          query_pool: Iterable[str] = ()) -> dict[str, Any]:
    row = hit.get("chunk") or hit
    doc = hit.get("document") or {}
    metadata = _as_dict(getattr(doc, "metadata_json", None))
    if not metadata:
        metadata = _as_dict(doc.get("metadata") if isinstance(doc, dict) else None)
    row_metadata = _as_dict(getattr(row, "metadata_json", None))
    if not row_metadata:
        row_metadata = _as_dict(row.get("metadata") if isinstance(row, dict) else None)
    text = str(getattr(row, "parent_text", None) or getattr(row, "text", None) or row.get("parent_text", row.get("text", "") if isinstance(row, dict) else ""))
    context_text = str(row_metadata.get("retrieval_text", "") or "")
    searchable_text = "\n".join(part for part in (text, context_text) if part)
    title = str(getattr(doc, "title", None) or doc.get("title", "") if isinstance(doc, dict) else getattr(doc, "title", ""))
    doc_id = str(getattr(doc, "id", None) or doc.get("id", "") if isinstance(doc, dict) else getattr(doc, "id", ""))
    chunk_id = str(getattr(row, "id", None) or row.get("id", "") if isinstance(row, dict) else getattr(row, "id", ""))
    page = int(getattr(row, "page", None) or row.get("page", 1) if isinstance(row, dict) else getattr(row, "page", 1))
    heading = str(getattr(row, "heading", None) or row.get("heading", "") if isinstance(row, dict) else getattr(row, "heading", ""))
    merged = {**metadata, **row_metadata}
    conflicts = _conflicts(merged, context, question, searchable_text, ir)
    images = hit.get("images") or row_metadata.get("images") or []
    required = _required_terms(question, query_pool)
    matched = sorted(required & _text_tokens(searchable_text))
    requested_facts = _fact_slots(question)
    evidence_facts = _fact_slots(searchable_text)
    fact_matches = {key: sorted(values & evidence_facts[key]) for key, values in requested_facts.items() if values}
    fact_missing = {key: sorted(values - evidence_facts[key]) for key, values in requested_facts.items() if values and values.isdisjoint(evidence_facts[key])}
    document_hash = getattr(doc, "content_hash", None)
    if not document_hash and isinstance(doc, dict):
        document_hash = doc.get("content_hash")
    source_hash = merged.get("content_hash") or merged.get("source_hash") or document_hash or ""
    version_id = merged.get("version_id") or merged.get("version") or ""
    return {
        "evidence_id": f"{doc_id}:{chunk_id}:{rank}",
        "chunk_id": chunk_id,
        "doc_id": doc_id,
        "title": title,
        "page": page,
        "heading": heading,
        "text": text,
        "quote": text[:1200],
        "context_text": context_text,
        "bbox": row_metadata.get("bbox", []),
        "bbox_source": row_metadata.get("bbox_source", ""),
        "kind": row_metadata.get("kind", "text"),
        "table_context": _table_context(row_metadata, heading),
        "structure": {key: row_metadata[key] for key in ("table_id", "row_index", "col_index", "row_span", "col_span", "column_header", "row_header") if key in row_metadata},
        "images": images,
        "version_id": version_id,
        "source_hash": source_hash,
        "valid_from": merged.get("valid_from", ""),
        "valid_to": merged.get("valid_to", ""),
        "applicable_to": {key: merged.get(key, "") for key in ("program", "cohort", "term") if merged.get(key)},
        "rank": rank,
        "retrieval_score": hit.get("score"),
        "semantic_score": hit.get("semantic_score"),
        "rerank_score": hit.get("rerank_score"),
        "support_score": _score(question, searchable_text, hit.get("score"), hit.get("semantic_score"), hit.get("rerank_score"), query_pool),
        "matched_terms": matched,
        "missing_terms": sorted(required - set(matched)),
        "fact_matches": fact_matches,
        "missing_facts": fact_missing,
        "fact_coverage": round(sum(len(values) for values in fact_matches.values()) / max(1, sum(len(values) for values in requested_facts.values())), 4) if any(requested_facts.values()) else 1.0,
        "conflicts": conflicts,
        "blocking": any(conflict["blocking"] for conflict in conflicts),
        "provenance": {key: merged[key] for key in PROVENANCE_KEYS if merged.get(key) not in (None, "", [])},
        "applicability": {key: merged[key] for key in APPLICABILITY_KEYS if merged.get(key) not in (None, "", [])},
        "version": {
            "series_id": merged.get("series_id", ""),
            "is_latest": merged.get("is_latest", True),
            "superseded_by": merged.get("superseded_by", ""),
            "version_order": merged.get("version_order", 1),
        },
    }


PROVENANCE_KEYS = ("doc_uid", "block_id", "source_url", "alias_urls", "site", "publisher", "published", "modified", "doc_number", "heading_path", "char_start", "char_end", "build_id", "raw_sha256")
APPLICABILITY_KEYS = ("doc_type", "audience", "scope_level", "tier", "terms", "academic_years", "cohorts", "authority_rank")


def _version_conflicts(items: list[dict[str, Any]], ir: QueryIR | None, advisory: bool = False) -> None:
    """Block superseded editions when the newest edition of the same series is present.

    A question pinned to an explicit term keeps every edition for that term;
    the term check already removed editions of other terms.

    Advisory mode (2026-10-17) records the supersession as a non-blocking
    label instead — the old edition stays usable evidence for the generator.
    """
    if ir is not None and ir.terms and not ir.wants_latest:
        return
    latest_series = {item["version"]["series_id"] for item in items if item["version"]["series_id"] and item["version"]["is_latest"] and not item["blocking"]}
    for item in items:
        version = item["version"]
        if not version["series_id"] or version["is_latest"]:
            continue
        if version["series_id"] in latest_series:
            item["conflicts"].append({"kind": "superseded", "reason": "a newer edition of this document is in the evidence pack", "blocking": not advisory})
            if not advisory:
                item["blocking"] = True
        else:
            item["conflicts"].append({"kind": "stale_risk", "reason": f"superseded by {version['superseded_by']}; newer edition not retrieved", "blocking": False})


def _fact_disagreements(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """P3: two *current* editions of one series asserting disjoint fact values.

    Two co-latest editions (``ambiguous_latest``) or current docs sharing a
    series disagreeing on dates/credits/numbers must surface as a conflict,
    not be silently merged into one answer.  Non-blocking: the pack keeps both
    sides and the decision layer labels it ``conflict``.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        series = (item.get("version") or {}).get("series_id") or ""
        if series and not item.get("blocking") and (item.get("version") or {}).get("is_latest", True):
            groups.setdefault(series, []).append(item)
    disagreements: list[dict[str, Any]] = []
    for series, members in sorted(groups.items()):
        by_doc: dict[str, list[dict[str, Any]]] = {}
        for item in members:
            facts = _fact_slots(str(item.get("text", "")))
            for kind in ("dates", "number_facts"):
                if facts[kind]:
                    by_doc.setdefault(item["doc_id"], []).append({"item": item, "kind": kind, "values": facts[kind]})
        for doc_a, entries_a in by_doc.items():
            for doc_b, entries_b in by_doc.items():
                if doc_a >= doc_b:
                    continue
                for entry_a in entries_a:
                    for entry_b in entries_b:
                        if entry_a["kind"] != entry_b["kind"]:
                            continue
                        if entry_a["values"].isdisjoint(entry_b["values"]):
                            disagreements.append({
                                "kind": "fact_conflict",
                                "fact_kind": entry_a["kind"],
                                "parties": [entry_a["item"]["evidence_id"], entry_b["item"]["evidence_id"]],
                                "values": {doc_a: sorted(entry_a["values"]), doc_b: sorted(entry_b["values"])},
                                "series_id": series,
                                "reason": "two current editions of the same series assert disjoint values",
                                "blocking": False,
                            })
    return disagreements


def _match_known_conflicts(items: list[dict[str, Any]], hits: Iterable[dict[str, Any]], known: list[dict[str, Any]], question: str, context: dict[str, Any], ir: QueryIR | None, start_rank: int, query_pool: Iterable[str] = ()) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """P3: retain both sides of build-detected conflicts inside the pack.

    For every build-time conflict candidate with at least one party already in
    the pack: emit a non-blocking ``known_conflict`` entry carrying both
    parties (value/span/published/authority).  When exactly one party is in
    the pack but the other was retrieved (present in ``hits``), the missing
    side is appended as a compact ``conflict_counterpart`` item so the
    generator sees the disagreement verbatim instead of trusting the survivor.
    """
    if not known:
        return [], []
    present_docs = {item["doc_id"] for item in items}
    hits_by_doc: dict[str, dict[str, Any]] = {}
    for hit in hits:
        document = hit.get("document") or {}
        doc_id = str(document.get("id", "") if isinstance(document, dict) else getattr(document, "id", ""))
        if doc_id and doc_id not in hits_by_doc:
            hits_by_doc[doc_id] = hit
    entries: list[dict[str, Any]] = []
    appended: list[dict[str, Any]] = []
    for record in known:
        parties = record.get("values") or []
        uids = [side.get("doc_uid", "") for side in parties]
        matched = [uid for uid in uids if uid in present_docs]
        if not matched:
            continue
        entries.append({
            "kind": "known_conflict",
            "fact_kind": record.get("fact_kind", ""),
            "series_id": record.get("series_id", ""),
            "series_source": record.get("series_source", ""),
            "parties": parties,
            "matched_docs": matched,
            "blocking": False,
        })
        missing = [uid for uid in uids if uid not in present_docs]
        if len(matched) == 1 and missing:
            hit = hits_by_doc.get(missing[0])
            if hit is not None:
                counterpart = _item(question, hit, start_rank + len(appended) + 1, context, ir, query_pool)
                counterpart["conflict_counterpart"] = True
                counterpart["conflicts"].append({"kind": "known_conflict_counterpart", "reason": f"appended to retain both sides of a {record.get('fact_kind', '')} conflict", "blocking": False})
                appended.append(counterpart)
                present_docs.add(missing[0])
    return entries, appended


def _hit_text(hit: dict[str, Any]) -> str:
    row = hit.get("chunk") or hit
    if isinstance(row, dict):
        metadata = _as_dict(row.get("metadata") or row.get("metadata_json"))
        raw = str(row.get("parent_text") or row.get("text") or "")
    else:
        metadata = _as_dict(getattr(row, "metadata_json", None))
        raw = str(getattr(row, "parent_text", None) or getattr(row, "text", None) or "")
    return "\n".join(part for part in (raw, str(metadata.get("retrieval_text", "") or "")) if part)


def _hit_numeric_score(hit: dict[str, Any]) -> float:
    values = [hit.get(key) for key in ("rerank_score", "semantic_score", "score")]
    return max((float(value) for value in values if isinstance(value, (int, float))), default=0.0)


def _hit_identity(hit: dict[str, Any]) -> tuple[str, int, str]:
    """Return stable document/page/kind identity for diversity selection."""
    row = hit.get("chunk") or hit
    document = hit.get("document") or {}
    if isinstance(row, dict):
        metadata = _as_dict(row.get("metadata") or row.get("metadata_json"))
        page = row.get("page", 1)
        kind = metadata.get("kind", "text")
        row_doc_id = row.get("doc_id", "")
    else:
        metadata = _as_dict(getattr(row, "metadata_json", None))
        page = getattr(row, "page", 1)
        kind = metadata.get("kind", "text")
        row_doc_id = getattr(row, "doc_id", "")
    if isinstance(document, dict):
        doc_id = document.get("id", "") or row_doc_id
    else:
        doc_id = getattr(document, "id", "") or row_doc_id
    try:
        page = int(page or 1)
    except (TypeError, ValueError):
        page = 1
    return str(doc_id), page, str(kind)


def _coverage_select(question: str, hits: Iterable[dict[str, Any]], limit: int, query_pool: Iterable[str] = ()) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Greedily cover query terms and explicit facts before filling by rank.

    Returns ``(selected, dropped)`` — the remainder is the coverage-selection
    casualty list, surfaced as pack diagnostics instead of vanishing silently.
    """
    candidates = list(hits)
    if len(candidates) <= limit:
        return candidates, []
    required = _required_terms(question, query_pool)
    requested_facts = _fact_slots(question)
    covered_terms: set[str] = set()
    covered_facts = {key: set() for key in requested_facts}
    selected_documents: set[str] = set()
    selected_pages: set[tuple[str, int]] = set()
    selected_kinds: set[str] = set()
    selected: list[dict[str, Any]] = []
    while candidates and len(selected) < limit:
        best_index, best_gain = 0, float("-inf")
        for index, hit in enumerate(candidates):
            text = _hit_text(hit)
            terms = _text_tokens(text) & required
            facts = _fact_slots(text)
            new_terms = len(terms - covered_terms)
            new_facts = sum(len(facts[key] - covered_facts[key]) for key in requested_facts)
            doc_id, page, kind = _hit_identity(hit)
            page_key = (doc_id, page)
            # A high-scoring table cell can otherwise consume the complete
            # evidence budget before its parent row or a corroborating policy
            # document is considered.  Reward a new source only when it has
            # some query compatibility, so unrelated documents do not win on
            # diversity alone.
            compatible = bool(terms or any(facts[key] & requested_facts[key] for key in requested_facts))
            source_gain = 0.0
            if compatible and doc_id not in selected_documents:
                source_gain += 8.0
            if compatible and page_key not in selected_pages:
                source_gain += 2.0
            if kind not in selected_kinds:
                source_gain += 0.25
            # Explicit facts receive more weight than generic lexical overlap;
            # rank and model scores break ties without becoming probabilities.
            gain = source_gain + 3.0 * new_terms + 5.0 * new_facts + 0.05 * _hit_numeric_score(hit) + 0.01 / (index + 1)
            if gain > best_gain:
                best_index, best_gain = index, gain
        hit = candidates.pop(best_index)
        selected.append(hit)
        text = _hit_text(hit)
        covered_terms.update(_text_tokens(text) & required)
        facts = _fact_slots(text)
        for key in covered_facts:
            covered_facts[key].update(facts[key] & requested_facts[key])
        doc_id, page, kind = _hit_identity(hit)
        selected_documents.add(doc_id)
        selected_pages.add((doc_id, page))
        selected_kinds.add(kind)
    return selected, candidates


def _scope_gap(question: str, items: list[dict[str, Any]]) -> dict[str, str] | None:
    """Detect a broad question answered only by course-scoped evidence.

    This is intentionally a safety signal rather than a relevance score.  It
    prevents a locally relevant syllabus from being presented as a complete
    university-wide policy when the indexed evidence has narrower scope.
    """
    lowered = str(question or "").casefold()
    if not any(marker.casefold() in lowered for marker in SCOPE_MARKERS):
        return None
    usable = [item for item in items if not item.get("blocking", bool(item.get("conflicts")))]
    if not usable:
        return None
    scopes = {str(item.get("applicability", {}).get("scope_level", "")) for item in usable}
    if all(scopes):
        # Corpus metadata is authoritative: broad questions need at least one
        # document whose scope is broader than a single course.
        if scopes & BROAD_SCOPES:
            return None
        return {"kind": "scope-gap", "reason": f"question asks for a broad policy but evidence scope is {sorted(scopes)}"}
    bodies = "\n".join(
        " ".join((str(item.get("title", "")), str(item.get("heading", "")), str(item.get("text", ""))))
        for item in usable
    ).casefold()
    course_scoped = any(marker.casefold() in bodies for marker in COURSE_SCOPE_MARKERS)
    if not course_scoped:
        return None
    doc_ids = {str(item.get("doc_id", "")) for item in usable if item.get("doc_id")}
    if len(doc_ids) > 1:
        # Multiple independent documents may establish a broader rule; leave
        # the decision to the claim verifier unless their metadata says so.
        return None
    return {
        "kind": "scope-gap",
        "reason": "question asks for a broad policy but retrieved evidence is course-scoped",
    }


def _constraint_gaps(ir: QueryIR, usable: list[dict[str, Any]], items: list[dict[str, Any]] | None = None, question: str = "", query_pool: Iterable[str] = ()) -> list[dict[str, str]]:
    """Hard constraints that none of the usable evidence covers.

    Two refusal signals live here rather than in per-item conflicts:

    - A question that pins a term (``2027-spring``) must be answered by
      evidence declaring that term.  Documents without term metadata neither
      violate nor cover the constraint, so "retrieved something topically
      similar" is not coverage — that is exactly the future-term trap.
      Titles/headings are a fallback so a guide whose metadata missed the
      term still counts when its name carries it.
    - A question whose asker and topic audiences are incompatible (an
      undergraduate asking about a postdoc position) cannot be answered by
      any in-scope evidence, regardless of what retrieval surfaced.
    """
    gaps: list[dict[str, str]] = []
    asked = set(ir.terms)
    # A SOFT term key (relative_term == "latest", 2026-10-17) is a retrieval
    # preference, not a question constraint: "最新的<旧系列文档>" must stay
    # answerable from that series' latest edition even when it predates the
    # current term.  Explicit-term questions keep the full future-term trap.
    if asked and getattr(ir, "relative_term", "") != "latest":
        for item in usable:
            applicability = item.get("applicability", {})
            if set(applicability.get("terms") or []) & asked:
                break
            title_terms, _ = extract_terms(f"{item.get('title', '')} {item.get('heading', '')}")
            if set(title_terms) & asked:
                break
        else:
            gaps.append({"kind": "term-gap", "reason": f"no retrieved evidence covers the requested term(s) {sorted(asked)}"})
    if ir.audience and ir.topic_audience and ir.topic_audience not in AUDIENCE_COMPATIBLE.get(ir.audience, {ir.topic_audience}):
        gaps.append({"kind": "audience-mismatch", "reason": f"question is asked by a {ir.audience} but concerns {ir.topic_audience}-only material"})
    audience_gap = _audience_evidence_gap(question, ir, items, usable, query_pool)
    if audience_gap:
        gaps.append(audience_gap)
    return gaps


def _audience_evidence_gap(question: str, ir: QueryIR, items: list[dict[str, Any]], usable: list[dict[str, Any]], query_pool: Iterable[str] = ()) -> dict[str, str] | None:
    """The answer exists, but only for another audience.

    When the question never names the other audience, the mismatch is only
    visible in evidence metadata: audience-blocked items cover the question's
    topic terms while no usable item does.  The subtraction keeps an
    undergraduate rule with the same wording usable (both editions cover the
    terms, so nothing is exclusively blocked), which is what separates this
    from a false refusal.
    """
    if not ir.audience or not usable:
        return None
    # Only a stated asker identity makes "the answer is not for you" decidable;
    # an audience inferred from topic words must not block faculty/graduate
    # evidence against each other.
    if not identity_audience(question):
        return None
    blocked = [item for item in items if item.get("blocking") and any(conflict.get("kind") == "audience" for conflict in item.get("conflicts", []))]
    if not blocked:
        return None
    required = _required_terms(question, query_pool)
    if not required:
        return None
    blocked_cov = set().union(*(set(item.get("matched_terms") or []) for item in blocked)) & required
    usable_cov = set().union(*(set(item.get("matched_terms") or []) for item in usable)) & required
    exclusive = blocked_cov - usable_cov
    if exclusive:
        return {"kind": "audience-mismatch", "reason": f"the only evidence covering {sorted(exclusive)[:4]} targets a different audience"}
    return None


def _requirements(requested_facts: dict[str, set[str]], covered_facts: dict[str, set[str]], usable: list[dict[str, Any]],
                  required: set[str], covered: set[str], paraphrase_groups: list[set[str]] | None = None) -> list[dict[str, Any]]:
    """Atomic per-question requirements (step 8): every fact slot the question
    demands becomes one requirement with its own coverage status, so the
    product can tell a fully-covered answer from a fluent half-answer."""
    requirements: list[dict[str, Any]] = []
    for kind, values in requested_facts.items():
        for target in sorted(values):
            covered_ids = [item["evidence_id"] for item in usable
                           if target in (item.get("fact_matches", {}).get(kind) or [])]
            requirements.append({
                "requirement_id": f"req-{kind}-{target}",
                "type": kind,
                "normalized_target": target,
                "covered_evidence_ids": covered_ids[:4],
                "status": "covered" if covered_ids else "missing",
            })
    if required:
        # A paraphrase group (question or one retrieval variant) fully covered
        # means the information need itself is satisfied in the language the
        # evidence speaks; the diluted pooled ratio stays on the pack for the
        # abstention gate only.
        groups = paraphrase_groups or [required]
        covered_any = any(len(covered & group) >= len(group) for group in groups if group)
        requirements.append({
            "requirement_id": "req-terms",
            "type": "terms",
            "normalized_target": sorted(required)[:8],
            "covered_evidence_ids": [],
            "status": "covered" if covered_any else "missing",
        })
    return requirements


def build_evidence_pack(question: str, hits: Iterable[dict[str, Any]], context: dict[str, Any] | None = None, limit: int = 8, today: date | None = None, known_conflicts: Iterable[dict[str, Any]] | None = None, ir: QueryIR | None = None, hard_drop_kinds: list[str] | None = None, query_pool: Iterable[str] | None = None, advisory: bool = False) -> dict[str, Any]:
    """Build a generator-independent evidence contract from retrieval hits.

    ``context`` holds explicit user context (program/cohort/term).  The
    question itself is parsed into a :class:`QueryIR` so that term, audience
    and cohort constraints come from the question, never from benchmark gold.
    ``known_conflicts`` (P3) carries build-time conflict candidates
    (``conflict_candidates.jsonl``): both sides of a matched conflict are
    retained in the pack instead of being silently merged.  ``ir`` may pass a
    pre-parsed (or user-context-merged) QueryIR; omitted → parsed here.
    ``query_pool`` (2026-10-16) carries the retrieval variants actually used:
    required terms and per-paraphrase requirement coverage are computed over
    the question UNION the pool, so a Chinese question against English
    evidence is judged by its English restatement instead of being
    structurally uncoverable.
    """
    context = context or {}
    if ir is None:
        ir = parse_query(question, today=today)
    pool = [str(variant) for variant in (query_pool or ()) if str(variant).strip()]
    groups = _paraphrase_groups(question, pool)
    hits = list(hits)
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    dropped: list[dict[str, Any]] = []

    def _record_drop(hit: dict[str, Any], rank: int, reason: str) -> None:
        if len(dropped) >= 5:
            return
        chunk = hit.get("chunk") if isinstance(hit, dict) else None
        document = hit.get("document") if isinstance(hit, dict) else None
        doc_uid = str(hit.get("doc_uid") or hit.get("doc_id") or getattr(document, "id", "") or "")
        block_id = str(hit.get("block_id") or getattr(chunk, "id", "") or "")
        dropped.append({
            "doc_uid": doc_uid,
            "block_id": block_id,
            "rank": rank,
            "score": round(float(hit.get("score", 0) or 0), 4),
            "reason": reason,
        })

    selected_hits, coverage_dropped = _coverage_select(question, hits, limit, pool)
    for rank, hit in enumerate(selected_hits, 1):
        item = _item(question, hit, rank, context, ir, pool)
        key = (item["doc_id"], item["page"], item["text"])
        if key in seen:
            _record_drop(hit, rank, "dedup_merge")
            continue
        seen.add(key)
        items.append(item)
        if len(items) >= limit:
            for tail_rank, tail_hit in enumerate(selected_hits[rank:], rank + 1):
                _record_drop(tail_hit, tail_rank, "pack_limit")
            break
    for drop_rank, drop_hit in enumerate(coverage_dropped, len(selected_hits) + 1):
        _record_drop(drop_hit, drop_rank, "coverage")
    _version_conflicts(items, ir, advisory=advisory)
    known_entries, appended = _match_known_conflicts(items, hits, list(known_conflicts or []), question, context, ir, len(items), pool)
    items.extend(appended)
    fact_disagreements = _fact_disagreements(items)
    usable = [item for item in items if not item["blocking"]]
    required = _required_terms(question, pool)
    covered = set().union(*(set(item["matched_terms"]) for item in usable)) if usable else set()
    term_coverage = len(required & covered) / max(1, len(required))
    requested_facts = _fact_slots(question)
    covered_facts = {
        key: set().union(*(set(item.get("fact_matches", {}).get(key, [])) for item in usable)) if usable else set()
        for key in requested_facts
    }
    fact_total = sum(len(values) for values in requested_facts.values())
    fact_coverage = sum(len(covered_facts[key] & requested_facts[key]) for key in requested_facts) / max(1, fact_total)
    max_support = max((item["support_score"] for item in usable), default=0.0)
    if any(requested_facts.values()):
        coverage = round(0.45 * term_coverage + 0.30 * fact_coverage + 0.25 * max_support, 4)
    else:
        coverage = round((0.6 * term_coverage + 0.4 * max_support) if required else max_support, 4)
    conflicts = [
        {"evidence_id": item["evidence_id"], **conflict}
        for item in items
        for conflict in item["conflicts"]
    ]
    conflicts.extend(known_entries)
    conflicts.extend(fact_disagreements)
    scope_gap = _scope_gap(question, items)
    if scope_gap:
        conflicts.append(scope_gap)
    constraint_gaps = _constraint_gaps(ir, usable, items, question, pool)
    conflicts.extend(constraint_gaps)
    premise = [conflict for conflict in conflicts if conflict.get("kind") in PREMISE_KINDS]
    # The abstention decision lives in decision.status; ``abstain`` and
    # ``abstain_reason`` are parity-frozen, ``evidence_status`` refines them.
    decision = decide(
        usable_count=len(usable),
        coverage=coverage,
        scope_gap=scope_gap,
        constraint_gaps=constraint_gaps,
        version_conflicts=[conflict for conflict in conflicts if conflict.get("kind") == "superseded"],
        stale_risk_conflicts=[conflict for conflict in conflicts if conflict.get("kind") == "stale_risk"],
        fact_conflicts=[conflict for conflict in conflicts if conflict.get("kind") in ("fact_conflict", "known_conflict")],
        hard_drop_kinds=hard_drop_kinds,
        advisory=advisory,
    )
    requirements = _requirements(requested_facts, covered_facts, usable, required, covered, groups)
    return {
        "query": question,
        "query_ir": ir.to_json(),
        "context": {key: str(value) for key, value in context.items() if value},
        "evidence": items,
        "requirements": requirements,
        "coverage": coverage,
        "required_terms": sorted(required),
        "covered_terms": sorted(required & covered),
        "missing_terms": sorted(required - covered),
        "fact_coverage": round(fact_coverage, 4),
        "covered_facts": {key: sorted(values) for key, values in covered_facts.items() if values},
        "missing_facts": {key: sorted(values - covered_facts[key]) for key, values in requested_facts.items() if values and values.isdisjoint(covered_facts[key])},
        "conflicts": conflicts,
        "premise_conflicts": premise,
        "fact_conflicts": [c for c in conflicts if c.get("kind") in ("fact_conflict", "known_conflict")],
        "usable_evidence_ids": [item["evidence_id"] for item in usable],
        # Diagnostics (2026-10-15, never metric-feeding): top candidates the
        # pack assembly dropped (coverage selection / dedup merge / limit).
        # Makes "governance dropped the answer block" distinguishable from
        # "the corpus has nothing" — the drop-deadlock bug class.
        "dropped_candidates": dropped,
        "scope_gap": scope_gap,
        "constraint_gaps": constraint_gaps,
        "evidence_status": decision["status"],
        "abstain": decision["abstain"],
        "abstain_reason": decision["abstain_reason"],
    }

