"""P7 update cycle: one command from changed raw materials to a released build.

    snapshot re-freeze (idempotent)  →  incremental pipeline build (--base)
    →  conflict scan  →  release gate (runtime smoke + regression set)
    →  admin load + atomic switch (opt-in via --switch)

Every step is idempotent and individually skippable; nothing is deleted
unless --prune is given AND --yes confirms (the frozen master build and the
current LATEST are never pruned).

Usage:
    python scripts/update_cycle.py --sites sharepoint --ocr
    python scripts/update_cycle.py --skip-snapshot --switch
    python scripts/update_cycle.py --prune 12            # dry-run list
    python scripts/update_cycle.py --prune 12 --yes      # actually delete
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROVIDER = ROOT.parent / "raw-material-provider"
DEFAULT_SNAPSHOT = PROVIDER / "raw-snapshot" / "snap-20261014-v1"
FROZEN_MASTER = "20261014-020301-90c398"  # 92-question frozen results live here
#: The crawl tree is NOT inside the suite folder — absolute default, env-overridable.
CRAWL_ROOT = Path(os.environ.get("CRAWL_ROOT", r"E:\Projects\浙大网站爬取"))
PY = sys.executable
PYENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(ROOT)}


def run(label: str, args: list[str], env: dict | None = None) -> int:
    print(f"[update_cycle] {label}: {' '.join(str(a) for a in args)}", flush=True)
    return subprocess.run([str(a) for a in args], env=env or PYENV, cwd=str(ROOT)).returncode


def latest_build_id(out_root: Path) -> str:
    return (out_root / "LATEST").read_text(encoding="utf-8").strip()


def http(method: str, url: str, payload: dict | None = None) -> tuple[int, dict]:
    import httpx

    response = httpx.request(method, url, json=payload, timeout=180)
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, {}


def prune(out_root: Path, keep: int, yes: bool) -> int:
    latest = latest_build_id(out_root)
    builds = sorted((p for p in out_root.iterdir() if (p / "manifest.json").is_file()), key=lambda p: p.name)
    victims = [p for p in builds[:-keep] if p.name != latest and p.name != FROZEN_MASTER]
    print(f"[prune] {len(builds)} builds; keep {keep}; latest={latest}; frozen master always kept")
    for path in victims:
        print(f"[prune] {'DELETE' if yes else 'dry-run'} {path.name}")
    if yes and victims:
        import shutil

        for path in victims:
            shutil.rmtree(path)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sites", default="sharepoint")  # SP-domain scope ruling 2026-10-17: product/eval are sharepoint-only
    parser.add_argument("--ocr", action="store_true")
    parser.add_argument("--snapshot-dir", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--skip-snapshot", action="store_true")
    parser.add_argument("--skip-build", action="store_true", help="release an existing build instead of building")
    parser.add_argument("--build-id", default="", help="use this build (with --skip-build) instead of a fresh one")
    parser.add_argument("--runtime-url", default="http://127.0.0.1:8772")
    parser.add_argument("--product-url", default="http://127.0.0.1:8770")
    parser.add_argument("--regression", type=Path, default=ROOT.parent / "eval-suite" / "data" / "benchmarks" / "corpus-eval" / "product_regression.jsonl")
    parser.add_argument("--switch", action="store_true", help="gate + atomically switch the runtime to the new build")
    parser.add_argument("--prune", type=int, default=0, help="retention: keep the newest N builds")
    parser.add_argument("--yes", action="store_true", help="confirm destructive prune")
    args = parser.parse_args()

    out_root = ROOT / "data" / "corpus"

    if args.prune:
        return prune(out_root, args.prune, args.yes)

    # 1. idempotent snapshot re-freeze
    if not args.skip_snapshot:
        code = run("snapshot re-freeze", [PY, PROVIDER / "snapshot.py", "--crawl-root", CRAWL_ROOT, "--snapshot-id", args.snapshot_dir.name])
        if code != 0:
            return code
    elif not args.snapshot_dir.is_dir():
        raise SystemExit(f"--skip-snapshot but {args.snapshot_dir} does not exist")

    # 2. incremental build on top of the current LATEST
    if not args.skip_build:
        base = latest_build_id(out_root)
        code = run("incremental build", [PY, "-m", "pipeline", "--sites", args.sites, "--snapshot", args.snapshot_dir,
                                         "--base", base, *(["--ocr"] if args.ocr else [])])
        if code != 0:
            return code
        build_id = latest_build_id(out_root)
    else:
        build_id = args.build_id or latest_build_id(out_root)

    # 3. conflict scan for the new build
    code = run("conflict scan", [PY, "-m", "curate.conflicts", "--corpus", str(out_root / build_id)])
    if code != 0:
        return code

    # 4-5. release gate + atomic switch (opt-in)
    if args.switch:
        gate = ROOT.parent / "academic-affairs-product" / "scripts" / "release_gate.py"
        product_py = ROOT.parent / "academic-affairs-product" / ".venv" / "Scripts" / "python.exe"
        load = http("POST", f"{args.runtime_url}/admin/builds", {"build_dir": str(out_root / build_id)})
        print(f"[switch] admin load -> {load}")
        slot = None
        for _ in range(120):
            status = http("GET", f"{args.runtime_url}/admin/builds")[1]
            slot = next((s for s in status.get("builds", []) if s["key"] == build_id), None)
            if slot and not slot["loading"]:
                break
            time.sleep(3)  # background load of the LLM-augmented index takes ~1 min; without the sleep the gate races it (caught in the V5 demo)
        if not slot or slot["error"]:
            raise SystemExit(f"build {build_id} failed to load: {slot}")
        code = run("release gate", [product_py, gate, "--key", build_id, "--runtime-url", args.runtime_url,
                                    "--product-url", args.product_url, "--regression", args.regression])
        if code != 0:
            print("[switch] RELEASE GATE FAILED — runtime keeps serving the previous build")
            return code
        switched = http("POST", f"{args.runtime_url}/admin/switch", {"key": build_id})
        print(f"[switch] {switched}")
        return 0 if switched[0] == 200 else 1

    print(f"[update_cycle] build {build_id} ready (gate+switch skipped — pass --switch to release)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
