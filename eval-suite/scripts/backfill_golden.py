# -*- coding: utf-8 -*-
"""Backfill golden answers for an existing generated set (user design ruling
2026-10-15: every question carries a reference answer derived STRICTLY from
its gold quotes — the judge scores against this instead of inferring from
the quote).

Usage:
  PYTHONPATH=. ../llm-corpus-builder/.venv/Scripts/python.exe scripts/backfill_golden.py \
      --eval data/benchmarks/corpus-eval/eval_sp_generated_v1.jsonl [--limit N]
"""
import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evalsuite.questiongen.provider import QuestionGenProvider  # noqa: E402

PROMPT = """你是教务问答系统的标准答案撰写人。依据给出的【问题】和【gold 依据原文】，写一句话标准答案（golden answer）。

规则：
1. 答案只准使用 gold 依据原文中的信息——数字、日期、人名、课程码必须逐字一致，禁止推断或补充
2. 语言跟随问题（中文问→中文答，英文问→英文答，混合→中文为主）
3. 直接回答问题所问，不加"根据文档"之类前缀，一句话（≤60字），问什么答什么
4. 若依据不足以回答问题，输出空字符串 ""

【问题】{question}
【gold 依据原文】{quote}

输出 JSON：{{"golden_answer": "..."}} 只输出 JSON。"""


_DATE_RE = re.compile(
    r"(?:19|20)\d{2}[-/年.]\s*\d{1,2}[-/月.]\s*\d{1,2}日?"
    r"|(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s*\d{1,2}(?:st|nd|rd|th)?(?:,?\s*(?:19|20)\d{2})?"
    r"|\d{1,2}\s*月\s*\d{1,2}\s*日"
    r"|\b\d{1,2}[:.]\d{2}\b|\d{1,2}\s?[ap]\.?m\.?",
    re.I,
)


def _nondigit_date_safe(text: str) -> set[str]:
    """Digits outside date/time expressions — month-word vs digit-form dates
    must not count as fabrication (the judge checks date values itself)."""
    stripped = _DATE_RE.sub(" ", str(text))
    return set(re.findall(r"\d+(?:\.\d+)?", stripped))


def backfill_row(row: dict, provider: QuestionGenProvider) -> dict:
    golds = row.get("gold") or []
    if not golds:
        return row  # traps: the golden behavior IS the refusal — no answer text
    quotes = "\n".join(f"- {g.get('quote', '')}" for g in golds)
    raw = provider.generate_json(
        PROMPT.replace("{question}", row["question"]).replace("{quote}", quotes[:1500]),
        task="golden-answer-v1", max_tokens=300,
    )
    answer = ""
    if isinstance(raw, dict):
        answer = str(raw.get("golden_answer") or "").strip()
    # machine guard: non-date numbers in the answer must come from quote ∪ question
    allowed = _nondigit_date_safe(quotes) | _nondigit_date_safe(row["question"])
    if answer and not _nondigit_date_safe(answer) <= allowed:
        answer = ""  # drop self-fabricating reference answers
    row["golden_answer"] = answer
    # empty golden answer on an answerable row = the gold quote cannot answer
    # the question (weak gold that slipped past review) — quarantine, don't force
    row["gold_insufficient"] = bool(row.get("gold")) and not answer
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
    empty = [i for i, r in enumerate(targets) if "golden_answer" not in r]
    print(f"backfilling golden answers for {len(empty)}/{len(targets)} rows", file=sys.stderr)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        filled = list(pool.map(lambda i: backfill_row(targets[i], provider), empty))
    for i, row in zip(empty, filled):
        targets[i] = row
    # ALWAYS write the full file — a --limit smoke run must never shrink it
    # (the 2026-10-15 truncation incident: limit-6 smoke overwrote 410→6)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    n_golden = sum(1 for r in targets if r.get("golden_answer"))
    n_empty_gold = sum(1 for r in targets if not r.get("gold"))
    print(json.dumps({"rows": len(targets), "golden_answer": n_golden, "traps_no_answer": n_empty_gold,
                      "gold_insufficient": sum(1 for r in targets if r.get("gold_insufficient"))}))


if __name__ == "__main__":
    main()
