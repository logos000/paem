"""Shared evidence-store primitives: stable identifiers, normalization, anchors.

The corpus builder, benchmark generators and the online evidence layer all
refer to the same objects:

* a *document* is identified by ``doc_uid`` = ``d_`` + sha1(canonical source)
  and never changes when the corpus is rebuilt;
* a *block* (heading, paragraph, list item, table row) is identified by
  ``<doc_uid>:<sha1(normalized text)[:12]>`` so its id survives re-chunking,
  re-ordering and parser changes as long as its text does not change;
* an *anchor* is ``{doc_uid, quote}``.  Gold labels store anchors instead of
  row indexes or chunk ids, and :func:`resolve_anchor` maps them back to blocks
  after any rebuild.  This removes the drifting ``row_index`` gold of v1.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import difflib
import hashlib
import json
from pathlib import Path
import re
import unicodedata
from typing import Any, Iterable, Iterator

SCHEMA_VERSION = "evidence-store/1"

_SPACE_RE = re.compile(r"\s+")
_PUNCT_MAP = str.maketrans({
    "　": " ", "\xa0": " ", "​": "", "﻿": "", "‍": "", "‌": "",
    "“": '"', "”": '"', "‘": "'", "’": "'", "–": "-", "—": "-", "－": "-",
})


def normalize_text(value: Any) -> str:
    """Return display-safe text: NFKC, unified quotes/dashes, collapsed spaces."""
    text = unicodedata.normalize("NFKC", str(value or "")).translate(_PUNCT_MAP)
    return _SPACE_RE.sub(" ", text).strip()


def match_key(value: Any) -> str:
    """Return an aggressive key for matching: casefolded, no whitespace/punctuation."""
    text = normalize_text(value).casefold()
    return "".join(ch for ch in text if ch.isalnum())


def sha1(value: str | bytes) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha1(data).hexdigest()


def sha256(value: str | bytes) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def canonical_url(url: str) -> str:
    """Canonicalize a source URL so http/https and trailing slashes do not split ids."""
    value = str(url or "").strip()
    value = re.sub(r"^https?://", "", value, flags=re.I)
    value = value.split("#", 1)[0].rstrip("/")
    host, _, rest = value.partition("/")
    host = host.casefold()
    if host.startswith("www."):
        host = host[4:]
    return host + ("/" + rest if rest else "")


def doc_uid(source: str) -> str:
    """Stable document id from a URL (canonicalized) or a source-relative path."""
    key = canonical_url(source) if re.match(r"^https?://", str(source), re.I) else str(source).replace("\\", "/")
    return "d_" + sha1(key)[:16]


def block_id(doc: str, text: str, salt: int = 0) -> str:
    digest = sha1(match_key(text) + (f"#{salt}" if salt else ""))[:12]
    return f"{doc}:{digest}"


@dataclass
class Block:
    block_id: str
    doc_uid: str
    ordinal: int
    kind: str  # heading | paragraph | list_item | table_row | page_text
    text: str
    heading_path: list[str] = field(default_factory=list)
    page: int | None = None
    char_start: int = 0
    char_end: int = 0
    table: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        return {key: value for key, value in data.items() if value not in (None, [], {})}


@dataclass
class Anchor:
    doc_uid: str
    quote: str
    role: str = "support"  # support | counter | scope

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def read_jsonl(path: str | Path) -> Iterator[dict[str, Any]]:
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def write_jsonl(path: str | Path, rows: Iterable[dict[str, Any]]) -> int:
    count = 0
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=False) + "\n")
            count += 1
    return count


class BlockIndex:
    """In-memory lookup of blocks by document, used to resolve anchors."""

    def __init__(self, blocks: Iterable[dict[str, Any]]):
        self.by_doc: dict[str, list[dict[str, Any]]] = {}
        self.by_id: dict[str, dict[str, Any]] = {}
        for block in blocks:
            self.by_doc.setdefault(block["doc_uid"], []).append(block)
            self.by_id[block["block_id"]] = block
        for rows in self.by_doc.values():
            rows.sort(key=lambda row: row.get("ordinal", 0))
        self._keys: dict[str, list[str]] = {}

    @classmethod
    def load(cls, path: str | Path) -> "BlockIndex":
        return cls(read_jsonl(path))

    def keys(self, doc: str) -> list[str]:
        if doc not in self._keys:
            self._keys[doc] = [match_key(row["text"]) for row in self.by_doc.get(doc, [])]
        return self._keys[doc]


def resolve_anchor(index: BlockIndex, anchor: dict[str, Any] | Anchor, *, fuzzy_threshold: float = 0.86) -> dict[str, Any]:
    """Resolve ``{doc_uid, quote}`` to the block ids containing the quote.

    Matching order: exact normalized substring in one block, substring across
    consecutive blocks (for quotes spanning a list item and its continuation),
    then a difflib fuzzy fallback.  Returns ``{"status", "block_ids", "score"}``.
    """
    data = anchor.to_json() if isinstance(anchor, Anchor) else dict(anchor)
    doc = data.get("doc_uid", "")
    quote = match_key(data.get("quote", ""))
    rows = index.by_doc.get(doc, [])
    if not rows:
        return {"status": "missing_doc", "block_ids": [], "score": 0.0}
    if not quote:
        return {"status": "empty_quote", "block_ids": [], "score": 0.0}
    keys = index.keys(doc)
    exact = [rows[i]["block_id"] for i, key in enumerate(keys) if quote in key]
    if exact:
        return {"status": "exact", "block_ids": exact, "score": 1.0}
    for width in (2, 3, 4):
        for start in range(0, max(0, len(keys) - width + 1)):
            if quote in "".join(keys[start:start + width]):
                return {"status": "span", "block_ids": [rows[i]["block_id"] for i in range(start, start + width)], "score": 1.0}
    best_index, best_score = -1, 0.0
    for i, key in enumerate(keys):
        if not key:
            continue
        matcher = difflib.SequenceMatcher(None, quote, key, autojunk=False)
        if matcher.real_quick_ratio() < fuzzy_threshold * min(1.0, len(key) / max(1, len(quote))):
            continue
        block = matcher.find_longest_match(0, len(quote), 0, len(key))
        if block.size < max(4, len(quote) // 4):
            continue
        window = key[max(0, block.b - block.a): max(0, block.b - block.a) + len(quote)]
        score = difflib.SequenceMatcher(None, quote, window, autojunk=False).ratio()
        if score > best_score:
            best_index, best_score = i, score
    if best_index >= 0 and best_score >= fuzzy_threshold:
        return {"status": "fuzzy", "block_ids": [rows[best_index]["block_id"]], "score": round(best_score, 4)}
    return {"status": "unresolved", "block_ids": [], "score": round(best_score, 4)}
