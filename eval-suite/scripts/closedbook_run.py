# -*- coding: utf-8 -*-
"""Closed-book LLM baseline (2026-10-16): answer the benchmark with NO
retrieval at all — the bare commercial-assistant form.  Writes an e2e-shaped
snapshot so the standard judge scores it (groundedness/citation will be
null — citations are structurally impossible; correctness/completeness tell
the hallucination story).

Usage: python scripts/closedbook_run.py --eval <bench> --out <snapshot.json>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")  # QUESTIONGEN_API_KEY lives in the suite .env
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PROMPT = """你是高校教务问答助手。仅凭你自己的知识回答下面的问题。
规则：
1. 如果不确定或不知道，必须明确说"我不知道/无法确定"，禁止编造具体的数字、日期、课程、政策条款
2. 中文问题用中文回答，英文问题用英文回答
3. 直接给出答案，不要长篇铺垫

问题：{question}"""


def _client_env() -> tuple[str, str, str]:
    base = os.environ.get("LLM_BASE_URL", "https://your-gateway.example.com/v1")
    key = os.environ.get("LLM_API_KEY") or os.environ.get("QUESTIONGEN_API_KEY", "")
    if not key:
        raise RuntimeError("no gateway key: set QUESTIONGEN_API_KEY or LLM_API_KEY")
    model = os.environ.get("LLM_MODEL", "deepseek-v4-flash-780k-local")
    return base, key, model


def ask_one(question: str) -> str:
    base, key, model = _client_env()
    body = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT.format(question=question)}],
        "temperature": 0,
        "max_tokens": 600,
        "stream": True,
        "thinking": {"type": "disabled"},
    }
    for attempt in range(4):
        try:
            text = ""
            with httpx.stream("POST", f"{base}/chat/completions", headers={"Authorization": f"Bearer {key}"},
                              json=body, timeout=180) as resp:
                if resp.status_code == 429:
                    time.sleep(20 + attempt * 20)
                    continue
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload = line[6:]
                    if payload.strip() == "[DONE]":
                        break
                    try:
                        delta = json.loads(payload)["choices"][0]["delta"]
                        text += delta.get("content") or ""
                    except Exception:
                        continue
            return text.strip()
        except Exception:
            if attempt == 3:
                return ""
            time.sleep(5 + attempt * 10)
    return ""


def _write(out_path: Path, rows: list[dict]) -> None:
    snapshot = {"meta": {"label": "closedbook", "url": "gateway-direct", "n": len(rows),
                         "providers": ["closed-book-llm"], "snapshot_version": 3},
                "rows": rows}
    out_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--eval", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=20)
    args = parser.parse_args()

    bench = [json.loads(l) for l in open(args.eval, encoding="utf-8")]
    done: dict[str, dict] = {}
    if args.out.is_file():
        done = {r["id"]: r for r in json.loads(args.out.read_text(encoding="utf-8"))["rows"]}
        print(f"resume: {len(done)} already answered")

    todo = [b for b in bench if b["id"] not in done]
    rows: list[dict] = []

    def run(item):
        started = time.perf_counter()
        answer = ask_one(item["question"])
        return item, answer, (time.perf_counter() - started) * 1000

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run, item) for item in todo]
        for k, fut in enumerate(as_completed(futures), 1):
            item, answer, elapsed = fut.result()
            abstained = any(marker in answer for marker in ("不知道", "无法确定", "I don't know")) or "not sure" in answer.lower()
            rows.append({
                "id": item["id"], "category": item.get("category", ""), "level": item.get("level"),
                "language": item.get("language"), "qshape": item.get("qshape", ""),
                "source_set": item.get("source_set", ""), "answerable": bool(item.get("gold")),
                "answer_text": answer, "answer_disposition": "evidence_refusal" if abstained else "answer",
                "abstained": abstained, "cites_evidence": False, "grounded": False,
                "numeric_fidelity": True, "latency_ms": round(elapsed, 1),
                "claims": [], "citations": [], "citation_map": {},
                "decision": {"status": "sufficient", "abstain": False}, "generation": {"mode": "closedbook"},
                "evidence_pack": {"evidence": [], "usable_evidence_ids": [], "requirements": [],
                                  "evidence_status": "sufficient", "abstain": False, "abstain_reason": "",
                                  "coverage": 0.0, "conflicts": [], "fact_conflicts": []},
                "retrieval_trace": {"strategy": "closed_book", "queries": [], "rounds": []},
            })
            if k % 20 == 0:
                print(f"[closedbook] {k}/{len(todo)}", flush=True)
                _write(args.out, rows + list(done.values()))

    _write(args.out, rows + list(done.values()))
    print(f"done: {len(rows) + len(done)} rows -> {args.out}")


if __name__ == "__main__":
    main()
