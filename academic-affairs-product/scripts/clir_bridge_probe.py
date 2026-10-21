# -*- coding: utf-8 -*-
"""CLIR bridge probe (product venv): does the LLM query-optimization layer
close the cross-language retrieval gap?

For the verified Chinese questions of the generated set, run the product's
optimize_queries (cold cache — fresh calls) against the local model, then
call the runtime /v1/answer_context with the optimized retrieval_query and
score gold doc_rank / quote hit — directly comparable to the bare-harness
zh numbers (anchor@1 0.175 on this set).
"""
import asyncio
import json
import re
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from product.config import load_config
from product.providers import ApiProvider, _reset_query_cache

RUNTIME = "http://127.0.0.1:8772"
EVAL = Path(r"E:\Projects\zjui-product\eval-suite\data\benchmarks\corpus-eval\eval_sp_generated_v1.jsonl")
OUT = Path(r"E:\Projects\zjui-product\eval-suite\data\benchmarks\corpus-eval\results\qg_v1_sp\clir_bridge_probe.json")
TODAY = "2026-10-15"


async def main(limit: int = 60) -> None:
    rows = [json.loads(l) for l in EVAL.open(encoding="utf-8")]
    zh = [r for r in rows if r.get("verified") and r.get("gold") and r.get("language") == "zh"][:limit]
    config = load_config()
    provider = ApiProvider(config)
    _reset_query_cache()

    def key(text: str) -> str:
        return re.sub(r"\s+", "", str(text)).casefold()

    results = []
    async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
        for i, q in enumerate(zh, 1):
            queries = await provider.optimize_queries(q["question"])
            rq = " ".join(queries) if queries and queries != [q["question"]] else None
            body = {"question": q["question"], "today": q.get("today") or TODAY, "config": "active_ir", "k": 10, "pack_limit": 8}
            if rq:
                body["retrieval_query"] = rq
            resp = await client.post(f"{RUNTIME}/v1/answer_context", json=body)
            pack = resp.json()["evidence_pack"]
            evidence = pack.get("evidence", [])
            gold_docs = {g["doc_uid"] for g in q["gold"]}
            gold_quotes = [key(g["quote"]) for g in q["gold"]]
            doc_rank = next((n for n, ev in enumerate(evidence, 1) if ev.get("doc_id") in gold_docs), None)
            anchor_rank = None
            if doc_rank:
                for n, ev in enumerate(evidence, 1):
                    if ev.get("doc_id") in gold_docs and any(k and k in key(ev.get("text", "")) for k in gold_quotes):
                        anchor_rank = n
                        break
            results.append({"id": q["id"], "question": q["question"][:40], "optimized": bool(rq),
                            "queries": queries[:2], "doc_rank": doc_rank, "anchor_rank": anchor_rank,
                            "decision": resp.json()["decision"]["status"]})
            if i % 10 == 0:
                a1 = sum(1 for r in results if r["anchor_rank"] == 1) / len(results)
                d1 = sum(1 for r in results if r["doc_rank"] == 1) / len(results)
                print(f"[probe] {i}/{len(zh)} bridged doc@1={d1:.3f} anchor@1={a1:.3f} optimized={sum(1 for r in results if r['optimized'])}", flush=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    n = len(results)
    print(json.dumps({
        "n": n,
        "optimized_rate": sum(1 for r in results if r["optimized"]) / n,
        "bridged_doc@1": sum(1 for r in results if r["doc_rank"] == 1) / n,
        "bridged_anchor@1": sum(1 for r in results if r["anchor_rank"] == 1) / n,
        "bridged_anchor@5": sum(1 for r in results if r["anchor_rank"] and r["anchor_rank"] <= 5) / n,
    }, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 60))
