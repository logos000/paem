#!/usr/bin/env python3
"""Stage 5 V2: the unattended ingest pipeline.

Polls ``inbox/jobs/*.json`` (job = {id, file, kind, site, title, url,
published, path}; the file is the uploaded material) and, one job at a time:

    add_material --job …      (placement + idempotent snapshot re-freeze)
    update_cycle --switch     (incremental build → conflicts → release gate
                               → atomic switch; old build keeps serving on
                               any failure)

Every transition appends one event to ``inbox/status.jsonl``:
``{ts, job_id, stage, detail}`` with stages
``received → placed → published | failed``.  Processed job files move to
``jobs/done/`` (or stay in ``jobs/failed/`` with the reason).

Run standalone (``python scripts/watch_inbox.py``) or via the product
launcher.  ``--once`` drains the queue and exits (tests / manual kicks).
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
import time

PROVIDER = Path(__file__).resolve().parents[1]
INBOX = Path(os.environ.get("WATCH_INBOX", str(PROVIDER / "inbox")))
BUILDER = PROVIDER.parent / "llm-corpus-builder"
BUILDER_PY = BUILDER / ".venv" / "Scripts" / "python.exe"
ADD_MATERIAL = PROVIDER / "scripts" / "add_material.py"
UPDATE_CYCLE = BUILDER / "scripts" / "update_cycle.py"
STATUS = INBOX / "status.jsonl"


def _event(job_id: str, stage: str, detail: str = "", **extra) -> None:
    record = {"ts": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"), "job_id": job_id, "stage": stage}
    if detail:
        record["detail"] = detail[:1000]
    record.update(extra)
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    with STATUS.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


class Watcher:
    def __init__(self, *, add_command: list[str] | None = None, cycle_command: list[str] | None = None, annotate: bool = False, echo=print):
        self.add_command = add_command or [sys.executable, str(ADD_MATERIAL), "--job", "{job}"]
        cycle = cycle_command or [str(BUILDER_PY), str(UPDATE_CYCLE), "--switch"]
        if annotate:
            cycle = cycle + ["--annotate"]
        self.cycle_command = cycle
        self.echo = echo

    @staticmethod
    def _run(command: list[str], job: Path) -> tuple[int, str]:
        rendered = [str(part).replace("{job}", str(job)) for part in command]
        completed = subprocess.run(rendered, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(BUILDER))
        output = (completed.stdout or "") + (completed.stderr or "")
        return completed.returncode, output.strip()

    def process(self, job_path: Path) -> bool:
        spec = json.loads(job_path.read_text(encoding="utf-8"))
        job_id = str(spec.get("id") or job_path.stem)
        self.echo(f"[watch] job {job_id}: start")
        _event(job_id, "received", file=str(spec.get("file", "")))
        code, output = self._run(self.add_command, job_path)
        if code != 0:
            _event(job_id, "failed", f"add_material exit {code}: {output[-400:]}")
            return self._fail(job_path, job_id)
        _event(job_id, "placed", "material in tree + snapshot re-frozen")
        code, output = self._run(self.cycle_command, job_path)
        if code != 0:
            _event(job_id, "failed", f"update_cycle exit {code}: {output[-600:]} — old build keeps serving")
            return self._fail(job_path, job_id)
        match = re.search(r"'build_id':\s*'([^']+)'", output) or re.search(r'"build_id":\s*"([^"]+)"', output)
        _event(job_id, "published", output[-300:], **({"build_id": match.group(1)} if match else {}))
        done = INBOX / "jobs" / "done"
        done.mkdir(parents=True, exist_ok=True)
        job_path.rename(done / job_path.name)
        self.echo(f"[watch] job {job_id}: PUBLISHED{(' build ' + match.group(1)) if match else ''}")
        return True

    def _fail(self, job_path: Path, job_id: str) -> bool:
        failed = INBOX / "jobs" / "failed"
        failed.mkdir(parents=True, exist_ok=True)
        job_path.rename(failed / job_path.name)
        self.echo(f"[watch] job {job_id}: FAILED (see status.jsonl)")
        return False

    def run_once(self) -> int:
        jobs_dir = INBOX / "jobs"
        if not jobs_dir.is_dir():
            return 0
        pending = sorted(p for p in jobs_dir.glob("*.json"))
        for job in pending:
            self.process(job)
        return len(pending)

    def run_forever(self, interval: float) -> None:
        self.echo(f"[watch] polling {INBOX / 'jobs'} every {interval}s")
        while True:
            try:
                self.run_once()
            except Exception as error:  # a broken job must not kill the watcher
                self.echo(f"[watch] loop error: {type(error).__name__}: {error}")
            time.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--once", action="store_true", help="drain the queue and exit")
    parser.add_argument("--annotate", action="store_true", help="also run LLM annotation on changed docs (API whitelist applies)")
    parser.add_argument("--add-command", nargs="+", default=None, help="override the add_material command ({job} placeholder; tests)")
    parser.add_argument("--cycle-command", nargs="+", default=None, help="override the update_cycle command (tests)")
    args = parser.parse_args(argv)

    watcher = Watcher(add_command=args.add_command, cycle_command=args.cycle_command, annotate=args.annotate)
    if args.once:
        watcher.run_once()
        return 0
    watcher.run_forever(args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
