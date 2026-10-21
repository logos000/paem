"""Structured jsonl ask log — one line per /v1/ask, correlated with the corpus
runtime's own log via request_id.

P2: question text is redacted by default (emails / phones / student-id-like
digits) and truncated; set ``ASK_LOG_FULL=true`` to log verbatim during
debugging.  The file rotates by size with a bounded number of suffixes.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

_LOCK = threading.Lock()

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d{2,3[- ]?)?1[3-9]\d{9}(?!\d)")
STUDENT_ID_RE = re.compile(r"(?<!\d)3\d{9}(?!\d)")  # 10-digit ids starting with 3 (ZJU style)
QUESTION_MAX_CHARS = 120

_MAX_BYTES = 20 * 1024 * 1024
_KEEP = 3


def redact(text: str) -> str:
    text = EMAIL_RE.sub("[email]", str(text or ""))
    text = PHONE_RE.sub("[phone]", text)
    text = STUDENT_ID_RE.sub("[student-id]", text)
    return text[:QUESTION_MAX_CHARS]


def _log_path() -> Path:
    return Path(os.getenv("ASK_LOG", str(REPO_ROOT / "logs" / "ask.jsonl")))


def _full_questions_enabled() -> bool:
    return os.getenv("ASK_LOG_FULL", "").strip().lower() in ("1", "true", "yes")


def _rotate_if_needed(path: Path) -> None:
    try:
        if path.stat().st_size < _MAX_BYTES:
            return
    except OSError:
        return
    for index in range(_KEEP - 1, 0, -1):
        source, target = path.with_suffix(f".jsonl.{index}"), path.with_suffix(f".jsonl.{index + 1}")
        if source.exists():
            source.replace(target)
    path.replace(path.with_suffix(".jsonl.1"))


def log_ask(record: dict[str, Any]) -> None:
    if "question" in record and not _full_questions_enabled():
        record = {**record, "question": redact(record["question"])}
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **record}
    path = _log_path()
    try:
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            _rotate_if_needed(path)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        # Logging must never break the answer path; the request already
        # carries a request_id for correlation via the runtime's own log.
        pass
