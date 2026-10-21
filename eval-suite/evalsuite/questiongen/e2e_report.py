"""E2E dimension report: join e2e_product rows + question set + product ask log.

Metrics per dimension (language/tone/layer/intent):
- generation mode rates (validated / unverified / fallback / refusal)
- cites, numeric_fidelity, grounded (verbatim-quote — biased against Chinese
  answers quoting English evidence)
- fact_hit: every atomic fact (dates/numbers/codes) of the gold quote appears
  in the answer text — the language-agnostic grounding metric; answer texts
  are recovered from the product ask log by request_id
- refusal discipline on answerable vs trap questions
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from evalsuite import SUITE_ROOT

if str(SUITE_ROOT) not in sys.path:
    sys.path.insert(0, str(SUITE_ROOT))

from evidence.pack import _fact_slots  # noqa: E402
from evidence.store import match_key, read_jsonl  # noqa: E402


def _fact_hit(answer: str, golds: list[dict[str, Any]]) -> bool | None:
    groups = [_fact_slots(str(g.get("quote", ""))) for g in golds]
    all_facts = [f for slots in groups for values in slots.values() for f in values]
    if not all_facts:
        return None
    answer_key = match_key(str(answer or ""))
    for fact in all_facts:
        core = re.split(r"@", str(fact))[0]
        core = re.sub(r"[^0-9a-z\u4e00-\u9fff.]", "", core)
        if core and core not in answer_key:
            return False
    return True


def build_report(e2e_json: Path, eval_jsonl: Path, ask_log: Path | None) -> str:
    data = json.loads(e2e_json.read_text(encoding="utf-8"))
    questions = {row["id"]: row for row in read_jsonl(eval_jsonl)}
    answers: dict[str, str] = {}
    if ask_log and ask_log.is_file():
        for row in read_jsonl(ask_log):
            if row.get("request_id") and row.get("answer"):
                answers[row["request_id"]] = str(row["answer"])
    rows = [r for r in data["rows"] if r.get("id") in questions and r.get("decision")]
    for row in rows:
        q = questions[row["id"]]
        row["_dim"] = {f: q.get(f) for f in ("language", "tone", "layer", "intent")}
        row["_answerable"] = bool(q.get("gold"))
        # v2 e2e rows carry the answer text; the ask-log join is a fallback
        text = row.get("answer_text") or answers.get(row.get("request_id", ""), "")
        row["_fact_hit"] = _fact_hit(text, q.get("gold") or [])

    out = [f"# 全系统 E2E 分维度报告 — {data['meta']['label']}, build={data['meta']['served_build']}, n={len(rows)}\n",
           "provenance 注脚：题集 ai_reviewed 级（未经用户亲检）\n", "## 总览", "```json",
           json.dumps(data["summary"], ensure_ascii=False, indent=1), "```", ""]

    def table(title: str, field: str) -> str:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[str(row["_dim"].get(field) or "?")].append(row)
        lines = [f"## {title}", "",
                 "| slice | n | validated | unverified | fallback | answered | cites | numeric_fid | grounded | fact_hit |",
                 "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
        for name in sorted(grouped):
            group = grouped[name]
            n = len(group)
            modes = Counter((r.get("generation") or {}).get("mode", "?") for r in group)
            fallback = sum(v for k, v in modes.items() if k not in ("validated", "unverified", "refusal"))
            answered = [r for r in group if not r["decision"]["abstain"]]
            def _rate(sub: list, fn) -> str:
                return f"{sum(1 for r in sub if fn(r)) / len(sub):.3f}" if sub else "–"
            hits = [r["_fact_hit"] for r in answered if r["_fact_hit"] is not None]
            fact_cell = f"{sum(hits)/len(hits):.3f} ({len(hits)})" if hits else "–"
            lines.append(
                f"| {name} | {n} | {modes.get('validated', 0)/n:.3f} | {modes.get('unverified', 0)/n:.3f} | "
                f"{fallback/n:.3f} | {len(answered)/n:.3f} | {_rate(answered, lambda r: r.get('cites_evidence'))} | "
                f"{_rate(answered, lambda r: r.get('numeric_fidelity'))} | {_rate(answered, lambda r: r.get('grounded'))} | "
                f"{fact_cell} |")
        return "\n".join(lines) + "\n"

    for field, title in (("language", "按语言"), ("tone", "按语气"), ("layer", "按层次"), ("intent", "按意图")):
        out.append(table(title, field))

    answerable = [r for r in rows if r["_answerable"]]
    traps = [r for r in rows if not r["_answerable"]]
    out.append("## 拒答纪律\n")
    out.append(f"- 可答题误拒率: {sum(1 for r in answerable if r['decision']['abstain'])/max(1,len(answerable)):.3f}（n={len(answerable)}）")
    out.append(f"- 陷阱题正确拒答率: {sum(1 for r in traps if r['decision']['abstain'])/max(1,len(traps)):.3f}（n={len(traps)}）")
    refused = [r["id"] for r in answerable if r["decision"]["abstain"]]
    out.append(f"- 误拒题目（前12）: {refused[:12]}\n")
    return "\n".join(out)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--e2e", required=True)
    parser.add_argument("--eval", required=True)
    parser.add_argument("--ask-log", default=str(SUITE_ROOT.parent / "zjui-product" / "academic-affairs-product" / "logs" / "ask.jsonl"))
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    text = build_report(Path(args.e2e), Path(args.eval), Path(args.ask_log))
    out = Path(args.out) if args.out else Path(args.e2e).with_suffix(".dimensions.md")
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"\nwritten: {out}")
