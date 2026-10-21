"""V2 watcher state-machine tests (stub commands, no real build)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import watch_inbox  # noqa: E402


def _ok_add():
    return [sys.executable, "-c", "print('placed ok')"]


def _ok_cycle():
    return [sys.executable, "-c", "print(\"[switch] (200, {'previous': 'default', 'current': 'b1', 'build_id': 'build-2026-new'})\")"]


def _failing(exit_code: int, message: str):
    return [sys.executable, "-c", f"import sys; print({message!r}, file=sys.stderr); sys.exit({exit_code})"]


@pytest.fixture()
def inbox(tmp_path, monkeypatch):
    monkeypatch.setattr(watch_inbox, "INBOX", tmp_path / "inbox")
    monkeypatch.setattr(watch_inbox, "STATUS", tmp_path / "inbox" / "status.jsonl")
    jobs = tmp_path / "inbox" / "jobs"
    jobs.mkdir(parents=True)
    (jobs / "job-001.json").write_text(json.dumps({"id": "job-001", "file": "x.pdf", "kind": "pdf"}), encoding="utf-8")
    return tmp_path / "inbox"


def _events(inbox) -> list[dict]:
    return [json.loads(line) for line in (inbox / "status.jsonl").read_text(encoding="utf-8").splitlines()]


def test_success_path_publishes_and_moves_to_done(inbox):
    watcher = watch_inbox.Watcher(add_command=_ok_add(), cycle_command=_ok_cycle(), echo=lambda *_: None)
    assert watcher.run_once() == 1
    events = _events(inbox)
    assert [event["stage"] for event in events] == ["received", "placed", "published"]
    assert events[-1]["build_id"] == "build-2026-new"
    assert (inbox / "jobs" / "done" / "job-001.json").is_file()
    assert not (inbox / "jobs" / "job-001.json").exists()


def test_add_failure_marks_failed_and_keeps_old_build_semantics(inbox):
    watcher = watch_inbox.Watcher(add_command=_failing(1, "bad site"), cycle_command=_ok_cycle(), echo=lambda *_: None)
    watcher.run_once()
    events = _events(inbox)
    assert [event["stage"] for event in events] == ["received", "failed"]
    assert "bad site" in events[-1]["detail"]
    assert (inbox / "jobs" / "failed" / "job-001.json").is_file()
    assert not (inbox / "jobs" / "done").exists()  # publish never ran


def test_cycle_failure_marks_failed_after_placed(inbox):
    watcher = watch_inbox.Watcher(add_command=_ok_add(), cycle_command=_failing(1, "RELEASE GATE FAILED"), echo=lambda *_: None)
    watcher.run_once()
    events = _events(inbox)
    assert [event["stage"] for event in events] == ["received", "placed", "failed"]
    assert "RELEASE GATE FAILED" in events[-1]["detail"] and "old build" in events[-1]["detail"]
    assert (inbox / "jobs" / "failed" / "job-001.json").is_file()


def test_run_once_empty_is_noop(inbox):
    (inbox / "jobs" / "job-001.json").unlink()
    watcher = watch_inbox.Watcher(add_command=_ok_add(), cycle_command=_ok_cycle(), echo=lambda *_: None)
    assert watcher.run_once() == 0
    assert not (inbox / "status.jsonl").exists()
