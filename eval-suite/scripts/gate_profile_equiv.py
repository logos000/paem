# -*- coding: utf-8 -*-
"""Zero-drift gate: replay benchmark questions directly against the runtime
(no LLM, no gateway) and compare evidence packs with a baseline snapshot.

Joins by QUESTION TEXT — generated-set ids once collided across sources
(c1 qg-* vs foldcap qg-*, fixed 2026-10-16); text is the stable key.

Compares per row: evidence doc/block sequence, decision, coverage,
section_rollup presence, usable_evidence_ids.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "data/benchmarks/corpus-eval/benchmark_superset_v1_275.jsonl"


def pack_signature(payload: dict) -> dict:
    ep = payload.get("evidence_pack") or {}
    return {
        "docs": [e.get("doc_id") for e in ep.get("evidence", [])],
        "blocks": [e.get("chunk_id") for e in ep.get("evidence", [])],
        "decision": ((payload.get("decision") or {}).get("status"), (payload.get("decision") or {}).get("abstain")),
        "coverage": ep.get("coverage"),
        "rollup": bool(ep.get("section_rollup")),
        "usable": ep.get("usable_evidence_ids"),
        "pack_hash": payload.get("pack_hash"),
    }


def stored_signature(row: dict) -> dict:
    ep = row.get("evidence_pack") or {}
    dec = row.get("decision") or {}
    return {
        "docs": [e.get("doc_id") for e in ep.get("evidence", [])],
        "blocks": [e.get("chunk_id") for e in ep.get("evidence", [])],
        "decision": (dec.get("status"), dec.get("abstain")),
        "coverage": ep.get("coverage"),
        "rollup": bool(ep.get("section_rollup")),
        "usable": ep.get("usable_evidence_ids"),
        "pack_hash": row.get("pack_hash"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8773")
    parser.add_argument("--config", default="active_hybrid")
    parser.add_argument("--today", default="2026-10-15")
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--ids", default="", help="comma list of bench ids; default all")
    args = parser.parse_args()

    def _stored_question(row: dict) -> str:
        rounds = ((row.get("retrieval_trace") or {}).get("rounds")) or []
        return str(rounds[0].get("query") or "") if rounds else ""

    base = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    stored = {_stored_question(r): r for r in base["rows"] if not r.get("error") and _stored_question(r)}
    bench = [json.loads(l) for l in open(BENCH, encoding="utf-8")]
    wanted = {x.strip() for x in args.ids.split(",") if x.strip()}
    todo = [(b["id"], b["question"]) for b in bench
            if b["question"] in stored and (not wanted or b["id"] in wanted)]

    def ask(item):
        qid, question = item
        for attempt in range(3):
            try:
                r = httpx.post(f"{args.url}/v1/answer_context",
                               json={"question": question, "config": args.config, "today": args.today},
                               timeout=120)
                if r.status_code == 200:
                    return qid, question, pack_signature(r.json()), None
                return qid, question, None, f"HTTP {r.status_code}"
            except Exception as error:
                if attempt == 2:
                    return qid, question, None, f"{type(error).__name__}"
                time.sleep(2)
        return qid, question, None, "unreachable"

    diffs = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for k, (qid, question, sig, err) in enumerate(pool.map(ask, todo), 1):
            if err:
                diffs.append((qid, f"request failed: {err}"))
            else:
                want = stored_signature(stored[question])
                if sig != want:
                    changed = [key for key in want if sig.get(key) != want.get(key)]
                    diffs.append((qid, ",".join(changed)))
            if k % 40 == 0:
                print(f"[gate] {k}/{len(todo)}, diffs so far {len(diffs)}", flush=True)

    print(f"RESULT: {len(todo) - len(diffs)}/{len(todo)} identical, {len(diffs)} differ")
    for qid, what in diffs[:15]:
        print(f"  {qid}: {what}")
    sys.exit(0 if not diffs else 1)


if __name__ == "__main__":
    main()
