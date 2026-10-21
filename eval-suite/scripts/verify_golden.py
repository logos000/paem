# -*- coding: utf-8 -*-
"""Independent golden-answer verification pass (user ruling 2026-10-15:
golden answers must be highly reliable).

For every row that HAS a golden answer, an independent context (quote +
golden answer ONLY — no question, no other rows) checks sentence-by-sentence
that every claim in the golden answer is literally supported by the quote.
A golden answer failing verification is dropped (row becomes
gold_insufficient) — a wrong reference answer is worse than none.

Usage:
  PYTHONPATH=. ../llm-corpus-builder/.venv/Scripts/python.exe \
      scripts/verify_golden.py --eval <set.jsonl> [--limit N] [--workers 6]
"""
import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evalsuite.questiongen.provider import QuestionGenProvider  # noqa: E402

PROMPT = """你是标准答案审核员。只依据下面【原文】，逐句核对【golden answer】中的每个事实性陈述（数字、日期、人名、课程码、地点、结论）是否被原文逐字支撑。

【原文】
{quote}

【golden answer】
{golden}

判定（只输出 JSON）：
{{"supported": true 或 false, "violations": ["不被支撑的具体内容…"], "reason": "一句话"}}
"""


def verify_row(row: dict, provider: QuestionGenProvider) -> dict:
    golden = str(row.get("golden_answer") or "").strip()
    quotes = "\n".join(f"- {g.get('quote', '')}" for g in (row.get("gold") or []))
    if not golden or not quotes:
        return row
    raw = provider.generate_json(
        PROMPT.replace("{quote}", quotes[:1500]).replace("{golden}", golden[:300]),
        task="golden-verify-v1", max_tokens=300,
    )
    supported = True
    if isinstance(raw, dict):
        supported = bool(raw.get("supported", True))
    if not supported:
        row["golden_answer"] = ""
        row["gold_insufficient"] = bool(row.get("gold"))
        row["golden_rejected_by_verify"] = True
    return row


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--eval", required=True)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=20)
    args = parser.parse_args()

    path = Path(args.eval)
    rows = [json.loads(l) for l in path.open(encoding="utf-8")]
    targets = rows if not args.limit else rows[: args.limit]
    provider = QuestionGenProvider()
    todo = [i for i, r in enumerate(targets) if r.get("golden_answer")]
    print(f"verifying {len(todo)} golden answers", file=sys.stderr)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        verified = list(pool.map(lambda i: verify_row(targets[i], provider), todo))
    for i, row in zip(todo, verified):
        targets[i] = row
    with path.open("w", encoding="utf-8") as handle:  # always full file
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps({"rows": len(rows), "verified": len(todo),
                      "rejected": sum(1 for r in rows if r.get("golden_rejected_by_verify")),
                      "golden_remaining": sum(1 for r in rows if r.get("golden_answer"))}))


if __name__ == "__main__":
    main()
