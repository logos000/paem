"""P6: dimension report over harness results — language/tone/layer/intent
breakdowns, CLIR view, and consistency@family.  Consumes the harness output
JSON; never modifies scoring (harness itself stays frozen).
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from evidence.store import read_jsonl


def _fmt_table(title: str, rows: list[tuple[str, int, float, float, float]]) -> str:
    lines = [f"## {title}", "", "| slice | n | anchor@1 | doc@1 | gold_usable@8 |", "| --- | ---: | ---: | ---: | ---: |"]
    for name, n, a1, d1, gu in rows:
        lines.append(f"| {name} | {n} | {a1:.3f} | {d1:.3f} | {gu:.3f} |")
    return "\n".join(lines) + "\n"


def dimension_report(harness_json: Path, eval_jsonl: Path, config: str) -> str:
    result = json.loads(harness_json.read_text(encoding="utf-8"))
    questions = {row["id"]: row for row in read_jsonl(eval_jsonl)}
    block = next((b for b in result["results"] if b["config"] == config), None)
    if block is None:
        return f"config {config} not found in {harness_json}"
    rows_by_id = {r["id"]: r for r in block["rows"]}

    def metrics(ids: list[str]) -> tuple[int, float, float, float]:
        n = len(ids)
        if not n:
            return 0, 0.0, 0.0, 0.0
        a1 = sum(1 for i in ids if rows_by_id[i].get("anchor_rank") == 1) / n
        d1 = sum(1 for i in ids if rows_by_id[i].get("doc_rank") == 1) / n
        gu = sum(1 for i in ids if rows_by_id[i].get("gold_usable")) / n
        return n, a1, d1, gu

    answerable = [i for i, q in questions.items() if q.get("gold") and i in rows_by_id]
    out = [f"# 生成题集分维度报告 — config={config}, build={result['meta']['build_id']}, n={len(answerable)} answerable\n"]
    for field in ("language", "tone", "layer", "intent"):
        grouped: dict[str, list[str]] = defaultdict(list)
        for qid in answerable:
            grouped[str(questions[qid].get(field, "?"))].append(qid)
        rows = [(name, *metrics(ids)) for name, ids in sorted(grouped.items())]
        out.append(_fmt_table(field, rows))

    # consistency@family: a family is consistent when every member's gold-doc
    # hit agrees (all doc_rank==1, or all miss the gold doc in top-10)
    families: dict[str, list[str]] = defaultdict(list)
    for qid in answerable:
        fam = questions[qid].get("family")
        if fam:
            families[fam].append(qid)
    consistent = 0
    for fam, ids in families.items():
        hits = [rows_by_id[i].get("doc_rank") == 1 for i in ids]
        consistent += int(len(set(hits)) == 1)
    if families:
        out.append(f"## consistency@family\n\n| families | consistent (all hit or all miss gold@1) |\n| ---: | ---: |\n| {len(families)} | {consistent / len(families):.3f} |\n")
    return "\n".join(out)
