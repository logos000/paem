# -*- coding: utf-8 -*-
"""Gateway-LLM review pass over the trap set (dual-AI review, pass 1 of 2).

Each question is judged blind — the model sees the question, the gold quotes
and the family expectation, never our system's internals.

Usage: python scripts/review_trapset.py --eval <trapset.jsonl> --out <review.json>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

FAMILY_RULES = {
    "wrong_term": "题目询问某个学期的具体事实。金答案引用该学期文档原文。要点：答案必须是该学期文档中的值，而不是其他学期的。",
    "current_version": "题目不带学期限定词询问'现行/最新'版本的事实（今天是2026-10-15，当前学期=2026年秋季）。金答案应来自最新学期版本的文档。",
    "abstain": "知识库中不存在该事实（学期/专业不存在）。正确行为是明确说没有该信息。金答案为空。要点：题目必须听起来合理（像真实学生会问的），不能明显荒谬。",
    "audience": "这是面向教师的活动，但提问者自称本科生。正确行为是指明该政策不适用于本科生（或说明面向教师）。",
    "conflict": "知识库中同一文档对同一事实列出了两个不同的值。正确答案是同时呈现两个值及其出处。",
}

PROMPT = """你是教务问答基准的题集审查员。审查下面这道题是否可以进入正式评测集。

题目：{question}
题目类型说明：{rule}
金答案（引用原文）：{gold}

审查三个维度，各打 pass/fail 并说明：
1. 自然性：像真实学生/教职工会问的问题吗？有没有机制性语言（如"版本治理""检索"）或歧义？
2. 金答案正确性：金答案的原文引用是否真的回答了题目所问的那个事实？
3. 可辩护性：如果答辩评委追问"这题凭什么算对/错"，这个判定站得住吗？

输出一行 JSON：{{"id": "{rid}", "natural": true/false, "gold_correct": true/false, "defensible": true/false, "verdict": "KEEP|FIX|DROP", "issue": "一句话问题说明或空"}}
只输出这一行 JSON，不要其他内容。"""


def ask(prompt: str) -> str:
    base = os.environ.get("LLM_BASE_URL", "https://your-gateway.example.com/v1")
    key = os.environ.get("LLM_API_KEY") or os.environ.get("QUESTIONGEN_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "deepseek-v4-flash-780k-local")
    body = {"model": model, "messages": [{"role": "user", "content": prompt}],
            "temperature": 0, "max_tokens": 300, "stream": True, "thinking": {"type": "disabled"}}
    for attempt in range(4):
        try:
            text = ""
            with httpx.stream("POST", f"{base}/chat/completions", headers={"Authorization": f"Bearer {key}"},
                              json=body, timeout=120) as resp:
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
                        text += json.loads(payload)["choices"][0]["delta"].get("content") or ""
                    except Exception:
                        continue
            return text.strip()
        except Exception:
            if attempt == 3:
                return ""
            time.sleep(5 + attempt * 10)
    return ""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--eval", type=Path, default=ROOT / "data" / "benchmarks" / "corpus-eval" / "trapset_v1.jsonl")
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "benchmarks" / "corpus-eval" / "trapset_v1.review-gateway.json")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()

    rows = [json.loads(l) for l in open(args.eval, encoding="utf-8")]
    results: list[dict] = []

    def run(item):
        gold = "\n".join(f"- [{g['doc_uid'][:12]}] {g['quote'][:180]}" for g in item.get("gold") or []) or "（无——正确行为是拒答/指明不适用）"
        text = ask(PROMPT.format(question=item["question"], rule=FAMILY_RULES[item["trap_type"]], gold=gold, rid=item["id"]))
        try:
            m = re.search(r"\{.*\}", text, re.S)
            verdict = json.loads(m.group(0)) if m else {"id": item["id"], "verdict": "ERROR", "issue": text[:120]}
        except Exception:
            verdict = {"id": item["id"], "verdict": "ERROR", "issue": text[:120]}
        return verdict

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run, item) for item in rows]
        for fut in as_completed(futures):
            results.append(fut.result())

    results.sort(key=lambda r: r.get("id", ""))
    args.out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    from collections import Counter
    print("verdicts:", dict(Counter(r.get("verdict") for r in results)))
    for r in results:
        if r.get("verdict") != "KEEP":
            print(f"  [{r.get('verdict')}] {r.get('id')}: {r.get('issue', '')[:90]}")


if __name__ == "__main__":
    main()
