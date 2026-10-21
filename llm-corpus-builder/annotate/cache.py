"""Disk cache for LLM annotation responses (offline cost control, per doc hash).

Cache key = sha256(payload + task + model + prompt_version), so a prompt or
model bump automatically invalidates old entries (PLAN.md §风险防护:
LLM 仅离线、按文档哈希缓存).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["DEFAULT_CACHE_DIR", "cache_key", "load", "save"]

DEFAULT_CACHE_DIR = Path("data/llm-cache/annotations")


def cache_key(payload: str, task: str, model: str, prompt_version: str, prompt_hash: str | None = None) -> str:
    """Return the sha256 cache key for one (payload, task, model, prompt_version[, prompt_hash]).

    ``prompt_hash`` (sha256 of the rendered prompt text) is folded in only when
    supplied: same-name "v1" with an edited prompt body is exactly the staleness
    this guards against, but folding it in unconditionally would orphan every
    entry cached under the original v1 key — so existing prompt versions keep
    their digest and new versions opt in at the call site.
    """
    blob = {"payload": payload, "task": task, "model": model, "prompt_version": prompt_version}
    if prompt_hash:
        blob["prompt_hash"] = prompt_hash
    return hashlib.sha256(json.dumps(blob, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _resolve(cache_dir: str | Path | None = None) -> Path:
    return Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR


def path_for(key: str, cache_dir: str | Path | None = None) -> Path:
    return _resolve(cache_dir) / f"{key}.json"


def load(key: str, cache_dir: str | Path | None = None) -> dict[str, Any] | None:
    """Return the cached record (with ``response`` inside) or None on miss."""
    path = path_for(key, cache_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save(
    key: str,
    obj: Any,
    *,
    model: str = "",
    prompt_version: str = "",
    cache_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Persist ``obj`` under ``key``; the record carries key/model/prompt_version/created_at."""
    directory = _resolve(cache_dir)
    directory.mkdir(parents=True, exist_ok=True)
    record = {
        "key": key,
        "model": model,
        "prompt_version": prompt_version,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "response": obj,
    }
    path = directory / f"{key}.json"
    tmp = directory / f"{key}.json.tmp"
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    return record
