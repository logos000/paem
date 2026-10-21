"""Tests for metrics.build_metrics (construction-period quality metrics)."""

import json
from pathlib import Path

from metrics.build_metrics import compute


def _write_build(tmp_path: Path) -> Path:
    build = tmp_path / "20260101-000000-abcd12"
    build.mkdir()
    docs = [
        {"doc_uid": "d_a", "status": "active", "site": "zjui", "page_kind": "article",
         "doc_type": "notice", "audience": "graduate", "terms": ["2026-fall"],
         "term_source": "title", "published": "2026-09-01", "series_id": "s1",
         "superseded_by": "", "duplicate_of": "", "warnings": [], "heading": 0},
        {"doc_uid": "d_b", "status": "active", "site": "zjui", "page_kind": "file",
         "doc_type": "page", "audience": "zjui_undergraduate", "terms": [],
         "term_source": "", "published": "", "series_id": "s1",
         "superseded_by": "d_a", "duplicate_of": "", "warnings": ["odd date"]},
        {"doc_uid": "d_c", "status": "duplicate", "site": "zjui", "page_kind": "article",
         "doc_type": "page", "audience": "zjui_undergraduate", "terms": [],
         "term_source": "", "published": "", "series_id": "", "superseded_by": "",
         "duplicate_of": "d_a", "warnings": []},
        {"doc_uid": "d_d", "status": "active", "site": "zjui", "page_kind": "article",
         "doc_type": "calendar", "audience": "zjui_undergraduate", "terms": ["2027-spring"],
         "term_source": "body", "published": "2027-01-01", "series_id": "",
         "superseded_by": "", "duplicate_of": "", "warnings": []},
    ]
    blocks = [
        {"block_id": "d_a:1", "doc_uid": "d_a", "kind": "table_row", "heading_path": ["Fall 2026"], "text": "row one"},
        {"block_id": "d_a:2", "doc_uid": "d_a", "kind": "table_row", "heading_path": [], "text": "row two"},
        {"block_id": "d_a:3", "doc_uid": "d_a", "kind": "paragraph", "heading_path": [], "text": "same"},
        {"block_id": "d_a:4", "doc_uid": "d_a", "kind": "paragraph", "heading_path": [], "text": "same"},
    ]
    (build / "documents.jsonl").write_text(
        "\n".join(json.dumps(d, ensure_ascii=False) for d in docs), encoding="utf-8")
    (build / "blocks.jsonl").write_text(
        "\n".join(json.dumps(b, ensure_ascii=False) for b in blocks), encoding="utf-8")
    (build / "dedup.json").write_text(json.dumps([{"members": ["d_a", "d_c"]}]), encoding="utf-8")
    (build / "series.json").write_text(
        json.dumps([{"series_id": "s1", "members": [{"doc_uid": "d_a", "is_latest": True},
                                                    {"doc_uid": "d_b", "is_latest": False}]}]),
        encoding="utf-8")
    return build


def test_compute_counts_and_ratios(tmp_path):
    m = compute(_write_build(tmp_path), today="2026-10-13")
    d = m["documents"]
    assert d["total"] == 4
    assert d["active"] == 3
    assert d["active_ratio"] == 0.75
    # d_a(graduate≠zjui 默认) 与 d_d… zjui 默认是 zjui_undergraduate，d_d 等于默认 → 显式 1
    assert d["explicit_audience"] == 1
    assert d["explicit_audience_ratio"] == round(1 / 3, 4)

    r = m["redundancy"]
    assert r["duplicate_members"] == 1
    assert r["dedup_groups"] == 1
    assert r["intra_doc_duplicate_blocks"] == 1  # d_a 的两个 "same" 段落

    v = m["versions"]
    assert v["series"] == 1
    assert v["series_members"] == 2
    assert v["superseded_active"] == 1
    assert v["multi_latest_series"] == 0

    t = m["temporal"]
    assert t["terms_coverage"] == round(2 / 3, 4)
    assert t["published_in_future"] == 1  # 2027-01-01 > today

    s = m["structure"]
    assert s["table_rows"] == 2
    assert s["table_rows_with_heading"] == 1
    assert s["table_row_context_ratio"] == 0.5

    g = m["gates"]
    assert g["warned_active_docs"] == 1
