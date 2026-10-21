"""Pilot document selection: rank high-value docs for LLM annotation.

Priority signals (descending importance, per P2 plan):
(a) 受众疑似误标：``audience == zjui_undergraduate`` 但 title/breadcrumb 含
    研究生/导师/博士后/教师/faculty/postdoc/graduate/PhD 关键词；
(b) series 成员（``series_id`` 非空）；
(c) ``n_table_rows > 0`` 且 ``doc_type`` 属于高价值结构化类型；
(d) ``doc_type == 'page'`` 且 ``n_blocks >= 10``。

Docs are ordered by (a) > (b) > (c) > (d) score descending, then ``n_blocks``
descending, then truncated to ``limit``.

:func:`stratified_sample_docs` is the P2 full-corpus complement: proportional
sampling over site × doc_type × has-table × series-membership strata, so field
quality numbers generalize beyond the pilot's deliberately biased selection.
"""

from __future__ import annotations

import random
import re
from pathlib import Path

from evidence.store import read_jsonl

__all__ = ["MISLABEL_KEYWORDS", "TABLE_DOC_TYPES", "select_pilot_docs", "stratified_sample_docs"]

MISLABEL_KEYWORDS = ("研究生", "导师", "博士后", "教师", "faculty", "postdoc", "graduate", "phd")
TABLE_DOC_TYPES = {
    "guide", "schedule", "calendar", "notice", "regulation", "exam_schedule", "program_plan",
}

STRATIFIED_SEED = 20261014


def _keyword_patterns() -> list[re.Pattern[str]]:
    """Chinese keywords match anywhere; ASCII keywords need word boundaries.

    A plain substring test would flag every "Undergraduate" title because
    "graduate" is a substring of it.
    """
    patterns = []
    for keyword in MISLABEL_KEYWORDS:
        if keyword.isascii():
            patterns.append(re.compile(rf"(?<![a-z0-9]){keyword}(?![a-z0-9])"))
        else:
            patterns.append(re.compile(re.escape(keyword)))
    return patterns


_PATTERNS = _keyword_patterns()


def _looks_mislabeled(doc: dict) -> bool:
    if doc.get("audience") != "zjui_undergraduate":
        return False
    haystack = " ".join(
        [str(doc.get("title") or "")] + [str(part) for part in (doc.get("breadcrumb") or [])]
    ).casefold()
    return any(pattern.search(haystack) for pattern in _PATTERNS)


def _pilot_score(doc: dict) -> tuple[int, int, int, int, int]:
    mislabel = 1 if _looks_mislabeled(doc) else 0
    in_series = 1 if doc.get("series_id") else 0
    table = 1 if doc.get("n_table_rows", 0) > 0 and doc.get("doc_type") in TABLE_DOC_TYPES else 0
    big_page = 1 if doc.get("doc_type") == "page" and doc.get("n_blocks", 0) >= 10 else 0
    return (mislabel, in_series, table, big_page, int(doc.get("n_blocks", 0) or 0))


def select_pilot_docs(build: Path, limit: int = 300) -> list[str]:
    """Return up to ``limit`` doc_uids ranked by pilot value (highest first).

    Only ``active`` documents are eligible — listing/shell pages are navigation
    shells whose blocks live in ``inactive_blocks.jsonl``, and annotating them
    both wastes calls and yields empty payloads.
    """
    docs = list(read_jsonl(Path(build) / "documents.jsonl"))
    ranked = sorted((d for d in docs if d.get("status") == "active"), key=_pilot_score, reverse=True)
    return [doc["doc_uid"] for doc in ranked[:limit]]


def _stratum_of(doc: dict) -> tuple[str, str, int, int]:
    return (
        str(doc.get("site") or ""),
        str(doc.get("doc_type") or ""),
        1 if doc.get("n_table_rows", 0) > 0 else 0,
        1 if doc.get("series_id") else 0,
    )


def stratified_sample_docs(build: Path, n: int, *, seed: int = STRATIFIED_SEED) -> tuple[list[str], dict[str, int]]:
    """Proportionally sample ``n`` active doc_uids over the P2 strata.

    Strata = site × doc_type × has-table × in-series.  Quotas follow the
    population share (largest remainder), every non-empty stratum keeps at
    least one doc when ``n`` allows, and members are drawn uniformly within a
    stratum (fixed seed → reproducible).  Returns ``(uids, stratum_counts)``
    so the sample's composition can be reported next to the population's.
    """
    import math

    docs = [d for d in read_jsonl(Path(build) / "documents.jsonl") if d.get("status") == "active"]
    strata: dict[tuple[str, str, int, int], list[dict]] = {}
    for doc in docs:
        strata.setdefault(_stratum_of(doc), []).append(doc)
    rng = random.Random(seed)
    for members in strata.values():
        rng.shuffle(members)

    keys = sorted(strata)
    if n >= len(docs):
        uids = [doc["doc_uid"] for key in keys for doc in strata[key]]
        return uids, {"/".join(map(str, key)): len(strata[key]) for key in keys}

    exact = {key: n * len(strata[key]) / len(docs) for key in keys}
    quotas = {key: math.floor(exact[key]) for key in keys}
    remainder = n - sum(quotas.values())
    for key in sorted(keys, key=lambda k: exact[k] - math.floor(exact[k]), reverse=True)[:remainder]:
        quotas[key] += 1
    # Guarantee one per non-empty stratum by borrowing from the largest quotas.
    for key in keys:
        if quotas.get(key, 0) == 0 and strata[key]:
            donor = max(keys, key=lambda k: quotas.get(k, 0))
            if quotas.get(donor, 0) > 1:
                quotas[donor] -= 1
                quotas[key] = 1
    uids: list[str] = []
    for key in keys:
        uids.extend(doc["doc_uid"] for doc in strata[key][: quotas.get(key, 0)])
    return uids, {"/".join(map(str, key)): quotas.get(key, 0) for key in keys}
