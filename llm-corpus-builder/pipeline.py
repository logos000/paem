"""Build a versioned evidence corpus from the crawled official materials.

Usage::

    .venv/Scripts/python.exe -m pipeline \
        --crawl-root "E:/Projects/浙大网站爬取" --out data/corpus [--convert-legacy] [--ocr]

Output (``data/corpus/<build_id>/``, gitignored — real documents never enter git):

* ``documents.jsonl``  one record per document (pages and attachments) with
  provenance, metadata, dedup status and version-chain fields;
* ``blocks.jsonl``     content-addressed evidence blocks of *active* documents;
* ``series.json``      version chains; ``dedup.json`` duplicate groups;
* ``rejected.jsonl``   files that could not become evidence and why;
* ``baseline_pages.jsonl``  the crawler's raw ``_text`` for the before/after study;
* ``manifest.json`` and ``REPORT.md``.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from curate.dedup import duplicate_groups  # noqa: E402
from structure.extract import Extraction, RawBlock, extract_file  # noqa: E402
from ingest.legacy import LegacyConverter  # noqa: E402
from curate.metadata import SITE_DEFAULTS, build_metadata  # noqa: E402
from ingest.sources import DOC_EXTS, SourceItem, iter_cms_orphans, iter_gfxwj, iter_gfxwj_files, iter_sharepoint, iter_sharepoint_files, parse_cms_page  # noqa: E402
from curate.versions import build_series  # noqa: E402
from evidence.store import SCHEMA_VERSION, block_id, canonical_url, doc_uid, match_key, normalize_text, read_jsonl, sha256, write_jsonl  # noqa: E402

DEFAULT_SITES = {
    "sharepoint": (True, True),
    "gfxwj": (True, True),
    "zjui": (True, True),
    "ugrs": (True, True),
    "grs": (True, False),
    "zju": (True, False),
    "news": (True, False),
    "libweb": (True, False),
}
LEGACY_EXTS = {".doc", ".xls", ".wps", ".et"}
GENERIC_LABEL_RE = re.compile(r"^(?:附件\s*\d*[:：.]?|下载|点击下载|点击此处|点击这里|这里|此处|click\s+here|here|download|link|pdf|文件|全文|详见附件)$", re.I)
UUIDISH_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$|^[0-9a-f]{16,}$|^\d{10,}$", re.I)


# ---------------------------------------------------------------- workers


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _parse_cms_row(args: tuple[str, str, dict[str, Any]]) -> SourceItem | None:
    archive, site, row = args
    site_dir = Path(archive) / site
    rel = row.get("rel", "").replace("\\", "/")
    path = site_dir / "_raw" / rel
    if not path.is_file():
        return None
    try:
        item = parse_cms_page(site, site_dir, row["url"], _decode(path.read_bytes()), row.get("title", ""))
    except Exception as exc:  # pragma: no cover - keep the build going
        item = SourceItem(site=site, kind="page", source_url=row["url"], local_path=path, rel_path="", title=row.get("title", ""))
        item.warnings.append(f"parse_error:{type(exc).__name__}")
    item.local_path = path
    item.rel_path = f"{site}/_raw/{rel}"
    text_path = site_dir / "_text" / rel
    item.raw_text_path = text_path if text_path.is_file() else None
    return item


def _quiet_pymupdf() -> None:
    import warnings
    try:
        import pymupdf
        pymupdf.TOOLS.mupdf_display_errors(False)
        pymupdf.TOOLS.mupdf_display_warnings(False)
        pymupdf.no_recommend_layout()
    except Exception:
        pass
    warnings.filterwarnings("ignore", module="openpyxl")


def _mineru_sidecar_fallback(path: Path) -> Path | None:
    """MinerU sidecar next to the current file path (non-snapshot mode)."""
    candidate = Path(str(path) + ".mineru.md")
    return candidate if candidate.is_file() and candidate.stat().st_size > 50 else None


def _mineru_blocks(md_text: str) -> list[RawBlock]:
    blocks: list[RawBlock] = []
    table_no = 0
    for line in md_text.splitlines():
        line = line.strip()
        if not line or line.startswith("![") or line.startswith("<!--"):
            continue
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if cells and set("".join(cells)) <= set("-: "):
                continue  # markdown table separator row
            blocks.append(RawBlock("table_row", normalize_text(" | ".join(cells)), table=f"m{table_no}"))
            continue
        if line.startswith("#"):
            level = min(len(line) - len(line.lstrip("#")), 4)
            text = normalize_text(line.lstrip("# "))
            if text:
                blocks.append(RawBlock("heading", text, level=level))
            continue
        text = normalize_text(line)
        if text:
            blocks.append(RawBlock("paragraph", text))
    # 表号：按连续管道段编号（sidecar markdown 无法感知表格边界）
    table_no = 0
    prev_row = False
    for b in blocks:
        if b.kind == "table_row":
            if not prev_row:
                table_no += 1
            b.table = f"m{table_no - 1}"
            prev_row = True
        else:
            prev_row = False
    return blocks


def _extract_cached(args: tuple) -> dict[str, Any]:
    path, sha, cache_dir, ocr = args[:4]
    sidecar = Path(args[4]) if len(args) > 4 and args[4] else None
    if sidecar is None or not sidecar.is_file() or sidecar.stat().st_size <= 50:
        sidecar = _mineru_sidecar_fallback(Path(path))
    cache = Path(cache_dir) / f"{sha}{'.ocr' if ocr else ''}{'.mineru' if sidecar is not None else ''}.json"
    if cache.is_file():
        return json.loads(cache.read_text(encoding="utf-8"))
    if sidecar is not None:
        started = time.perf_counter()
        blocks = _mineru_blocks(sidecar.read_text(encoding="utf-8", errors="replace"))
        data = {"ok": True, "blocks": [asdict(b) for b in blocks], "warnings": ["mineru_sidecar"],
                "pages": 0, "needs_ocr_pages": [], "ocr_attempted_pages": [], "ocr_succeeded_pages": [],
                "ocr_failed_pages": [], "ocr_dependency_missing_pages": []}
        data["seconds"] = round(time.perf_counter() - started, 3)
        cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data
    _quiet_pymupdf()
    started = time.perf_counter()
    try:
        result = extract_file(Path(path), ocr=ocr)
        data = {"ok": True, "blocks": [asdict(b) for b in result.blocks], "warnings": result.warnings, "pages": result.pages, "needs_ocr_pages": result.needs_ocr_pages,
                "ocr_attempted_pages": result.ocr_attempted_pages, "ocr_succeeded_pages": result.ocr_succeeded_pages,
                "ocr_failed_pages": result.ocr_failed_pages, "ocr_dependency_missing_pages": result.ocr_dependency_missing_pages}
    except Exception as exc:
        data = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    data["seconds"] = round(time.perf_counter() - started, 3)
    cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------- assembly


def assemble_blocks(uid: str, title: str, raw: list[RawBlock]) -> tuple[list[dict[str, Any]], str]:
    """Attach heading paths, offsets and content-addressed ids to raw blocks."""
    blocks: list[dict[str, Any]] = []
    stack: list[tuple[int, str]] = []
    seen: Counter[str] = Counter()
    offset = 0
    parts: list[str] = []
    for block in raw:
        text = normalize_text(block.text)
        if not text or not match_key(text):
            continue
        if block.kind == "heading":
            level = block.level or 2
            while stack and stack[-1][0] >= level:
                stack.pop()
            path = [h for _, h in stack]
            stack.append((level, text[:120]))
        else:
            path = [h for _, h in stack]
        key = match_key(text)
        salt = seen[key]
        seen[key] += 1
        record: dict[str, Any] = {
            "block_id": block_id(uid, text, salt),
            "doc_uid": uid,
            "ordinal": len(blocks),
            "kind": block.kind,
            "text": text,
            "heading_path": path,
            "char_start": offset,
            "char_end": offset + len(text),
        }
        if block.page is not None:
            record["page"] = block.page
        if block.table:
            record["table"] = block.table
        blocks.append(record)
        parts.append(text)
        offset += len(text) + 1
    return blocks, "\n".join(parts)


def content_chars(blocks: list[dict[str, Any]]) -> int:
    """Characters of actual content: table rows count their cells, not the
    header names repeated into every row for self-contained evidence."""
    total = 0
    for block in blocks:
        cells = (block.get("table") or {}).get("cells")
        total += sum(len(c) for c in dict.fromkeys(cells) if c) if cells else len(block["text"])
    return total


def attachment_title(label: str, path: Path, parent_title: str, embedded_count: int) -> tuple[str, str]:
    label = normalize_text(label)
    stem = normalize_text(path.stem)
    label_stem = re.sub(r"\.(?:pdf|docx?|xlsx?|wps|txt)$", "", label, flags=re.I).strip()
    if label_stem and not GENERIC_LABEL_RE.match(label_stem) and not UUIDISH_RE.match(label_stem):
        return label_stem, "link_label"
    if stem and not UUIDISH_RE.match(stem):
        return stem, "filename"
    if parent_title:
        if embedded_count == 1:
            return parent_title, "parent_title"
        return f"{parent_title}（附件{(' ' + label_stem) if label_stem else ''}）", "parent_title+label"
    return label_stem or stem, "filename"


INACTIVE_INDEXABLE = ("duplicate", "listing", "home", "shell")


def choose_canonical(docs: list[dict[str, Any]]) -> dict[str, Any]:
    tier_rank = {"core": 0, "support": 1, "distractor": 2}
    kind_rank = {"article": 0, "shell": 1, "listing": 2, "home": 3}
    return min(docs, key=lambda d: (
        tier_rank.get(d.get("tier", ""), 3),
        kind_rank.get(d.get("page_kind", "article"), 4),
        # Rendered text loses tables; keep the copy parsed from page HTML.
        "rendered_text_fallback" in d.get("warnings", []),
        "/node/" in d["source_url"],
        "/en/" in d["source_url"],
        -int(d.get("authority_rank", 0)),
        len(d["source_url"]),
        d["source_url"],
    ))


# ---------------------------------------------------------------- build stages


def build(args: argparse.Namespace) -> Path:
    """Build orchestrator: the stages below run in this order.

    collect_pages -> collect_attachments -> apply_snapshot -> extract_attachments
    -> assemble_documents -> dedup_documents -> build_version_series
    -> triage_blocks -> write_build
    """
    started = time.perf_counter()
    crawl = Path(args.crawl_root)
    out_root = Path(args.out)
    cache = out_root / "_cache"
    (cache / "extract").mkdir(parents=True, exist_ok=True)
    sites = _selected_sites(args)
    workers = args.workers or max(1, (os.cpu_count() or 4) - 2)
    log = lambda message: print(f"[{time.perf_counter() - started:7.1f}s] {message}", flush=True)  # noqa: E731

    pages = collect_pages(args, crawl, sites, workers, log)
    attachments, rejected = collect_attachments(pages, crawl, sites, log)
    pages, snapshot = apply_snapshot(args, crawl, pages, attachments, rejected, log)
    results = extract_attachments(args, attachments, cache, workers, log)
    documents, blocks_by_doc, text_by_doc, baseline = assemble_documents(pages, attachments, results, snapshot, rejected, log)
    dedup_records = dedup_documents(documents, text_by_doc, log)
    series = build_version_series(documents, log)
    demoted_blocks = triage_blocks(documents, blocks_by_doc, log)
    return write_build(args, out_root, crawl, sites, snapshot, documents, blocks_by_doc, baseline,
                       rejected, series, dedup_records, demoted_blocks, started, log)


def _selected_sites(args: argparse.Namespace) -> dict[str, tuple[bool, bool]]:
    sites = {name: DEFAULT_SITES[name] for name in (args.sites.split(",") if args.sites else DEFAULT_SITES)}
    if args.grs_attachments and "grs" in sites:
        sites["grs"] = (True, True)
    return sites


def collect_pages(args: argparse.Namespace, crawl: Path, sites: dict[str, tuple[bool, bool]],
                  workers: int, log: Any) -> list[SourceItem]:
    """Stage 1 — source collection: SharePoint tree + CMS pages from the archive index."""
    archive = crawl / "zju-sites" / "archive"
    sharepoint = crawl / "ZJUIntl"
    pages: list[SourceItem] = []
    if "sharepoint" in sites and sharepoint.is_dir():
        pages += list(iter_sharepoint(sharepoint))
        log(f"sharepoint pages: {len(pages)}")
    if "gfxwj" in sites and (archive / "gfxwj").is_dir():
        before = len(pages)
        pages += list(iter_gfxwj(crawl))
        log(f"gfxwj pages: {len(pages) - before}")
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for site in sites:
            if site == "sharepoint":
                continue
            index = archive / site / "_index.jsonl"
            if not index.is_file():
                log(f"skip {site}: no index")
                continue
            rows = [json.loads(line) for line in index.read_text(encoding="utf-8").splitlines() if line.strip()]
            if args.limit:
                rows = rows[: args.limit]
            items = [item for item in pool.map(_parse_cms_row, [(str(archive), site, row) for row in rows], chunksize=16) if item]
            pages += items
            log(f"{site} pages: {len(items)}")
    return pages

def collect_attachments(pages: list[SourceItem], crawl: Path, sites: dict[str, tuple[bool, bool]],
                        log: Any) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Stage 2 — attachment registry: dedupe files by content hash, remember
    every parent link, register page links / SharePoint files / CMS orphans."""
    archive = crawl / "zju-sites" / "archive"
    sharepoint = crawl / "ZJUIntl"
    rejected: list[dict[str, Any]] = []
    attachments: dict[str, dict[str, Any]] = {}
    linked_paths: set[Path] = set()

    def register(path: Path, site: str, url: str, label: str = "", parent: SourceItem | None = None, embedded: bool = False, modified: str = "", rel: str = "") -> None:
        sha = file_sha256(path)
        linked_paths.add(path.resolve())
        entry = attachments.setdefault(sha, {"sha": sha, "path": path, "site": site, "urls": [], "labels": [], "parents": [], "embedded": False, "modified": modified, "rel": rel})
        if url not in entry["urls"]:
            entry["urls"].append(url)
        if label and label not in entry["labels"]:
            entry["labels"].append(label)
        if parent is not None and parent.source_url not in [p.source_url for p in entry["parents"]]:
            entry["parents"].append(parent)
        entry["embedded"] = entry["embedded"] or embedded
        entry["modified"] = entry["modified"] or modified

    for page in pages:
        if not sites.get(page.site, (True, False))[1]:
            continue
        for link in page.attachments:
            ext = Path(link.url.split("?")[0]).suffix.lower()
            if ext not in DOC_EXTS:
                rejected.append({"source_url": link.url, "site": page.site, "parent_url": page.source_url, "reason": f"unsupported_type:{ext}"})
                continue
            if link.local_path is None:
                rejected.append({"source_url": link.url, "site": page.site, "parent_url": page.source_url, "reason": "not_downloaded", "label": link.label})
                continue
            rel = link.local_path.relative_to(crawl).as_posix() if link.local_path.is_relative_to(crawl) else str(link.local_path)
            register(link.local_path, page.site, link.url, link.label, page, link.embedded, rel=rel)
    if "sharepoint" in sites and sharepoint.is_dir():
        for item in iter_sharepoint_files(sharepoint):
            register(item.local_path, "sharepoint", item.source_url, modified=item.modified, rel=item.rel_path)
    if "gfxwj" in sites and (archive / "gfxwj").is_dir():
        for item in iter_gfxwj_files(crawl):
            register(item.local_path, "gfxwj", item.source_url, label=item.title, modified=item.modified, rel=item.rel_path)
    for site, (_, with_attachments) in sites.items():
        if site == "sharepoint" or not with_attachments or not (archive / site).is_dir():
            continue
        for item in iter_cms_orphans(archive, site, linked_paths):
            register(item.local_path, site, item.source_url, rel=item.rel_path)
    log(f"attachments: {len(attachments)} unique files, {len(rejected)} rejected links")
    return attachments, rejected


def apply_snapshot(args: argparse.Namespace, crawl: Path, pages: list[SourceItem],
                   attachments: dict[str, dict[str, Any]], rejected: list[dict[str, Any]],
                   log: Any) -> tuple[list[SourceItem], Any]:
    """Stage 3 — strict snapshot intake: verify every byte against the
    immutable object store, then redirect all reads to it."""
    if not getattr(args, "snapshot", ""):
        return pages, None
    from ingest.snapshot import SnapshotStore, apply_snapshot_gate

    snapshot = SnapshotStore.load(Path(args.snapshot))
    log(f"snapshot: {snapshot.snapshot_id} ({len(snapshot)} records)")
    pages, _dropped_pages, _dropped_attachments = apply_snapshot_gate(snapshot, pages, attachments, rejected, crawl, log=log)
    return pages, snapshot


def extract_attachments(args: argparse.Namespace, attachments: dict[str, dict[str, Any]],
                        cache: Path, workers: int, log: Any) -> dict[str, Any]:
    """Stage 4 — legacy COM conversion (optional, cached) + cached structure
    extraction in a process pool."""
    converter = LegacyConverter(cache / "legacy") if args.convert_legacy else None
    extract_jobs: list[tuple[str, str, str, bool]] = []
    for sha, entry in attachments.items():
        path: Path = entry["path"]
        if path.suffix.lower() in LEGACY_EXTS:
            if converter is None:
                entry["error"] = "legacy_format_not_converted"
                continue
            try:
                converted = converter.convert(path, sha)
            except Exception as exc:
                converted = None
                entry["error"] = f"legacy_conversion_failed:{type(exc).__name__}"
            if converted is None:
                entry.setdefault("error", "legacy_converter_unavailable")
                continue
            entry["converted"] = converted
            path = converted
        # 严格摄入把 path 重定向到对象库；sidecar 用 gate 保留的原始爬取树路径找。
        origin = Path(entry.get("source_name") or path)
        sidecar = Path(str(origin) + ".mineru.md")
        extract_jobs.append((str(path), sha, str(cache / "extract"), bool(args.ocr and SITE_DEFAULTS.get(entry["site"], {}).get("tier") == "core"),
                             str(sidecar) if sidecar.is_file() and sidecar.stat().st_size > 50 else ""))
    if converter is not None:
        converter.close()
        log(f"legacy converted: {sum(1 for e in attachments.values() if e.get('converted'))}")
    with ProcessPoolExecutor(max_workers=workers) as pool:
        results = dict(zip((job[1] for job in extract_jobs), pool.map(_extract_cached, extract_jobs, chunksize=2)))
    log(f"extracted: {len(results)}")
    return results


def assemble_documents(pages: list[SourceItem], attachments: dict[str, dict[str, Any]], results: dict[str, Any],
                       snapshot: Any, rejected: list[dict[str, Any]], log: Any) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], dict[str, str], list[dict[str, Any]]]:
    """Stage 5 — pages and attachments become documents with metadata and
    evidence blocks (appends no-text/OCR-failed attachments to ``rejected``)."""
    documents: list[dict[str, Any]] = []
    blocks_by_doc: dict[str, list[dict[str, Any]]] = {}
    text_by_doc: dict[str, str] = {}
    baseline: list[dict[str, Any]] = []
    for page in pages:
        uid = doc_uid(page.source_url)
        blocks, text = assemble_blocks(uid, page.title, page.blocks)
        meta = build_metadata(site=page.site, kind="page", title=page.title, body=text, breadcrumb=page.breadcrumb, published=page.published, url=page.source_url)
        raw_bytes = page.local_path.read_bytes() if page.local_path and page.local_path.is_file() else b""
        status = "active"
        if page.page_kind in ("listing", "home"):
            status = page.page_kind
            # P1-7: listing pages carry pseudo-terms from nav/link text; they
            # never count as corpus evidence for a term.
            meta["terms"] = []
            meta["academic_years"] = []
        elif len(text) < 40:
            status = "shell" if page.attachments else "empty"
        doc = {
            "doc_uid": uid, "kind": "page", "site": page.site, "source_url": page.source_url, "alias_urls": [],
            "rel_path": page.rel_path, "title": page.title, "title_source": page.title_source,
            "published": page.published, "modified": page.modified, "breadcrumb": page.breadcrumb,
            "date_provenance": page.date_provenance,
            "retrieved_at": (snapshot.fetched_at(page.rel_path) if snapshot is not None else (datetime.fromtimestamp(page.local_path.stat().st_mtime).strftime("%Y-%m-%d") if page.local_path and page.local_path.is_file() else "")),
            "page_kind": page.page_kind, "status": status, "duplicate_of": "",
            "parent_uids": [], "child_uids": [], "raw_sha256": sha256(raw_bytes) if raw_bytes else "",
            "text_sha256": sha256(match_key(text)), "chars": len(text), "content_chars": content_chars(blocks), "n_blocks": len(blocks),
            "n_table_rows": sum(1 for b in blocks if b["kind"] == "table_row"),
            "warnings": page.warnings, **meta,
        }
        documents.append(doc)
        blocks_by_doc[uid] = blocks
        text_by_doc[uid] = text
        if page.raw_text_path is not None:
            baseline.append({"doc_uid": uid, "site": page.site, "source_url": page.source_url, "title": page.title, "text": _decode(page.raw_text_path.read_bytes())})
    doc_by_url = {canonical_url(d["source_url"]): d for d in documents}

    for sha, entry in attachments.items():
        url = entry["urls"][0]
        uid = doc_uid(url)
        parents = entry["parents"]
        parent = parents[0] if parents else None
        embedded_count = sum(1 for link in parent.attachments if link.embedded) if parent else 0
        title, title_source = attachment_title(entry["labels"][0] if entry["labels"] else "", entry.get("source_name") or entry["path"], parent.title if parent else "", embedded_count if entry["embedded"] else 2)
        result = results.get(sha)
        raw_blocks = [RawBlock(**b) for b in result["blocks"]] if result and result.get("ok") else []
        if title_source == "filename" and UUIDISH_RE.match(title):
            first = next((b.text for b in raw_blocks[:8] if b.kind in ("heading", "paragraph") and 4 <= len(normalize_text(b.text)) <= 80
                          and not re.fullmatch(r"(?:sheet|工作表)\s*\d*", normalize_text(b.text), re.I)), "")
            if first:
                title, title_source = normalize_text(first), "first_block"
        blocks, text = assemble_blocks(uid, title, raw_blocks)
        published = (parent.published if parent else "") or (entry["modified"] or "")[:10]
        breadcrumb = parent.breadcrumb if parent else []
        meta = build_metadata(site=entry["site"], kind="attachment", title=title, body=text, breadcrumb=breadcrumb, published=published, url=url)
        if parent is not None and not meta["terms"] and not meta["academic_years"]:
            parent_meta = doc_by_url.get(canonical_url(parent.source_url), {})
            meta["terms"], meta["academic_years"] = parent_meta.get("terms", []), parent_meta.get("academic_years", [])
            meta["term_source"] = "parent" if meta["terms"] or meta["academic_years"] else ""
        if parent is not None:
            parent_doc = doc_by_url.get(canonical_url(parent.source_url))
            if parent_doc and parent_doc["audience"] in ("faculty", "graduate") and meta["audience"] not in ("faculty", "graduate"):
                meta["audience"] = parent_doc["audience"]
                meta["tier"] = parent_doc["tier"]
        error = entry.get("error") or ("" if result is None or result.get("ok") else result.get("error", "extract_failed"))
        needs_ocr = (result or {}).get("needs_ocr_pages", [])
        ocr_dep_pages = (result or {}).get("ocr_dependency_missing_pages", [])
        ocr_fail_pages = (result or {}).get("ocr_failed_pages", [])
        status = "active" if blocks else ("rejected" if error else "empty")
        if status != "active":
            rejected.append({"source_url": url, "site": entry["site"], "rel_path": entry["rel"] or str(entry["path"]), "title": title,
                             "reason": error or ("ocr_dependency_missing" if ocr_dep_pages else "ocr_failed" if ocr_fail_pages else "needs_ocr" if needs_ocr else "no_text"),
                             "pages": (result or {}).get("pages", 0)})
        doc = {
            "doc_uid": uid, "kind": "attachment", "site": entry["site"], "source_url": url, "alias_urls": entry["urls"][1:],
            "rel_path": entry["rel"] or (entry.get("source_name") or entry["path"]).as_posix(), "title": title, "title_source": title_source,
            "link_labels": entry["labels"], "published": published, "modified": entry["modified"], "breadcrumb": breadcrumb,
            "date_provenance": (("modified_fallback" if not (parent and parent.published) else (parent.date_provenance if parent else "")) if published else ""),
            "retrieved_at": (snapshot.fetched_at(entry["rel"]) if snapshot is not None and entry.get("rel") else (datetime.fromtimestamp(entry["path"].stat().st_mtime).strftime("%Y-%m-%d") if entry.get("path") else "")),
            "page_kind": "file", "file_ext": Path(entry.get("source_name") or entry["path"]).suffix.lower(), "converted_from_legacy": bool(entry.get("converted")),
            "status": status, "duplicate_of": "", "parent_uids": [], "child_uids": [],
            "raw_sha256": sha, "text_sha256": sha256(match_key(text)), "chars": len(text), "n_blocks": len(blocks),
            "n_table_rows": sum(1 for b in blocks if b["kind"] == "table_row"),
            "pages": (result or {}).get("pages", 0), "needs_ocr_pages": needs_ocr[:50],
            "n_ocr_attempted": len((result or {}).get("ocr_attempted_pages", [])),
            "n_ocr_succeeded": len((result or {}).get("ocr_succeeded_pages", [])),
            "n_ocr_failed": len(ocr_fail_pages), "n_ocr_dependency_missing": len(ocr_dep_pages),
            "warnings": (result or {}).get("warnings", [])[:20], **meta,
        }
        for parent_item in parents:
            parent_doc = doc_by_url.get(canonical_url(parent_item.source_url))
            if parent_doc:
                doc["parent_uids"].append(parent_doc["doc_uid"])
                parent_doc["child_uids"].append(uid)
        documents.append(doc)
        blocks_by_doc[uid] = blocks
        text_by_doc[uid] = text
    log(f"documents: {len(documents)}")
    return documents, blocks_by_doc, text_by_doc, baseline


def dedup_documents(documents: list[dict[str, Any]], text_by_doc: dict[str, str], log: Any) -> list[dict[str, Any]]:
    """Stage 6 — cross-document duplicate groups; the canonical member keeps
    evidence status, the rest are demoted and donate alias urls and parents."""
    candidates = [d for d in documents if d["status"] in ("active", "shell")]
    groups = duplicate_groups(candidates, text_of=lambda d: text_by_doc[d["doc_uid"]])
    by_uid = {d["doc_uid"]: d for d in documents}
    dedup_records = []
    for group in groups:
        members = [by_uid[uid] for uid in group["members"]]
        canonical = choose_canonical(members)
        for member in members:
            if member is canonical:
                continue
            member["status"] = "duplicate"
            member["duplicate_of"] = canonical["doc_uid"]
            for url in [member["source_url"], *member["alias_urls"]]:
                if url not in canonical["alias_urls"] and url != canonical["source_url"]:
                    canonical["alias_urls"].append(url)
            for parent in member["parent_uids"]:
                if parent not in canonical["parent_uids"]:
                    canonical["parent_uids"].append(parent)
        dedup_records.append({"canonical": canonical["doc_uid"], "kind": group["kind"], "max_distance": group["max_distance"],
                              "members": [{"doc_uid": m["doc_uid"], "site": m["site"], "url": m["source_url"], "title": m["title"]} for m in members]})
    log(f"duplicate groups: {len(groups)}")
    return dedup_records


def build_version_series(documents: list[dict[str, Any]], log: Any) -> list[dict[str, Any]]:
    """Stage 7 — version chains (same document family, latest/superseded)."""
    series = build_series(documents)
    log(f"series: {len(series)}")
    return series


def triage_blocks(documents: list[dict[str, Any]], blocks_by_doc: dict[str, list[dict[str, Any]]], log: Any) -> list[dict[str, Any]]:
    """Stage 8 — P1-2 block-level redundancy triage (three-way, docs/P1-DETERMINISTIC-FIXES.md).

    (1) intra-doc exact duplicates — keep the first occurrence, demote the rest;
    (2) cross-doc template boilerplate — identical NON-table_row blocks in
    >= TEMPLATE_DOC_MIN active docs (nav menus, institute labels, exchange
    boilerplate) are demoted in every doc;
    (3) identical table_row blocks across docs are legitimate shared reference
    facts (course rows cited by several official docs) and are KEPT.
    Demoted blocks move to inactive_blocks.jsonl with an ``inactive_reason``:
    still in the ``clean`` ablation tier, gone from ``active``.
    """
    TEMPLATE_DOC_MIN = 5

    def _block_key(text: str, kind: str) -> tuple[str, str]:
        return ("".join((text or "").split()), kind)

    template_cross_docs: dict[tuple[str, str], set[str]] = {}
    for d in documents:
        if d["status"] != "active":
            continue
        for b in blocks_by_doc.get(d["doc_uid"], []):
            if b["kind"] != "table_row" and len(b["text"].strip()) >= 4:
                template_cross_docs.setdefault(_block_key(b["text"], b["kind"]), set()).add(d["doc_uid"])
    template_keys = {key for key, uids in template_cross_docs.items() if len(uids) >= TEMPLATE_DOC_MIN}

    demoted_blocks: list[dict] = []
    demoted_counts = {"intra_doc_duplicate": 0, "template_boilerplate": 0}
    for d in documents:
        if d["status"] != "active":
            continue
        kept, seen_in_doc = [], set()
        for b in blocks_by_doc.get(d["doc_uid"], []):
            reason = None
            if _block_key(b["text"], b["kind"]) in template_keys:
                reason = "template_boilerplate"
            elif _block_key(b["text"], b["kind"]) in seen_in_doc:
                reason = "intra_doc_duplicate"
            if reason:
                demoted_blocks.append({**b, "inactive_reason": reason})
                demoted_counts[reason] += 1
            else:
                seen_in_doc.add(_block_key(b["text"], b["kind"]))
                kept.append(b)
        blocks_by_doc[d["doc_uid"]] = kept
    log(f"block triage: {demoted_counts['template_boilerplate']} template blocks, "
        f"{demoted_counts['intra_doc_duplicate']} intra-doc duplicates demoted")
    return demoted_blocks


def write_build(args: argparse.Namespace, out_root: Path, crawl: Path, sites: dict[str, tuple[bool, bool]], snapshot: Any,
                documents: list[dict[str, Any]], blocks_by_doc: dict[str, list[dict[str, Any]]],
                baseline: list[dict[str, Any]], rejected: list[dict[str, Any]], series: list[dict[str, Any]],
                dedup_records: list[dict[str, Any]], demoted_blocks: list[dict[str, Any]],
                started: float, log: Any) -> Path:
    """Stage 9 — write the immutable build dir (documents/blocks/series/dedup/
    rejected/baseline + manifest + REPORT) and repoint the LATEST marker."""
    config = {"sites": {k: {"pages": v[0], "attachments": v[1]} for k, v in sites.items()}, "convert_legacy": bool(args.convert_legacy), "ocr": bool(args.ocr), "limit": args.limit}
    if snapshot is not None:
        config["snapshot"] = snapshot.summary()
    module_files = sorted(path for folder in ("evidence", "ingest", "structure", "curate", "retrieve") for path in (ROOT / folder).glob("*.py")) + [ROOT / "pipeline.py"]
    code_hash = sha256("".join(Path(p).read_text(encoding="utf-8") for p in module_files))[:12]
    build_id = args.build_id or datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + code_hash[:6]
    out = out_root / build_id
    out.mkdir(parents=True, exist_ok=True)
    active = {d["doc_uid"] for d in documents if d["status"] == "active"}
    all_blocks = [b for d in documents if d["doc_uid"] in active for b in blocks_by_doc[d["doc_uid"]]]
    write_jsonl(out / "documents.jsonl", documents)
    write_jsonl(out / "blocks.jsonl", all_blocks)
    # Blocks of duplicates, listings and shells: not evidence, but kept so the
    # retrieval ablation can measure what cleaning and dedup buy.  P1-2 demoted
    # blocks (template boilerplate / intra-doc duplicates) land here too.
    write_jsonl(out / "inactive_blocks.jsonl", demoted_blocks + [b for d in documents if d["doc_uid"] not in active and d["status"] in INACTIVE_INDEXABLE for b in blocks_by_doc.get(d["doc_uid"], [])])
    write_jsonl(out / "rejected.jsonl", rejected)
    write_jsonl(out / "baseline_pages.jsonl", baseline)
    (out / "series.json").write_text(json.dumps(series, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "dedup.json").write_text(json.dumps(dedup_records, ensure_ascii=False, indent=1), encoding="utf-8")
    stats = corpus_stats(documents, all_blocks, rejected, series, dedup_records, baseline)
    manifest = {
        "schema": SCHEMA_VERSION, "build_id": build_id, "created_at": datetime.now(timezone.utc).isoformat(),
        "code_hash": code_hash, "crawl_root": str(crawl), "config": config, "stats": stats,
        "deps": _deps_snapshot(),
        "models": _models_snapshot(),
        "files": {name: sha256((out / name).read_bytes()) for name in ("documents.jsonl", "blocks.jsonl", "series.json", "dedup.json", "rejected.jsonl", "baseline_pages.jsonl", "inactive_blocks.jsonl")},
        "seconds": round(time.perf_counter() - started, 1),
    }
    if args.base:
        manifest["diff"] = _incremental_diff(out_root, args.base, out, documents)
        # Carry LLM sidecars for docs that survived unchanged: their cached
        # annotations remain valid (same doc_uid + raw_sha256), and without
        # the sidecar the active_llm_* arms cannot index the new build at all.
        present = {d["doc_uid"]: d.get("raw_sha256", "") for d in documents}
        carried = {}
        for sidecar in ("llm_annotations.jsonl", "llm_retrieval_text.jsonl", "llm_row_contexts.jsonl"):
            base_path = out_root / args.base / sidecar
            if not base_path.is_file():
                continue
            kept = []
            for row in read_jsonl(base_path):
                uid = row.get("doc_uid", "")
                if uid in present and present[uid] == row.get("doc_sha256", present[uid]):
                    kept.append(row)
            if kept:
                write_jsonl(out / sidecar, kept)
                carried[sidecar] = len(kept)
        if carried:
            manifest["carried_sidecars"] = carried
            log(f"sidecars carried from base: {carried}")
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "REPORT.md").write_text(render_report(manifest, documents, series, dedup_records, rejected), encoding="utf-8")
    (out_root / "LATEST").write_text(build_id, encoding="utf-8")
    log(f"wrote {out}")
    return out


# ---------------------------------------------------------------- reporting


def _incremental_diff(out_root: Path, base_id: str, out: Path, documents: list[dict[str, Any]]) -> dict[str, Any]:
    """P6: classify docs against a base build and carry over unchanged LLM sidecars.

    Identity = (doc_uid, raw_sha256, rel_path): a page whose crawled bytes or
    location changed is "changed" even when the uid is stable.  Sidecar rows of
    unchanged docs are copied into the new build so annotation coverage survives
    rebuilds; ``changed_docs.txt`` lists what a pilot re-run should target
    (``python -m annotate.run_pilot --uids-file <build>/changed_docs.txt``).
    """
    base_dir = out_root / base_id
    if not (base_dir / "documents.jsonl").is_file():
        raise SystemExit(f"--base build not found: {base_dir}")
    base_docs = {row["doc_uid"]: row for row in read_jsonl(base_dir / "documents.jsonl")}
    current = {row["doc_uid"]: row for row in documents}
    added = sorted(set(current) - set(base_docs))
    removed = sorted(set(base_docs) - set(current))
    changed = sorted(
        uid for uid in set(current) & set(base_docs)
        if current[uid].get("raw_sha256") != base_docs[uid].get("raw_sha256")
        or current[uid].get("rel_path") != base_docs[uid].get("rel_path")
    )
    unchanged = sorted(set(current) & set(base_docs) - set(changed))
    (out / "changed_docs.txt").write_text("\n".join(changed + added) + ("\n" if changed or added else ""), encoding="utf-8")
    for sidecar in ("llm_annotations.jsonl", "llm_retrieval_text.jsonl", "llm_row_contexts.jsonl"):
        src = base_dir / sidecar
        if not src.is_file():
            continue
        keep = set(unchanged)
        rows = [row for row in read_jsonl(src) if row.get("doc_uid") in keep]
        if rows:
            write_jsonl(out / sidecar, rows)
    print(f"[incremental] vs {base_id}: +{len(added)} ~{len(changed)} -{len(removed)} ={len(unchanged)} (re-run targets -> changed_docs.txt)", flush=True)
    return {"base": base_id, "added": len(added), "changed": len(changed),
            "removed": len(removed), "unchanged": len(unchanged),
            "added_uids": added[:200], "changed_uids": changed[:200]}


def _deps_snapshot() -> dict[str, Any]:
    """Environment facts a rebuild must reproduce (uv.lock hash + live versions).

    The lock alone is not enough: the CUDA torch wheel is installed outside
    ``uv sync`` (pyproject notes "CUDA 轮另装"), so the actually-imported
    versions are recorded next to the lock hash.
    """
    from importlib import metadata

    def version(name: str) -> str | None:
        try:
            return metadata.version(name)
        except metadata.PackageNotFoundError:
            return None

    lock = ROOT / "uv.lock"
    return {
        "uv_lock_sha256": sha256(lock.read_bytes()) if lock.is_file() else "",
        "packages": {name: version(name) for name in ("torch", "rapidocr-onnxruntime", "onnxruntime", "sentence-transformers", "transformers", "pymupdf")},
    }


def _models_snapshot() -> dict[str, Any]:
    """Local model folders the eval stack pins (dense embedding + rerankers)."""
    out: dict[str, Any] = {}
    for key, folder in (("dense_embedding", "G:/model_download/builder/embed/bge-small-zh"), ("reranker_bge_v2_m3", "G:/model_download/builder/rerank/bge-v2-m3"), ("reranker_bge_base", "G:/model_download/builder/rerank/bge-base")):
        path = ROOT / folder
        entry: dict[str, Any] = {"path": folder, "present": path.is_dir()}
        if path.is_dir():
            cfg = path / "config.json"
            entry["config_sha256"] = sha256(cfg.read_bytes())[:12] if cfg.is_file() else ""
        out[key] = entry
    return out


def corpus_stats(documents, blocks, rejected, series, dedup, baseline) -> dict[str, Any]:
    counter = lambda key, rows=documents: dict(Counter(str(r.get(key, "")) for r in rows).most_common())  # noqa: E731
    active = [d for d in documents if d["status"] == "active"]
    # Compare like with like: only pages that have a crawler `_text` file, and
    # clean content without repeated table headers.
    baseline_chars = sum(len(normalize_text(row["text"])) for row in baseline)
    with_baseline = {row["doc_uid"] for row in baseline}
    page_chars = sum(d.get("content_chars", d["chars"]) for d in documents if d["doc_uid"] in with_baseline)
    return {
        "documents": len(documents),
        "active_documents": len(active),
        "blocks": len(blocks),
        "block_kinds": counter("kind", blocks),
        "status": counter("status"),
        "by_site_kind_status": dict(Counter(f"{d['site']}/{d['kind']}/{d['status']}" for d in documents).most_common()),
        "active_tier": counter("tier", active),
        "active_doc_type": counter("doc_type", active),
        "active_audience": counter("audience", active),
        "active_scope": counter("scope_level", active),
        "active_with_terms": sum(1 for d in active if d["terms"] or d["academic_years"]),
        "active_with_doc_number": sum(1 for d in active if d["doc_number"]),
        "rejected_reasons": dict(Counter(r["reason"].split(":")[0] for r in rejected).most_common()),
        "series": len(series),
        "series_members": sum(s["size"] for s in series),
        "superseded_documents": sum(1 for d in active if not d.get("is_latest", True)),
        "duplicate_groups": len(dedup),
        "duplicates": sum(len(g["members"]) - 1 for g in dedup),
        "baseline_pages": len(baseline),
        "baseline_page_chars": baseline_chars,
        "clean_page_chars": page_chars,
        "page_char_reduction": round(1 - page_chars / max(1, baseline_chars), 4),
        "attachment_title_sources": counter("title_source", [d for d in documents if d["kind"] == "attachment"]),
        "converted_legacy_count": sum(1 for d in documents if d.get("converted_from_legacy")),
        # OCR five-way accounting: docs that produced text via OCR, docs that
        # could not because the dependency is missing, docs whose OCR calls
        # failed, and the no-text / not-downloaded buckets stay visible in
        # rejected_reasons.  A doc can appear in more than one doc bucket when
        # only some pages were affected.
        "ocr": {
            "docs_succeeded": sum(1 for d in documents if d.get("n_ocr_succeeded")),
            "docs_dependency_missing": sum(1 for d in documents if d.get("n_ocr_dependency_missing")),
            "docs_failed": sum(1 for d in documents if d.get("n_ocr_failed")),
            "pages_attempted": sum(d.get("n_ocr_attempted", 0) for d in documents),
            "pages_succeeded": sum(d.get("n_ocr_succeeded", 0) for d in documents),
            "pages_failed": sum(d.get("n_ocr_failed", 0) for d in documents),
            "pages_dependency_missing": sum(d.get("n_ocr_dependency_missing", 0) for d in documents),
        },
    }


def _table(rows: dict[str, Any], header: tuple[str, str] = ("key", "count")) -> str:
    lines = [f"| {header[0]} | {header[1]} |", "| --- | ---: |"]
    lines += [f"| {key or '(empty)'} | {value} |" for key, value in rows.items()]
    return "\n".join(lines)


def render_report(manifest, documents, series, dedup, rejected) -> str:
    stats = manifest["stats"]
    lines = [
        f"# Corpus build {manifest['build_id']}", "",
        f"schema `{manifest['schema']}` · code `{manifest['code_hash']}` · {manifest['seconds']} s", "",
        "> 本文件只含统计与标题，不含正文。语料本身位于 gitignored 的 `data/corpus/`。", "",
        f"- documents: {stats['documents']} (active {stats['active_documents']})",
        f"- blocks: {stats['blocks']}",
        f"- duplicate groups: {stats['duplicate_groups']} (removed {stats['duplicates']})",
        f"- version series: {stats['series']} ({stats['series_members']} members, {stats['superseded_documents']} superseded active docs)",
        f"- active docs with term/academic-year: {stats['active_with_terms']}; with 发文号: {stats['active_with_doc_number']}",
        f"- legacy converted docs: {stats.get('converted_legacy_count', 0)}",
        f"- OCR: {stats['ocr']['docs_succeeded']} docs succeeded ({stats['ocr']['pages_succeeded']}/{stats['ocr']['pages_attempted']} pages); "
        f"dependency missing on {stats['ocr']['docs_dependency_missing']} docs ({stats['ocr']['pages_dependency_missing']} pages); "
        f"failed on {stats['ocr']['docs_failed']} docs ({stats['ocr']['pages_failed']} pages)",
        f"- page text ({stats.get('baseline_pages', 0)} pages with crawler `_text`): {stats['baseline_page_chars']:,} chars → clean content {stats['clean_page_chars']:,} chars ({-stats['page_char_reduction']:+.1%}; table header repetition excluded)", "",
        "## Status by site/kind", "", _table(stats["by_site_kind_status"]), "",
        "## Active documents", "", "### tier", "", _table(stats["active_tier"]), "", "### doc_type", "", _table(stats["active_doc_type"]), "",
        "### audience", "", _table(stats["active_audience"]), "", "### scope_level", "", _table(stats["active_scope"]), "",
        "### block kinds", "", _table(stats["block_kinds"]), "",
        "### attachment title source", "", _table(stats["attachment_title_sources"]), "",
        "## Rejected", "", _table(stats["rejected_reasons"]), "",
        "## Largest version series", "",
    ]
    for record in series[:25]:
        titles = " → ".join(f"{m['title'][:40]}{'@' + ','.join(m['terms'][:2]) if m['terms'] else ''}" for m in record["members"][-4:])
        lines.append(f"- `{record['key'][:40]}` ({record['site']}/{record['doc_type']}, {record['size']}{', ambiguous latest' if record['ambiguous_latest'] else ''}): {titles}")
    lines += ["", "## Largest duplicate groups", ""]
    for record in sorted(dedup, key=lambda r: -len(r["members"]))[:15]:
        sites = Counter(m["site"] for m in record["members"])
        lines.append(f"- {record['kind']} ×{len(record['members'])} ({dict(sites)}): {record['members'][0]['title'][:50]}")
    lines += ["", "## Core documents needing OCR", ""]
    ocr = [d for d in documents if d.get("needs_ocr_pages") and d["tier"] == "core"]
    lines += [f"- {d['site']}: {d['title'][:60]} (pages {len(d['needs_ocr_pages'])}/{d.get('pages', 0)})" for d in ocr[:40]]
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--crawl-root", default=r"E:\Projects\浙大网站爬取")
    parser.add_argument("--out", default=str(ROOT / "data" / "corpus"))
    parser.add_argument("--sites", default="", help="comma list; default " + ",".join(DEFAULT_SITES))
    parser.add_argument("--grs-attachments", action="store_true")
    parser.add_argument("--convert-legacy", action="store_true", help="convert .doc/.xls via Office COM (cached)")
    parser.add_argument("--ocr", action="store_true", help="OCR image-only PDF pages of core sites (slow)")
    parser.add_argument("--workers", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0, help="pages per CMS site (smoke runs)")
    parser.add_argument("--build-id", default="")
    parser.add_argument("--base", default="", help="P6 incremental: diff against this build id, carry over unchanged docs' LLM sidecars, write changed_docs.txt")
    parser.add_argument("--set-latest", default="", help="P6 rollback: repoint the LATEST marker to this build id and exit (builds are immutable)")
    parser.add_argument("--snapshot", default="", help="Stage 3A strict intake: raw-snapshot directory (records.jsonl/objects/); verify every byte against it and read from the object store")
    args = parser.parse_args(argv)
    if args.set_latest:
        out_root = Path(args.out)
        target = out_root / args.set_latest
        if not (target / "manifest.json").is_file():
            raise SystemExit(f"not a build directory: {target}")
        (out_root / "LATEST").write_text(args.set_latest, encoding="utf-8")
        print(f"LATEST -> {args.set_latest}")
        return
    build(args)


if __name__ == "__main__":
    main()
