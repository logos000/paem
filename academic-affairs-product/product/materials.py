"""Material import endpoints (Stage 5 V3).

The product only HANDS WORK to the ingest pipeline: uploads land in the
provider inbox (file + job json) and the watcher does the rest; status is
read back from the watcher's status.jsonl.  The product never builds.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import HTTPException, UploadFile

# Mirror of raw-material-provider scripts/add_material.py (kept in sync on
# purpose — the product does not import the provider).
SITES = ("sharepoint", "zjui", "ugrs", "grs", "zju", "news", "libweb", "iczu", "zibs")
KINDS = {
    "pdf": {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".xlsm", ".txt", ".ppt", ".pptx"},
    "page": {".html", ".htm"},
    "cms-attachment": {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".xlsm", ".txt", ".ppt", ".pptx"},
}
MAX_BYTES = 100 * 1024 * 1024
STAGE_LABEL = {
    "queued": "待处理",
    "received": "处理中",
    "placed": "构建发布中",
    "published": "已发布",
    "failed": "失败",
}


def _inbox() -> Path:
    return Path(os.environ.get("INBOX_DIR", r"E:/Projects/zjui-product/raw-material-provider/inbox"))


def validate_form(*, kind: str, site: str, filename: str, url: str, title: str) -> None:
    if kind not in KINDS:
        raise HTTPException(status_code=400, detail=f"kind 必须是 {list(KINDS)}")
    if site not in SITES:
        raise HTTPException(status_code=400, detail=f"site 必须是 {list(SITES)}")
    ext = Path(filename).suffix.lower()
    if ext not in KINDS[kind]:
        raise HTTPException(status_code=400, detail=f"{kind} 类型不接受 {ext or '(无扩展名)'} 文件")
    if kind == "page":
        if not url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="网页通知必须填写真实来源 URL")
        if not title.strip():
            raise HTTPException(status_code=400, detail="网页通知必须填写标题")
        if site == "sharepoint":
            raise HTTPException(status_code=400, detail="网页通知仅支持 CMS 站点")


async def save_upload(upload: UploadFile, *, kind: str, site: str, title: str, url: str, published: str, path: str) -> dict[str, Any]:
    validate_form(kind=kind, site=site, filename=upload.filename or "", url=url, title=title)
    job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    inbox = _inbox()
    uploads = inbox / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    # Keep the ORIGINAL filename (job-prefixed for uniqueness): the ingest
    # pipeline derives the document title from it when nothing better exists.
    safe_name = "".join(ch if ch not in '\\/:*?"<>|' else "_" for ch in (upload.filename or "material"))
    target = uploads / f"{job_id}-{safe_name}"
    size = 0
    with target.open("wb") as handle:
        while chunk := await upload.read(1 << 20):
            size += len(chunk)
            if size > MAX_BYTES:
                target.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="文件超过 100MB 上限")
            handle.write(chunk)
    spec = {"id": job_id, "file": str(target), "filename": upload.filename or target.name, "kind": kind, "site": site, "title": title, "url": url, "published": published, "path": path}
    jobs = inbox / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    (jobs / f"{job_id}.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "job_id": job_id, "filename": upload.filename, "status": "queued"}


def list_materials(limit: int = 50) -> dict[str, Any]:
    inbox = _inbox()
    items: dict[str, dict[str, Any]] = {}
    for job_file in sorted((inbox / "jobs").glob("*.json"), reverse=True)[:limit]:
        try:
            spec = json.loads(job_file.read_text(encoding="utf-8"))
        except ValueError:
            continue
        items[spec.get("id", job_file.stem)] = {"job_id": spec.get("id", job_file.stem), "file": spec.get("filename") or Path(spec.get("file", "")).name,
                                                "title": spec.get("title", ""), "kind": spec.get("kind", ""), "stage": "queued", "ts": ""}
    status_path = inbox / "status.jsonl"
    if status_path.is_file():
        for line in status_path.read_text(encoding="utf-8").splitlines()[-limit * 3:]:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            entry = items.setdefault(event.get("job_id", ""), {"job_id": event.get("job_id", ""), "stage": "queued", "ts": ""})
            entry["stage"] = event["stage"]
            entry["ts"] = event.get("ts", "")
            if event.get("build_id"):
                entry["build_id"] = event["build_id"]
            if event.get("detail"):
                entry["detail"] = event["detail"]
    pending = sum(1 for item in items.values() if item["stage"] in ("queued", "received", "placed"))
    return {
        "items": sorted(items.values(), key=lambda item: item.get("ts", ""), reverse=True)[:limit],
        "pending": pending,
        "watcher_hint": "收到上传后由 watch_inbox 自动处理（启动器会一并拉起；也可手动 python scripts/watch_inbox.py --once）",
    }
