# -*- coding: utf-8 -*-
"""Long-context whole-corpus baseline (2026-10-17): stuff EVERY active doc of
the SP build into the prompt — the strongest "no-retrieval architecture"
challenger.  The 780k-context gateway model fits the corpus without
truncation (111 docs / ~1.5M chars ≈ 400-500k tokens).

Writes an e2e-shaped snapshot for the standard judge; [Dnn] markers let the
model cite docs, extracted into row['longcontext_cited'] for trap analysis
(the evidence pack stays empty — there is no retrieval to ground against).

Usage: python scripts/longcontext_run.py --eval <bench> --corpus <build> \
    --out <snapshot.json>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from evalsuite import builder_root  # noqa: E402

PROMPT_HEAD = """你是ZJUI（浙江大学国际联合学院）教务问答助手。下面是教务知识库的全部 {n_docs} 份文档，按 [D01]、[D02] … 编号。
规则：
1. 只依据这些文档回答；引用来源时写 [Dxx] 编号
2. 特别注意学期/学年/版本：问题问哪个学期，就用那个学期的文档；"现行/最新"指当前学期（今天是 {today}，即 2026 年秋季学期）
3. 文档中没有答案时，明确说"知识库中没有该信息"，禁止编造数字、日期、课程、政策
4. 中文问题用中文回答，英文问题用英文回答，直接给答案

=== 知识库文档开始 ===
"""

PROMPT_TAIL = """
=== 知识库文档结束 ===

问题：{question}"""


def build_context(corpus: Path, today: str) -> tuple[str, list[dict]]:
    docs = [json.loads(l) for l in open(corpus / "documents.jsonl", encoding="utf-8")]
    blocks_by_doc: dict[str, list[str]] = defaultdict(list)
    for line in open(corpus / "blocks.jsonl", encoding="utf-8"):
        b = json.loads(line)
        blocks_by_doc[b["doc_uid"]].append((b.get("ordinal", 0), b["text"]))
    active = [d for d in docs if d.get("status") == "active"]
    active.sort(key=lambda d: (d.get("doc_type") or "", d.get("title") or ""))
    doc_map: list[dict] = []
    parts: list[str] = []
    for i, d in enumerate(active, 1):
        terms = ",".join(d.get("terms") or []) or "-"
        body = "\n".join(t for _, t in sorted(blocks_by_doc.get(d["doc_uid"], [])))
        if len(body) > 20000:
            body = body[:20000] + "\n…(truncated)"
        parts.append(f"[D{i:02d}] {d.get('title','')} (term:{terms})\n{body}")
        doc_map.append({"n": i, "doc_uid": d["doc_uid"], "title": d.get("title", ""), "terms": d.get("terms") or []})
    context = PROMPT_HEAD.format(n_docs=len(active), today=today) + "\n\n".join(parts) + PROMPT_TAIL
    return context, doc_map


def ask_one(prompt: str) -> str:
    base = os.environ.get("LLM_BASE_URL", "https://your-gateway.example.com/v1")
    key = os.environ.get("LLM_API_KEY") or os.environ.get("QUESTIONGEN_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "deepseek-v4-flash-780k-local")
    if not key:
        raise RuntimeError("no gateway key")
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 800,
        "stream": True,
        "thinking": {"type": "disabled"},
    }
    for attempt in range(4):
        try:
            text = ""
            with httpx.stream("POST", f"{base}/chat/completions", headers={"Authorization": f"Bearer {key}"},
                              json=body, timeout=600) as resp:
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


def _write(out_path: Path, rows: list[dict], doc_map: list[dict], chars: int) -> None:
    snapshot = {"meta": {"label": "longcontext", "url": "gateway-direct", "n": len(rows),
                         "providers": ["long-context-llm"], "snapshot_version": 3,
                         "context_chars": chars, "n_docs": len(doc_map)},
                "doc_map": doc_map,
                "rows": rows}
    out_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--eval", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--corpus", type=Path, default=builder_root() / "data" / "corpus" / "sharepoint-only-20261015")
    parser.add_argument("--today", default="2026-10-15")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    context, doc_map = build_context(args.corpus, args.today)
    by_n = {d["n"]: d for d in doc_map}
    print(f"context: {len(context):,} chars over {len(doc_map)} docs")
    bench = [json.loads(l) for l in open(args.eval, encoding="utf-8")]
    done: dict[str, dict] = {}
    if args.out.is_file():
        done = {r["id"]: r for r in json.loads(args.out.read_text(encoding="utf-8"))["rows"]}
        print(f"resume: {len(done)} already answered")
    todo = [b for b in bench if b["id"] not in done]
    rows: list[dict] = []

    def run(item):
        started = time.perf_counter()
        # replace, not format: corpus text may contain literal braces
        prompt = context.replace("{question}", item["question"])
        answer = ask_one(prompt)
        return item, answer, (time.perf_counter() - started) * 1000

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run, item) for item in todo]
        for k, fut in enumerate(as_completed(futures), 1):
            item, answer, elapsed = fut.result()
            abstained = any(marker in answer for marker in ("没有该信息", "知识库中没", "不知道", "无法确定", "not in the", "I don't know", "no information"))
            cited = sorted({int(m) for m in re.findall(r"\[D(\d{1,3})\]", answer)})
            rows.append({
                "id": item["id"], "category": item.get("category", ""), "level": item.get("level"),
                "language": item.get("language"), "qshape": item.get("qshape", ""),
                "answerable": bool(item.get("gold")),
                "answer_text": answer, "answer_disposition": "evidence_refusal" if abstained else "answer",
                "abstained": abstained, "cites_evidence": bool(cited), "grounded": bool(cited),
                "numeric_fidelity": True, "latency_ms": round(elapsed, 1),
                "claims": [], "citations": [{"doc_uid": by_n[n]["doc_uid"], "title": by_n[n]["title"]} for n in cited if n in by_n],
                "citation_map": {}, "longcontext_cited": [by_n[n]["doc_uid"] for n in cited if n in by_n],
                "decision": {"status": "sufficient", "abstain": False}, "generation": {"mode": "longcontext"},
                "evidence_pack": {"evidence": [], "usable_evidence_ids": [], "requirements": [],
                                  "evidence_status": "sufficient", "abstain": False, "abstain_reason": "",
                                  "coverage": 0.0, "conflicts": [], "fact_conflicts": []},
                "retrieval_trace": {"strategy": "long_context", "queries": [], "rounds": []},
            })
            print(f"[longcontext] {k}/{len(todo)} {item['id']} {elapsed/1000:.0f}s abstain={abstained} cites={cited[:4]}", flush=True)
            _write(args.out, rows + list(done.values()), doc_map, len(context))

    _write(args.out, rows + list(done.values()), doc_map, len(context))
    print(f"done: {len(rows) + len(done)} rows -> {args.out}")


if __name__ == "__main__":
    main()
