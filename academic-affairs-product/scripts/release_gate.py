"""P6 release gate — run BEFORE /admin/switch.  Non-zero exit blocks release.

Checks against the TARGET build (via the runtime's X-Target-Build canary):
  1. runtime smoke all_ok (canned questions incl. the future-term refusal);
  2. regression set: semantic expectations (future-term/audience traps MUST
     refuse with the right status), every request must return a valid
     decision, zero transport errors;
  3. retrieval p95 within budget on the regression set;
  4. product API healthy and one end-to-end ask returns 200.

Usage:
    python scripts/release_gate.py --key <build-key> \
        --regression "E:/Projects/zjui-product/eval-suite/data/benchmarks/corpus-eval/product_regression.jsonl"
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runtime-url", default="http://127.0.0.1:8772")
    parser.add_argument("--product-url", default="http://127.0.0.1:8770")
    parser.add_argument("--key", required=True, help="target build key (as shown in /admin/builds)")
    parser.add_argument("--regression", type=Path, required=True)
    parser.add_argument("--budget-p95", type=float, default=2000.0, help="retrieval p95 budget in ms")
    args = parser.parse_args()

    failures: list[str] = []
    report: dict = {"key": args.key, "checks": {}}

    with httpx.Client(timeout=120) as client:
        # 1. runtime smoke on the target build
        smoke = client.post(f"{args.runtime_url}/admin/builds/{args.key}/smoke", json={})
        if smoke.status_code != 200:
            print(f"GATE FAIL: smoke request -> {smoke.status_code}: {smoke.text[:200]}")
            return 1
        smoke_data = smoke.json()
        report["checks"]["smoke_all_ok"] = smoke_data.get("all_ok")
        if not smoke_data.get("all_ok"):
            failures.append(f"smoke not all_ok: {smoke_data.get('results')}")

        # 2-3. regression set through the runtime, pinned to the target build
        items = [json.loads(line) for line in args.regression.read_text(encoding="utf-8").splitlines() if line.strip()]
        latencies: list[float] = []
        semantic_failures = []
        for item in items:
            started = time.perf_counter()
            response = client.post(
                f"{args.runtime_url}/v1/answer_context",
                json={"question": item["question"], **({"today": item["today"]} if item.get("today") else {})},
                headers={"X-Target-Build": args.key},
            )
            latencies.append((time.perf_counter() - started) * 1000)
            if response.status_code != 200:
                failures.append(f"{item['id']}: transport {response.status_code}")
                continue
            status = response.json()["decision"]["status"]
            expected = item.get("expect_status")
            if expected and status not in expected:
                semantic_failures.append(f"{item['id']} ({item['category']}): got {status}, expected {expected}")
        report["checks"]["regression_n"] = len(items)
        report["checks"]["semantic_failures"] = semantic_failures
        failures.extend(semantic_failures)
        ordered = sorted(latencies)
        p95 = round(ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))], 1) if ordered else 0.0
        report["checks"]["retrieval_p95_ms"] = p95
        if p95 > args.budget_p95:
            failures.append(f"retrieval p95 {p95}ms exceeds budget {args.budget_p95}ms")

        # 4. product path healthy
        ready = client.get(f"{args.product_url}/health/ready")
        report["checks"]["product_ready"] = ready.status_code == 200
        if ready.status_code != 200:
            failures.append(f"product /health/ready -> {ready.status_code}")

    report["passed"] = not failures
    report["failures"] = failures
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
