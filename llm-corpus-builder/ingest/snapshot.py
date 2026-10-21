"""Immutable raw-snapshot intake (Stage 3A W3).

A snapshot (raw-material-provider ``snapshot.py``) is a content-addressed
object store plus records/failures ledgers.  Under ``pipeline --snapshot
<dir>`` the build enters strict intake:

* every page/attachment byte must hash-match a snapshot record — a mutated
  input is rejected (``raw_hash_mismatch``), an unknown input never enters
  the build (``not_in_snapshot``);
* every surviving byte read (page HTML re-read, baseline text, attachment
  extraction, legacy conversion) is redirected to the object store, so the
  build no longer depends on the mutable crawl tree;
* ``retrieved_at`` comes from the record's ``fetched_at`` (the crawl-time
  mtime), not from the freshly copied object file.

Rel-path conventions unified here: builder rel paths are site-scoped
(``zju/_raw/<host>/…``, ``sharepoint/_page-content-raw/…``); snapshot rel
paths are crawl-root-relative (``zju-sites/archive/…``,
``ZJUIntl/sites/ZJUIUG/…``).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from evidence.store import read_jsonl

CMS_ARCHIVE_PREFIX = "zju-sites/archive/"
SHAREPOINT_PREFIX = "sharepoint/"
SP_OBJECT_PREFIX = "ZJUIntl/sites/ZJUIUG/"


def snapshot_rel(builder_rel: str) -> str:
    """Builder rel_path → snapshot (crawl-root-relative) rel path."""
    if builder_rel.startswith(SHAREPOINT_PREFIX):
        return SP_OBJECT_PREFIX + builder_rel[len(SHAREPOINT_PREFIX):]
    if builder_rel.startswith("zju-sites/") or builder_rel.startswith("ZJUIntl/"):
        return builder_rel
    return CMS_ARCHIVE_PREFIX + builder_rel


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class SnapshotStore:
    """Read access to one snapshot directory (records by rel path)."""

    def __init__(self, root: Path, manifest: dict[str, Any], records: list[dict[str, Any]], failures: list[dict[str, Any]]):
        self.root = root
        self.manifest = manifest
        self.snapshot_id = str(manifest.get("snapshot_id") or root.name)
        self.failures = failures
        self._by_rel = {row["rel_path"]: row for row in records if row.get("rel_path")}

    @classmethod
    def load(cls, root: Path) -> "SnapshotStore":
        root = Path(root)
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        records = list(read_jsonl(root / "records.jsonl"))
        failures = list(read_jsonl(root / "failures.jsonl")) if (root / "failures.jsonl").is_file() else []
        return cls(root, manifest, records, failures)

    def __len__(self) -> int:
        return len(self._by_rel)

    def record_for(self, builder_rel: str) -> dict[str, Any] | None:
        return self._by_rel.get(snapshot_rel(builder_rel))

    def object_path(self, builder_rel: str) -> Path | None:
        record = self.record_for(builder_rel)
        if record is None or not record.get("object"):
            return None
        return self.root / record["object"]

    def verify_path(self, builder_rel: str, path: Path) -> str:
        """``ok`` / ``not_in_snapshot`` / ``raw_hash_mismatch``."""
        record = self.record_for(builder_rel)
        if record is None:
            return "not_in_snapshot"
        if not record.get("sha256") or record["sha256"] != _sha256_file(Path(path)):
            return "raw_hash_mismatch"
        return "ok"

    def fetched_at(self, builder_rel: str) -> str:
        record = self.record_for(builder_rel)
        return str(record.get("fetched_at", ""))[:10] if record else ""

    def failures_by_status(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.failures:
            status = str(row.get("download_status", "unsupported"))
            counts[status] = counts.get(status, 0) + 1
        return dict(sorted(counts.items()))

    def summary(self) -> dict[str, Any]:
        counts = self.manifest.get("counts", {})
        return {
            "snapshot_id": self.snapshot_id,
            "sha256_of_hashes": self.manifest.get("sha256_of_hashes", ""),
            "records": counts.get("records", len(self)),
            "objects": counts.get("objects", 0),
            "failures": len(self.failures),
            "failures_by_status": self.failures_by_status(),
        }


def apply_snapshot_gate(
    snapshot: SnapshotStore,
    pages: list[Any],
    attachments: dict[str, dict[str, Any]],
    rejected: list[dict[str, Any]],
    crawl: Path,
    log: Callable[[str], None] = lambda message: None,
) -> tuple[list[Any], int, int]:
    """Strict intake gate; returns ``(kept_pages, dropped_pages, dropped_attachments)``.

    Mutates ``attachments`` in place (drops + redirects ``entry["path"]`` to
    the object store) and rewrites each kept page's ``local_path`` /
    ``raw_text_path`` to object-store paths.
    """
    kept: list[Any] = []
    dropped_pages = 0
    for page in pages:
        if not page.local_path or not page.local_path.is_file():
            verdict = "not_in_snapshot"
        else:
            verdict = snapshot.verify_path(page.rel_path, page.local_path)
        if verdict != "ok":
            rejected.append({"source_url": page.source_url, "site": page.site, "rel_path": page.rel_path, "title": page.title, "reason": verdict})
            dropped_pages += 1
            continue
        page.local_path = snapshot.object_path(page.rel_path)
        if page.raw_text_path is not None:
            text_rel = page.raw_text_path.relative_to(crawl).as_posix() if page.raw_text_path.is_absolute() and page.raw_text_path.is_relative_to(crawl) else ""
            record = snapshot._by_rel.get(text_rel) if text_rel else None
            page.raw_text_path = snapshot.root / record["object"] if record and record.get("object") else None
        kept.append(page)

    dropped_attachments = 0
    for sha, entry in list(attachments.items()):
        rel = entry.get("rel") or ""
        record = snapshot.record_for(rel) if rel else None
        if record is None:
            verdict = "not_in_snapshot"
        elif record.get("sha256") != sha:
            verdict = "raw_hash_mismatch"
        else:
            verdict = "ok"
        if verdict != "ok":
            rejected.append({"source_url": entry["urls"][0] if entry.get("urls") else "", "site": entry.get("site", ""), "rel_path": rel, "reason": verdict})
            attachments.pop(sha)
            dropped_attachments += 1
        else:
            # Bytes come from the object store, but title/extension derivation
            # must keep seeing the ORIGINAL filename (an object is named by its
            # sha, which the UUID-ish title fallback would otherwise eat).
            entry["source_name"] = entry["path"]
            entry["path"] = snapshot.root / record["object"]
    log(f"snapshot gate: {len(kept)} pages / {len(attachments)} attachments verified ({dropped_pages}+{dropped_attachments} rejected)")
    return kept, dropped_pages, dropped_attachments
