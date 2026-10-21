"""Anchor verification: does a claimed evidence span actually appear in the source text?

Used by the LLM annotation pilot as the anti-hallucination gate: every
high-risk field must cite ``evidence_spans`` that can be located, verbatim (after
normalization), inside the document's clean text.  A span that cannot be located
is treated as *unsupported* — no原文依据, regardless of how confident the model
claims to be (项目契约：LLM 只增结构不增事实).

P1 provenance contract additions:

* :func:`locate_spans` resolves each supported span to **structured anchors**
  (``block_id`` / ``char_start`` / ``char_end`` / ``page`` / ``table_id`` /
  ``row_index``), so an annotation points at *which* occurrence it means — not
  just "this sentence appears somewhere in the document".
* :func:`verify_summary` gates the one-sentence summary: index-only aid, but it
  must not invent numbers and must retain a verbatim anchor to the source.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable

__all__ = ["verify_spans", "locate_spans", "verify_summary", "norm_key"]

_DIGIT_RE = re.compile(r"\d+")
_WORD_RUN_RE = re.compile(r"\w+", re.UNICODE)


def norm_key(text: object) -> str:
    """Return an aggressive matching key: NFKC, all whitespace removed, casefolded.

    Whitespace-insensitive matching lets a span survive line wrapping, full/half
    width spaces and stray newlines between the source text and the quote.
    """
    value = unicodedata.normalize("NFKC", str(text if text is not None else ""))
    return "".join(ch for ch in value if not ch.isspace()).casefold()


def verify_spans(doc_text: str, spans: list[str]) -> dict:
    """Check each span against ``doc_text`` by normalized substring matching.

    Returns ``{"supported": [...], "unsupported": [...]}`` with the original
    span strings partitioned.  Empty/blank spans are always unsupported, and an
    empty ``spans`` list yields empty lists — callers must treat "no supported
    span" as *no evidence* (无原文依据), which is how :mod:`annotate.run_pilot`
    marks the field ``uncertain``.
    """
    haystack = norm_key(doc_text)
    supported: list[str] = []
    unsupported: list[str] = []
    for span in list(spans or []):
        needle = norm_key(span)
        if needle and needle in haystack:
            supported.append(span)
        else:
            unsupported.append(span)
    return {"supported": supported, "unsupported": unsupported}


def locate_spans(blocks: Iterable[dict[str, Any]], spans: list[str]) -> list[dict[str, Any]]:
    """Resolve supported spans to structured, block-level source anchors.

    ``blocks`` are records from ``blocks.jsonl`` (in reading order).  Each item
    of the result is ``{"text": <original span>, "anchors": [...],
    "match_count": N}``; an anchor carries ``block_id`` / ``ordinal`` /
    ``char_start`` / ``char_end`` (offsets are into the *normalized* block text,
    matching ``match_method``), plus ``page`` / ``table_id`` / ``row_index``
    when the block has them.  All matching blocks are recorded, so repeated
    template text stays auditable instead of silently pointing at one hit.
    """
    prepared = [(block.get("block_id"), block.get("ordinal"), norm_key(block.get("text") or ""), block) for block in blocks]
    prepared = [item for item in prepared if item[2]]
    resolved: list[dict[str, Any]] = []
    for span in list(spans or []):
        needle = norm_key(span)
        anchors: list[dict[str, Any]] = []
        if needle:
            for block_id, ordinal, hay, block in prepared:
                start = hay.find(needle)
                if start < 0:
                    continue
                table = block.get("table") or {}
                anchors.append({
                    "block_id": block_id,
                    "ordinal": ordinal,
                    "char_start": start,
                    "char_end": start + len(needle),
                    "page": block.get("page"),
                    "table_id": table.get("table_id"),
                    "row_index": table.get("row_index"),
                    "match_method": "norm_substring",
                })
        resolved.append({"text": span, "anchors": anchors, "match_count": len(anchors)})
    return resolved


def verify_summary(doc_text: str, summary: str) -> dict[str, Any]:
    """Gate a one-sentence summary: compression aid, never a fact source.

    A summary passes when (a) every digit run it uses also occurs in the source
    (a faithful paraphrase keeps its numbers; an invented date/credit does not)
    and (b) at least one 8+ character normalized word-run of the summary occurs
    verbatim in the source, so the summary demonstrably compresses this
    document rather than an imagined one.
    """
    text = str(summary or "").strip()
    if not text:
        return {"supported": False, "novel_digits": [], "verbatim_anchor": False}
    haystack = norm_key(doc_text)
    novel = sorted(set(_DIGIT_RE.findall(norm_key(text))) - set(_DIGIT_RE.findall(haystack)))
    anchor = any(len(run) >= 8 and norm_key(run) in haystack for run in _WORD_RUN_RE.findall(text))
    return {"supported": not novel and anchor, "novel_digits": novel, "verbatim_anchor": anchor}
