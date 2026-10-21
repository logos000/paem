#!/usr/bin/env python3
"""Build the raw snapshot object store from the existing crawl tree (v1).

Scans the two material roots of the crawl tree (read-only), hashes every
file, copies its bytes into a content-addressed object store and emits the
snapshot contract of docs/INTERFACE.md:

    raw-snapshot/<snapshot_id>/
        manifest.json      snapshot identity + counts + sha256_of_hashes
        records.jsonl      one line per material file (material_id, rel_path,
                           object, sha256, bytes, media_type, source_url,
                           parent_page, fetched_at, download_status)
        failures.jsonl     crawl-time failures normalized to the status
                           vocabulary (ok/not_found/forbidden/timeout/
                           decode_error/too_large/unsupported/manual_upload)
        objects/<sha256>.<ext>

Immutability model v1 ("legacy-tree import"): the object store is the
authoritative byte-level copy; the original crawl tree keeps its role as the
*discovery* source for this snapshot but builder verification
(``pipeline --snapshot``) accepts only bytes that hash-match a record.
Existing-tree mutation scripts (finalize/repair) must finish before this
snapshot is taken.

Usage:
    python snapshot.py --crawl-root "E:/Projects/浙大网站爬取" \
        --out raw-snapshot/snap-20261014 --resume

Idempotent: rerunning with the same snapshot_id skips objects already stored
(same sha256 ⇒ same object) and rewrites records/manifest deterministically.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time

MATERIAL_ROOTS = ("zju-sites/archive", "ZJUIntl")
# Crawler-side failure ledgers (NOT copied into objects; normalized into
# failures.jsonl).  Paths relative to the crawl root.
FAILURE_FILES = ("zju-sites/retry-failures.json", "zju-sites/sync-rest-failures.json", "zjuiug-crawl/failed.json")
SP_BASE = "https://zjuintl.sharepoint.com/sites/ZJUIUG"
CHUNK = 1 << 20

STATUS_BY_KEYWORD = (
    ("403", "forbidden"),
    ("forbidden", "forbidden"),
    ("404", "not_found"),
    ("not_found", "not_found"),
    ("timeout", "timeout"),
    ("timed out", "timeout"),
    ("size", "decode_error"),
    ("decode", "decode_error"),
)


def _status_for(message: str) -> str:
    lowered = str(message).lower()
    for keyword, status in STATUS_BY_KEYWORD:
        if keyword in lowered:
            return status
    return "unsupported"


def _material_id(rel_path: str) -> str:
    return "m_" + hashlib.sha256(rel_path.encode("utf-8")).hexdigest()[:16]


def _media_type(path: Path) -> str:
    import mimetypes

    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _load_cms_urls(archive_root: Path) -> dict[str, str]:
    """site/_raw-relative rel -> source URL, from each site's _index.jsonl."""
    mapping: dict[str, str] = {}
    if not archive_root.is_dir():
        return mapping
    for site_dir in sorted(archive_root.iterdir()):
        index = site_dir / "_index.jsonl"
        if not index.is_file():
            continue
        prefix = f"zju-sites/archive/{site_dir.name}/"
        with index.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                rel = str(row.get("rel", "")).replace("\\", "/")
                url = str(row.get("url", ""))
                if rel and url:
                    mapping[f"{prefix}_raw/{rel}"] = url
                    text_rel = rel.rsplit(".", 1)[0] + ".txt"
                    mapping[f"{prefix}_text/{text_rel}"] = url
    return mapping


def _load_sharepoint_urls(zjuiintl_root: Path) -> dict[str, str]:
    """Object rel paths -> source URLs for canvas pages and Shared Documents."""
    mapping: dict[str, str] = {}
    raw_dir = zjuiintl_root / "sites" / "ZJUIUG" / "_page-content-raw"
    render_path = zjuiintl_root / "sites" / "ZJUIUG" / "_page-content-text" / "_render-index.json"
    render_urls: dict[str, str] = {}
    if render_path.is_file():
        try:
            payload = json.loads(render_path.read_text(encoding="utf-8"))
            rows = payload.values() if isinstance(payload, dict) else payload if isinstance(payload, list) else []
            render_urls = {str(row.get("name", "")): str(row.get("url", "")) for row in rows if isinstance(row, dict)}
        except ValueError:
            render_urls = {}
    if raw_dir.is_dir():
        for page in raw_dir.glob("*.canvas.html"):
            name = page.name[: -len(".canvas.html")]
            mapping[f"ZJUIntl/sites/ZJUIUG/_page-content-raw/{page.name}"] = render_urls.get(name) or f"{SP_BASE}/SitePages/{name}.aspx"
    return mapping


def _hash_and_store(source: Path, objects_dir: Path, *, resume: bool) -> tuple[str, str, int, bool]:
    """Return (sha256, ext, bytes, copied).  Writes the object atomically."""
    digest = hashlib.sha256()
    total = 0
    with source.open("rb") as handle:
        while True:
            chunk = handle.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
    sha = digest.hexdigest()
    ext = source.suffix.lstrip(".").lower() or "bin"
    target = objects_dir / f"{sha}.{ext}"
    if resume and target.is_file() and target.stat().st_size == total:
        return sha, ext, total, False
    temp = objects_dir / f".tmp-{sha}"
    shutil.copyfile(source, temp)
    if target.is_file() and target.suffix == f".{ext}":
        temp.unlink(missing_ok=True)
        return sha, ext, total, False
    temp.replace(target)
    return sha, ext, total, True


def _iter_failures(crawl_root: Path) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for rel in FAILURE_FILES:
        path = crawl_root / rel
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except ValueError:
            continue
        entries = payload if isinstance(payload, list) else payload.get("failures", []) if isinstance(payload, dict) else []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            target = str(entry.get("sr") or entry.get("url") or entry.get("rel") or entry.get("path") or entry.get("file") or "")
            message = str(entry.get("err") or entry.get("error") or entry.get("reason") or "")
            if not target and not message:
                continue
            records.append({
                "ledger": rel,
                "material_id": _material_id(target) if target else "",
                "target": target,
                "download_status": _status_for(message),
                "note": message[:300],
            })
    return records


def build_snapshot(crawl_root: Path, out_dir: Path, *, resume: bool = True, log_every: int = 2000) -> dict:
    started = time.time()
    objects_dir = out_dir / "objects"
    objects_dir.mkdir(parents=True, exist_ok=True)
    url_map: dict[str, str] = {}
    url_map.update(_load_cms_urls(crawl_root / "zju-sites" / "archive"))
    url_map.update(_load_sharepoint_urls(crawl_root / "ZJUIntl"))

    records: list[dict] = []
    copied = skipped = 0
    for root_rel in MATERIAL_ROOTS:
        root = crawl_root / root_rel
        if not root.is_dir():
            continue
        files = sorted(p for p in root.rglob("*") if p.is_file())
        for index, path in enumerate(files, 1):
            rel = path.relative_to(crawl_root).as_posix()
            try:
                sha, _ext, size, did_copy = _hash_and_store(path, objects_dir, resume=resume)
            except OSError as error:
                records.append({"material_id": _material_id(rel), "rel_path": rel, "object": "", "sha256": "", "bytes": 0,
                                "media_type": "application/octet-stream", "source_url": url_map.get(rel, ""), "parent_page": "",
                                "fetched_at": "", "download_status": "unsupported", "note": f"read error: {error}"})
                continue
            copied += int(did_copy)
            skipped += int(not did_copy)
            stat = path.stat()
            records.append({
                "material_id": _material_id(rel),
                "rel_path": rel,
                "object": f"objects/{sha}.{_ext}",
                "sha256": sha,
                "bytes": size,
                "media_type": _media_type(path),
                "source_url": url_map.get(rel, ""),
                "parent_page": "",
                "fetched_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(timespec="seconds"),
                "download_status": "archived",
            })
            if index % log_every == 0:
                print(f"[snapshot] {root_rel}: {index}/{len(files)} files ({time.time() - started:.0f}s)", flush=True)

    records.sort(key=lambda row: row["rel_path"])
    failures = _iter_failures(crawl_root)
    hashes = "\n".join(row["sha256"] for row in records if row["sha256"])
    manifest = {
        "snapshot_id": out_dir.name,
        "created_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "source_root": str(crawl_root),
        "roots": list(MATERIAL_ROOTS),
        "counts": {
            "records": len(records),
            "objects": len({row["object"] for row in records if row["object"]}),
            "failures": len(failures),
            "bytes_total": sum(row["bytes"] for row in records),
            "bytes_copied_this_run": copied,
        },
        "sha256_of_hashes": hashlib.sha256(hashes.encode("utf-8")).hexdigest(),
        "tool": "raw-material-provider/snapshot.py v1",
    }
    with (out_dir / "records.jsonl").open("w", encoding="utf-8") as handle:
        for row in records:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    with (out_dir / "failures.jsonl").open("w", encoding="utf-8") as handle:
        for row in failures:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[snapshot] done: {manifest['counts']} in {time.time() - started:.0f}s ({copied} copied, {skipped} dedup-skipped)")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--crawl-root", type=Path, default=Path(r"E:\Projects\浙大网站爬取"))
    parser.add_argument("--out", type=Path, default=None, help="snapshot directory (default: raw-snapshot/snap-<timestamp>)")
    parser.add_argument("--snapshot-id", default=None, help="override the snapshot id (directory name)")
    parser.add_argument("--no-resume", action="store_true", help="re-copy objects even when they already exist")
    args = parser.parse_args(argv)

    snapshot_id = args.snapshot_id or f"snap-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    out_dir = args.out or (Path(__file__).resolve().parent / "raw-snapshot" / snapshot_id)
    if args.snapshot_id and args.out is None:
        out_dir = Path(__file__).resolve().parent / "raw-snapshot" / snapshot_id
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = build_snapshot(args.crawl_root, out_dir, resume=not args.no_resume)
    print(json.dumps(manifest["counts"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
