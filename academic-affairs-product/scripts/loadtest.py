"""Concurrency load test (P2): N parallel asks against the product/runtime.

Verifies the capacity guards behave: no unbounded queueing, no uncontrolled
5xx — under overload the only allowed failure is 429 (product rate limit) or
503 "at capacity" (runtime semaphore), never a stack-trace 500.

Usage:
    python scripts/loadtest.py --url http://127.0.0.1:8770 --concurrency 10
    python scripts/loadtest.py --url http://127.0.0.1:8772 --concurrency 50 --target runtime
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

QUESTION = "ZJUI 2026 fall undergraduate course withdrawal deadline?"


async def one(client: httpx.AsyncClient, url: str, endpoint: str) -> tuple[int, float]:
    started = time.perf_counter()
    if endpoint == "runtime":
        response = await client.post("/v1/answer_context", json={"question": QUESTION})
    else:
        response = await client.post("/v1/ask", json={"question": QUESTION})
    return response.status_code, (time.perf_counter() - started) * 1000


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8770")
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--target", choices=("product", "runtime"), default="product")
    args = parser.parse_args()

    started = time.perf_counter()
    async with httpx.AsyncClient(base_url=args.url, timeout=180, limits=httpx.Limits(max_connections=args.concurrency)) as client:
        results = await asyncio.gather(*(one(client, args.url, args.target) for _ in range(args.concurrency)), return_exceptions=True)
    wall = time.perf_counter() - started

    codes: Counter[str] = Counter()
    latencies: list[float] = []
    errors: list[str] = []
    for item in results:
        if isinstance(item, Exception):
            codes[f"exception:{type(item).__name__}"] += 1
            errors.append(str(item)[:120])
        else:
            codes[str(item[0])] += 1
            latencies.append(item[1])

    def pct(q: float) -> float:
        if not latencies:
            return 0.0
        ordered = sorted(latencies)
        return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 1)

    print(f"target={args.target} n={args.concurrency} wall={wall:.1f}s")
    print(f"codes: {dict(codes)}")
    if latencies:
        print(f"latency ms: p50={pct(0.5)} p95={pct(0.95)} max={max(latencies):.0f}")
    bad = [code for code in codes if not (code.startswith("2") or code in ("429", "503"))]
    if bad:
        print(f"UNEXPECTED FAILURE CODES: {bad}")
        for error in errors[:5]:
            print(" -", error)
        return 1
    if codes.get("503"):
        print("note: some requests hit the capacity guard (503) — bounded, by design")
    print("LOADTEST OK" if not bad else "LOADTEST FAILED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
