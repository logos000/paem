"""Stage 3A W3: strict raw-snapshot intake (SnapshotStore + pipeline gate)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ingest.snapshot import SnapshotStore, apply_snapshot_gate, snapshot_rel
from ingest.sources import SourceItem


def _write_object(snapshot: Path, content: bytes, ext: str) -> tuple[str, str]:
    sha = hashlib.sha256(content).hexdigest()
    rel = f"objects/{sha}.{ext}"
    (snapshot / rel).parent.mkdir(parents=True, exist_ok=True)
    (snapshot / rel).write_bytes(content)
    return sha, rel


def _mini_snapshot(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    """Snapshot of a two-file crawl tree; returns (snapshot_dir, crawl_root, shas)."""
    crawl = tmp_path / "crawl"
    page = crawl / "zju-sites" / "archive" / "zju" / "_raw" / "www.zju.edu.cn" / "a.htm"
    page.parent.mkdir(parents=True)
    page.write_text("<html><body><p>2026-fall notice</p></body></html>", encoding="utf-8")
    text = crawl / "zju-sites" / "archive" / "zju" / "_text" / "www.zju.edu.cn" / "a.htm.txt"
    text.parent.mkdir(parents=True, exist_ok=True)
    text.write_bytes(b"2026-fall notice")
    attachment = crawl / "zju-sites" / "archive" / "zju" / "www.zju.edu.cn" / "files" / "report.pdf"
    attachment.parent.mkdir(parents=True, exist_ok=True)
    attachment.write_bytes(b"%PDF-fake")
    snapshot = tmp_path / "raw-snapshot" / "snap-test"
    snapshot.mkdir(parents=True)
    shas: dict[str, str] = {}
    records = []
    for rel_path, content, ext, fetched in [
        ("zju-sites/archive/zju/_raw/www.zju.edu.cn/a.htm", page.read_bytes(), "htm", "2026-09-01T08:00:00+00:00"),
        ("zju-sites/archive/zju/_text/www.zju.edu.cn/a.htm.txt", text.read_bytes(), "txt", "2026-09-01T08:00:00+00:00"),
        ("zju-sites/archive/zju/www.zju.edu.cn/files/report.pdf", attachment.read_bytes(), "pdf", "2026-09-02T08:00:00+00:00"),
    ]:
        sha, obj = _write_object(snapshot, content, ext)
        shas[rel_path] = sha
        records.append({"material_id": "m_" + sha[:8], "rel_path": rel_path, "object": obj, "sha256": sha,
                        "bytes": len(content), "media_type": "application/octet-stream", "source_url": "",
                        "parent_page": "", "fetched_at": fetched, "download_status": "archived"})
    with (snapshot / "records.jsonl").open("w", encoding="utf-8") as handle:
        for row in records:
            handle.write(json.dumps(row) + "\n")
    with (snapshot / "failures.jsonl").open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({"ledger": "x", "target": "https://gone", "download_status": "forbidden", "note": "HTTP 403"}) + "\n")
    (snapshot / "manifest.json").write_text(json.dumps({
        "snapshot_id": "snap-test", "sha256_of_hashes": "abc123",
        "counts": {"records": len(records), "objects": len(records), "failures": 1, "bytes_total": 42},
    }), encoding="utf-8")
    return snapshot, crawl, shas


# ------------------------------------------------------------------ mapping

def test_snapshot_rel_unifies_both_conventions():
    assert snapshot_rel("sharepoint/_page-content-raw/Home.canvas.html") == "ZJUIntl/sites/ZJUIUG/_page-content-raw/Home.canvas.html"
    assert snapshot_rel("zju/_raw/www.zju.edu.cn/a.htm") == "zju-sites/archive/zju/_raw/www.zju.edu.cn/a.htm"
    assert snapshot_rel("zju-sites/archive/zju/_raw/a.htm") == "zju-sites/archive/zju/_raw/a.htm"
    assert snapshot_rel("ZJUIntl/sites/ZJUIUG/Shared Documents/x.pdf") == "ZJUIntl/sites/ZJUIUG/Shared Documents/x.pdf"


# ------------------------------------------------------------------ store

def test_store_verify_and_object_paths(tmp_path):
    snapshot, crawl, shas = _mini_snapshot(tmp_path)
    store = SnapshotStore.load(snapshot)
    assert len(store) == 3
    page_path = crawl / "zju-sites" / "archive" / "zju" / "_raw" / "www.zju.edu.cn" / "a.htm"
    assert store.verify_path("zju/_raw/www.zju.edu.cn/a.htm", page_path) == "ok"
    assert store.object_path("zju/_raw/www.zju.edu.cn/a.htm") == snapshot / "objects" / f"{shas['zju-sites/archive/zju/_raw/www.zju.edu.cn/a.htm']}.htm"
    assert store.verify_path("zju/_raw/www.zju.edu.cn/missing.htm", page_path) == "not_in_snapshot"
    page_path.write_text("<html>mutated</html>", encoding="utf-8")
    assert store.verify_path("zju/_raw/www.zju.edu.cn/a.htm", page_path) == "raw_hash_mismatch"
    assert store.fetched_at("zju/_raw/www.zju.edu.cn/a.htm") == "2026-09-01"
    assert store.failures_by_status() == {"forbidden": 1}
    assert store.summary()["snapshot_id"] == "snap-test"


# ------------------------------------------------------------------ gate

def _page(crawl: Path) -> SourceItem:
    path = crawl / "zju-sites" / "archive" / "zju" / "_raw" / "www.zju.edu.cn" / "a.htm"
    text = crawl / "zju-sites" / "archive" / "zju" / "_text" / "www.zju.edu.cn" / "a.htm.txt"
    return SourceItem(site="zju", kind="page", source_url="https://www.zju.edu.cn/a.htm",
                      local_path=path, rel_path="zju/_raw/www.zju.edu.cn/a.htm", raw_text_path=text)


def _attachment_entry(crawl: Path) -> dict[str, Any]:
    path = crawl / "zju-sites" / "archive" / "zju" / "www.zju.edu.cn" / "files" / "report.pdf"
    import hashlib as _h
    return {"sha": _h.sha256(path.read_bytes()).hexdigest(), "path": path, "site": "zju",
            "urls": ["https://www.zju.edu.cn/files/report.pdf"], "labels": [], "parents": [],
            "embedded": False, "modified": "", "rel": "zju-sites/archive/zju/www.zju.edu.cn/files/report.pdf"}


def test_gate_verifies_and_redirects_to_object_store(tmp_path):
    snapshot, crawl, shas = _mini_snapshot(tmp_path)
    store = SnapshotStore.load(snapshot)
    page = _page(crawl)
    entry = _attachment_entry(crawl)
    rejected: list[dict[str, Any]] = []
    kept, dropped_pages, dropped_att = apply_snapshot_gate(store, [page], {entry["sha"]: entry}, rejected, crawl)
    assert (dropped_pages, dropped_att) == (0, 0)
    assert len(kept) == 1 and not rejected
    # all reads now point into the object store, not the crawl tree
    assert kept[0].local_path == store.object_path("zju/_raw/www.zju.edu.cn/a.htm")
    assert kept[0].raw_text_path == store.object_path("zju-sites/archive/zju/_text/www.zju.edu.cn/a.htm.txt")
    assert entry["path"] == snapshot / "objects" / f"{shas['zju-sites/archive/zju/www.zju.edu.cn/files/report.pdf']}.pdf"


def test_gate_rejects_mutated_and_unknown_inputs(tmp_path):
    snapshot, crawl, shas = _mini_snapshot(tmp_path)
    store = SnapshotStore.load(snapshot)
    page = _page(crawl)
    page.local_path.write_text("<html>mutated after snapshot</html>", encoding="utf-8")
    unknown = SourceItem(site="zju", kind="page", source_url="https://www.zju.edu.cn/new.htm",
                         local_path=crawl / "zju-sites" / "archive" / "zju" / "_raw" / "www.zju.edu.cn" / "new.htm",
                         rel_path="zju/_raw/www.zju.edu.cn/new.htm")
    unknown.local_path.parent.mkdir(parents=True, exist_ok=True)
    unknown.local_path.write_text("<html>new file</html>", encoding="utf-8")
    entry = _attachment_entry(crawl)
    entry["sha"] = "0" * 64  # simulate hash mismatch
    rejected: list[dict[str, Any]] = []
    kept, dropped_pages, dropped_att = apply_snapshot_gate(store, [page, unknown], {entry["sha"]: entry}, rejected, crawl)
    assert kept == []
    assert (dropped_pages, dropped_att) == (2, 1)
    reasons = sorted(row["reason"] for row in rejected)
    # mutated page → raw_hash_mismatch; unknown page → not_in_snapshot;
    # tampered attachment hash → raw_hash_mismatch
    assert reasons == ["not_in_snapshot", "raw_hash_mismatch", "raw_hash_mismatch"]
