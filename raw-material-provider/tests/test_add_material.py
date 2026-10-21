"""V1 ingest-core tests (run with the builder venv's pytest)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import add_material  # noqa: E402


@pytest.fixture()
def crawl(tmp_path):
    root = tmp_path / "crawl"
    (root / "zju-sites" / "archive" / "zjui").mkdir(parents=True)
    (root / "zju-sites" / "archive" / "zjui" / "_index.jsonl").write_text(
        json.dumps({"url": "https://zjui.intl.zju.edu.cn/old.html", "title": "Old", "rel": "zjui.intl.zju.edu.cn/old.html", "bytes": 1, "keys": []}) + "\n",
        encoding="utf-8",
    )
    return root


def _pdf(tmp_path, content=b"%PDF-fresh-notice") -> Path:
    path = tmp_path / "notice.pdf"
    path.write_bytes(content)
    return path


def test_pdf_places_into_shared_documents(tmp_path, crawl, monkeypatch):
    monkeypatch.setattr(add_material, "INBOX", tmp_path / "inbox")
    result = add_material.add_material(_pdf(tmp_path), kind="pdf", path="Registrar/2026", crawl_root=crawl, log=True)
    target = crawl / "ZJUIntl" / "sites" / "ZJUIUG" / "Shared Documents" / "Registrar" / "2026" / "notice.pdf"
    assert target.read_bytes() == b"%PDF-fresh-notice"
    assert result["status"] == "placed" and result["target"] == str(target)
    log_rows = [json.loads(line) for line in (tmp_path / "inbox" / "materials_log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert log_rows[0]["sha256"] == result["sha256"] and log_rows[0]["kind"] == "pdf"


def test_pdf_repeat_is_idempotent(tmp_path, crawl, monkeypatch):
    monkeypatch.setattr(add_material, "INBOX", tmp_path / "inbox")
    pdf = _pdf(tmp_path)
    first = add_material.add_material(pdf, kind="pdf", crawl_root=crawl)
    second = add_material.add_material(pdf, kind="pdf", crawl_root=crawl)
    assert first["status"] == "placed" and second["status"] == "already-present"
    # different content at the same name is refused
    clash = _pdf(tmp_path, b"%PDF-DIFFERENT")
    with pytest.raises(add_material.IngestError, match="refusing to overwrite"):
        add_material.add_material(clash, kind="pdf", crawl_root=crawl)


def _page(tmp_path, content=b"<html><body><p>2026-fall notice body</p></body></html>") -> Path:
    path = tmp_path / "notice.html"
    path.write_bytes(content)
    return path


def test_page_places_and_appends_index(tmp_path, crawl, monkeypatch):
    monkeypatch.setattr(add_material, "INBOX", tmp_path / "inbox")
    url = "https://zjui.intl.zju.edu.cn/notice/2026/fresh.html"
    result = add_material.add_material(_page(tmp_path), kind="page", site="zjui", title="Fresh Notice", url=url, crawl_root=crawl)
    target = crawl / "zju-sites" / "archive" / "zjui" / "_raw" / "zjui.intl.zju.edu.cn" / "notice" / "2026" / "fresh.html"
    assert target.is_file() and result["status"] == "placed" and result["index_row"] is True
    rows = [json.loads(line) for line in (crawl / "zju-sites" / "archive" / "zjui" / "_index.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[-1] == {"url": url, "title": "Fresh Notice", "rel": "zjui.intl.zju.edu.cn/notice/2026/fresh.html", "bytes": len(target.read_bytes()), "keys": []}
    # same bytes again → already-present, index unchanged
    again = add_material.add_material(_page(tmp_path), kind="page", site="zjui", title="Fresh Notice", url=url, crawl_root=crawl)
    assert again["status"] == "already-present" and "index_row" not in again
    assert len(rows) == len([json.loads(l) for l in (crawl / "zju-sites" / "archive" / "zjui" / "_index.jsonl").read_text(encoding="utf-8").splitlines()])


def test_cms_attachment_places_from_url(tmp_path, crawl, monkeypatch):
    monkeypatch.setattr(add_material, "INBOX", tmp_path / "inbox")
    docx = tmp_path / "handout.docx"
    docx.write_bytes(b"PK-zip")
    result = add_material.add_material(docx, kind="cms-attachment", site="ugrs",
                                       url="https://ugrs.zju.edu.cn/docs/2026/handout.docx", crawl_root=crawl)
    assert (crawl / "zju-sites" / "archive" / "ugrs" / "ugrs.zju.edu.cn" / "docs" / "2026" / "handout.docx").read_bytes() == b"PK-zip"
    assert result["status"] == "placed"


def test_validation_rejections(tmp_path, crawl):
    with pytest.raises(add_material.IngestError, match="unknown site"):
        add_material.add_material(_pdf(tmp_path), kind="pdf", site="mit", crawl_root=crawl)
    with pytest.raises(add_material.IngestError, match="unknown kind"):
        add_material.add_material(_pdf(tmp_path), kind="video", crawl_root=crawl)
    with pytest.raises(add_material.IngestError, match="accepts"):
        add_material.add_material(_page(tmp_path), kind="pdf", crawl_root=crawl)  # wrong ext for pdf kind
    with pytest.raises(add_material.IngestError, match="requires --url"):
        add_material.add_material(_page(tmp_path), kind="page", site="zjui", title="t", crawl_root=crawl)
    with pytest.raises(add_material.IngestError, match="requires --title"):
        add_material.add_material(_page(tmp_path), kind="page", site="zjui", url="https://zjui.intl.zju.edu.cn/x.html", crawl_root=crawl)
    with pytest.raises(add_material.IngestError, match="targets CMS sites"):
        add_material.add_material(_page(tmp_path), kind="page", site="sharepoint", url="https://x/y.html", title="t", crawl_root=crawl)
