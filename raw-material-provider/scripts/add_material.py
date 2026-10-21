#!/usr/bin/env python3
"""Place one notice/PDF into the crawl tree (Stage 5 V1: the ingest core).

The ONLY sanctioned write path for new material.  After placement it
re-freezes the raw snapshot (idempotent) so the next build picks the file up
through the normal pipeline (attachment scans / _index.jsonl).

Kinds
-----
pdf              an attachment (pdf/doc(x)/xls(x)/txt/…) →
                 ZJUIntl/sites/ZJUIUG/Shared Documents/<path>/ — discovered
                 by the pipeline's Shared-Documents scan, no index needed.
page             an HTML notice page →
                 zju-sites/archive/<site>/_raw/<host>/<rel> plus ONE
                 _index.jsonl row (url/title required, URL deduplicated).
cms-attachment   an attachment belonging to a CMS site →
                 zju-sites/archive/<site>/<host>/<rel> — discovered by the
                 orphan scan.

Safety: never overwrites (same sha256 ⇒ idempotent success; different content
at the same path ⇒ abort).  Every placement is appended to
inbox/materials_log.jsonl (the ingest audit chain).  browser-profile and
crawler code are never touched.

Usage:
    python scripts/add_material.py notice.pdf --kind pdf --path "Registrar/2026"
    python scripts/add_material.py notice.html --kind page --site zjui \
        --url https://zjui.intl.zju.edu.cn/notice/2026/x.html --title "…"
    python scripts/add_material.py --job inbox/jobs/<id>.json     # watcher mode
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
import sys

PROVIDER = Path(__file__).resolve().parents[1]
CRAWL_ROOT = Path(r"E:\Projects\浙大网站爬取")
INBOX = PROVIDER / "inbox"
SNAPSHOT_SCRIPT = PROVIDER / "snapshot.py"
MAX_BYTES = 100 * 1024 * 1024

# Mirror of llm-corpus-builder ingest/sources.ARCHIVE_SITES (+ sharepoint).
# Duplicated on purpose: the provider is deliberately dependency-free.
SITES = ("sharepoint", "zjui", "ugrs", "grs", "zju", "news", "libweb", "iczu", "zibs")
ATTACHMENT_EXTS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".xlsm", ".txt", ".ppt", ".pptx"}
PAGE_EXTS = {".html", ".htm"}


class IngestError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _log(record: dict) -> None:
    INBOX.mkdir(parents=True, exist_ok=True)
    record = {"ts": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"), **record}
    with (INBOX / "materials_log.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _place(source: Path, target: Path) -> str:
    """Copy source→target; return placed|already-present."""
    if target.exists():
        if _sha256(target) == _sha256(source):
            return "already-present"
        raise IngestError(f"refusing to overwrite existing different file: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.parent.resolve() == CRAWL_ROOT.resolve():
        raise IngestError("placement must stay inside the crawl tree subdirectories")
    shutil.copyfile(source, target)
    return "placed"


def add_material(
    file: Path,
    *,
    kind: str,
    site: str = "sharepoint",
    title: str = "",
    url: str = "",
    published: str = "",
    path: str = "",
    crawl_root: Path = CRAWL_ROOT,
    log: bool = True,
) -> dict:
    if kind not in ("pdf", "page", "cms-attachment"):
        raise IngestError(f"unknown kind {kind!r}")
    if site not in SITES:
        raise IngestError(f"unknown site {site!r}; allowed: {SITES}")
    file = Path(file)
    if not file.is_file():
        raise IngestError(f"file not found: {file}")
    if file.stat().st_size > MAX_BYTES:
        raise IngestError(f"file exceeds {MAX_BYTES // (1024 * 1024)}MB limit")
    ext = file.suffix.lower()
    sha = _sha256(file)
    result = {"file": file.name, "sha256": sha, "kind": kind, "site": site, "title": title, "url": url, "published": published}

    if kind == "pdf":
        if ext not in ATTACHMENT_EXTS:
            raise IngestError(f"pdf kind accepts {sorted(ATTACHMENT_EXTS)}, got {ext}")
        target = crawl_root / "ZJUIntl" / "sites" / "ZJUIUG" / "Shared Documents" / (path or "Imported") / file.name
        result["status"] = _place(file, target)
        result["target"] = str(target)

    elif kind == "page":
        if ext not in PAGE_EXTS:
            raise IngestError(f"page kind accepts {sorted(PAGE_EXTS)}, got {ext}")
        if not url.startswith(("http://", "https://")):
            raise IngestError("page kind requires --url (the notice's real source URL)")
        if not title.strip():
            raise IngestError("page kind requires --title")
        if site == "sharepoint":
            raise IngestError("page kind targets CMS sites; use the SharePoint export for SharePoint pages")
        from urllib.parse import unquote, urlparse

        parsed = urlparse(url)
        host = parsed.netloc.lower().removeprefix("www.")
        if not host:
            raise IngestError(f"cannot derive host from url {url!r}")
        rel = (path or parsed.path).strip("/")
        if not rel:
            raise IngestError(f"cannot derive a path from url {url!r}; pass --path")
        if not rel.endswith(ext):
            rel = f"{rel}{ext}" if not rel.rsplit(".", 1)[-1] else rel
        site_dir = crawl_root / "zju-sites" / "archive" / site
        target = site_dir / "_raw" / host / unquote(rel)
        result["status"] = _place(file, target)
        result["target"] = str(target)
        if result["status"] == "placed":
            index = site_dir / "_index.jsonl"
            rows = [json.loads(line) for line in index.read_text(encoding="utf-8").splitlines() if line.strip()] if index.is_file() else []
            if any(row.get("url") == url for row in rows):
                raise IngestError(f"_index.jsonl already has url {url} but the file was new — refusing inconsistent state")
            rows.append({"url": url, "title": title, "rel": f"{host}/{unquote(rel)}", "bytes": file.stat().st_size, "keys": []})
            with index.open("a", encoding="utf-8") as handle:  # append keeps prior bytes untouched
                handle.write(json.dumps(rows[-1], ensure_ascii=False) + "\n")
            result["index_row"] = True

    else:  # cms-attachment
        if ext not in ATTACHMENT_EXTS:
            raise IngestError(f"cms-attachment kind accepts {sorted(ATTACHMENT_EXTS)}, got {ext}")
        if not url.startswith(("http://", "https://")):
            raise IngestError("cms-attachment kind requires --url (the attachment's real URL)")
        from urllib.parse import unquote, urlparse

        parsed = urlparse(url)
        host = parsed.netloc.lower().removeprefix("www.")
        rel = unquote(parsed.path.strip("/"))
        if not host or not rel:
            raise IngestError(f"cannot derive host/path from url {url!r}")
        target = crawl_root / "zju-sites" / "archive" / site / host / rel
        result["status"] = _place(file, target)
        result["target"] = str(target)

    if log:
        _log(result)
    return result


def run_snapshot(snapshot_id: str = "snap-20261014-v1") -> int:
    """Idempotent re-freeze so the next build verifies the new bytes."""
    code = subprocess.run([sys.executable, str(SNAPSHOT_SCRIPT), "--snapshot-id", snapshot_id]).returncode
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("file", nargs="?", type=Path, help="the material file (or omit with --job)")
    parser.add_argument("--job", type=Path, help="job json ({file, kind, site, title, url, published, path}) — watcher mode")
    parser.add_argument("--kind", choices=("pdf", "page", "cms-attachment"))
    parser.add_argument("--site", default="sharepoint")
    parser.add_argument("--title", default="")
    parser.add_argument("--url", default="")
    parser.add_argument("--published", default="", help="recorded in the ingest audit log")
    parser.add_argument("--path", default="", help="subpath (pdf: under Shared Documents; page: overrides the URL path)")
    parser.add_argument("--no-snapshot", action="store_true")
    parser.add_argument("--snapshot-id", default="snap-20261014-v1")
    args = parser.parse_args(argv)

    if args.job:
        spec = json.loads(args.job.read_text(encoding="utf-8"))
        args.file = Path(spec["file"])
        args.kind = spec.get("kind")
        args.site = spec.get("site", "sharepoint")
        args.title = spec.get("title", "")
        args.url = spec.get("url", "")
        args.published = spec.get("published", "")
        args.path = spec.get("path", "")
    if not args.file or not args.kind:
        parser.error("need <file> and --kind (or --job)")

    try:
        result = add_material(args.file, kind=args.kind, site=args.site, title=args.title, url=args.url,
                              published=args.published, path=args.path)
    except IngestError as error:
        print(f"INGEST FAILED: {error}")
        return 1
    print(json.dumps(result, ensure_ascii=False))
    if result["status"] == "placed" and not args.no_snapshot:
        code = run_snapshot(args.snapshot_id)
        if code != 0:
            print(f"SNAPSHOT RE-FREEZE FAILED (exit {code}) — material is placed but not yet snapshotted")
            return 2
        print("[snapshot] re-frozen")
    print("下一步：python scripts/update_cycle.py --switch   （或等 watch_inbox 自动处理）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
