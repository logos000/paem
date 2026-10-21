"""Session store (P5): multi-turn history for display and audit ONLY.

Each turn still goes to the builder as an independent question with its own
audited user_context — history text is never fed back as evidence, so the
applicability/version rules cannot be bypassed by conversation.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
_LOCK = threading.Lock()


def _store_path() -> Path:
    return Path(os.getenv("SESSION_STORE", str(REPO_ROOT / "logs" / "sessions.jsonl")))


def new_session_id() -> str:
    return "s-" + uuid.uuid4().hex[:12]


def append_turn(session_id: str, record: dict[str, Any]) -> None:
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "session_id": session_id, **record}
    path = _store_path()
    try:
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def history(session_id: str, limit: int = 50) -> list[dict[str, Any]]:
    path = _store_path()
    if not path.is_file():
        return []
    turns: list[dict[str, Any]] = []
    with _LOCK:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("session_id") == session_id:
                    turns.append(row)
    return turns[-limit:]


# ------------------------------------------------------------------ feedback

def append_feedback(record: dict[str, Any]) -> None:
    path = Path(os.getenv("FEEDBACK_STORE", str(REPO_ROOT / "logs" / "feedback.jsonl")))
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **record}
    try:
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def feedback_rows(limit: int = 50) -> list[dict[str, Any]]:
    """Newest-first feedback records (reviewer round 1, item 5: a 👎 must be
    reviewable, not just counted)."""
    path = Path(os.getenv("FEEDBACK_STORE", str(REPO_ROOT / "logs" / "feedback.jsonl")))
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with _LOCK:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    return rows[-limit:][::-1]


def find_turn(request_id: str) -> dict[str, Any] | None:
    """The one session turn with this request_id (feedback join key), or None."""
    path = _store_path()
    if not path.is_file():
        return None
    with _LOCK:
        with path.open(encoding="utf-8") as handle:
            for line in reversed(handle.readlines()):
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("request_id") == request_id:
                    return row
    return None
