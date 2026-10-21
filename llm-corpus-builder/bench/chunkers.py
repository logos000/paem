"""Baseline chunking strategies (P7.1): A0 fixed windows, A1 naive clean +
recursive split.

A0 ``raw_window``
    原始文本直接按固定长度窗口切（默认 500 字符、步长 450，即 50 字符重叠）。
A1 ``clean_recursive``
    常规清洗（空白归一、丢弃超短行、折叠页内连续重复行）+ 递归分隔符切块
    （``\\n\\n`` → ``\\n`` → 句号 → 空格），块长上限默认 512。

两个策略都只消费文本，不使用任何文档级元数据或结构信息。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from evidence.store import read_jsonl

__all__ = ["fixed_window", "recursive_split", "naive_clean", "build_baseline_units"]

_SEPARATORS = ("\n\n", "\n", "。", "！", "？", ". ", "；", "; ", "，", " ")
_MIN_LINE = 4


def fixed_window(text: str, size: int = 500, stride: int = 450) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    stride = max(1, min(stride, size))
    return [text[start:start + size] for start in range(0, len(text), stride) if text[start:start + size].strip()]


def _split_by(text: str, separator: str) -> list[str]:
    if separator == " ":
        return text.split(" ")
    return text.split(separator)


def recursive_split(text: str, max_size: int = 512, separators: tuple[str, ...] = _SEPARATORS) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_size:
        return [text]

    def split(text: str, level: int) -> list[str]:
        if len(text) <= max_size:
            return [text] if text.strip() else []
        if level >= len(separators):
            return [text[i:i + max_size] for i in range(0, len(text), max_size) if text[i:i + max_size].strip()]
        pieces = _split_by(text, separators[level])
        out: list[str] = []
        buffer = ""
        for piece in pieces:
            candidate = piece if not buffer else buffer + separators[level] + piece
            if len(candidate) <= max_size:
                buffer = candidate
                continue
            if buffer:
                out.append(buffer)
                buffer = ""
            if len(piece) <= max_size:
                out.append(piece)
            else:
                out.extend(split(piece, level + 1))
        if buffer:
            out.append(buffer)
        return [chunk for chunk in out if chunk.strip()]

    return split(text, 0)


def naive_clean(text: str) -> str:
    """常规清洗：空白归一、丢弃超短行、折叠连续重复行、去页眉页脚式纯符号行。"""
    lines: list[str] = []
    previous = None
    for raw in text.splitlines():
        line = re.sub(r"[ \t\u3000]+", " ", raw).strip()
        if not line or len(line) < _MIN_LINE:
            continue
        if re.fullmatch(r"[-=_*·•—–\s]+|\d{1,3}", line):  # separators / bare page numbers
            continue
        if line == previous:  # consecutive duplicates collapse
            continue
        previous = line
        lines.append(line)
    return "\n".join(lines)


def _resolve_raw(build_dir: Path, rel: str) -> Path:
    """Map a build rel_path back to the crawled raw file.

    Page rel_paths are site-relative ("zjui/_raw/<host>/..." under
    ``zju-sites/archive``, "sharepoint/_page-content-raw/..." under
    ``ZJUIntl/sites/ZJUIUG``); attachment rel_paths ("ZJUIntl/...") resolve
    against the crawl root directly.
    """
    import json

    crawl = Path(json.loads((Path(build_dir) / "manifest.json").read_text(encoding="utf-8")).get("crawl_root", "."))
    candidates = []
    if rel.startswith("sharepoint/"):
        candidates.append(crawl / "ZJUIntl" / "sites" / "ZJUIUG" / rel[len("sharepoint/"):])
    from ingest.sources import ARCHIVE_SITES

    first, _, _rest = rel.partition("/")
    if first in ARCHIVE_SITES:
        candidates.append(crawl / "zju-sites" / "archive" / rel)
    candidates.append(crawl / rel)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[-1]


def build_baseline_units(build_dir, strategy: str, *, window_size: int = 500, window_stride: int = 450,
                         chunk_size: int = 512, raw_dump=None, html_dump=None) -> list[dict[str, Any]]:
    """Construct a baseline corpus from a build directory.

    Pages are read from the crawled raw HTML and flattened with a naive soup
    (navigation shell included — that is what a standard pipeline starts from);
    attachments are plain-dumped (no structure recovery).  Exact-duplicate and
    rejected attachments are excluded via the build's own dedup — the one
    baseline-friendly concession, recorded in the benchmark manifest.  The
    result units are ``{"doc_uid", "block_id", "text"}`` dicts compatible with
    ``retrieve.lexical.Unit`` and the eval harness's quote matching.
    """
    build_dir = Path(build_dir)
    from bench.rawdump import html_plain, plain_text

    html_dump = html_dump or html_plain
    texts: list[tuple[str, str]] = []  # (doc_uid, text)
    for doc in read_jsonl(build_dir / "documents.jsonl"):
        if doc.get("status") != "active":
            continue
        rel = doc.get("rel_path") or ""
        if not rel:
            continue
        source = _resolve_raw(build_dir, rel)
        if not source.is_file():
            continue
        if doc.get("kind") == "page":
            dumped = html_dump(source.read_bytes())
        else:
            dumped = plain_text(source)
        if dumped.strip():
            texts.append((doc["doc_uid"], dumped))

    units: list[dict[str, Any]] = []
    for doc_uid, text in texts:
        prepared = text if strategy == "raw_window" else naive_clean(text)
        chunks = fixed_window(prepared, window_size, window_stride) if strategy == "raw_window" else recursive_split(prepared, chunk_size)
        for index, chunk in enumerate(chunks, 1):
            units.append({"doc_uid": doc_uid, "block_id": f"{doc_uid}#b{index:04d}", "text": chunk})
    return units
