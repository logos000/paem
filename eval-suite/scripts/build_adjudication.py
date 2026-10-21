"""Adjudication dossier builder (step 10, 2026-10-16).

One dossier page per invalid/uncertain reference-audit row: the question,
golden answer, source anchors, the independent audit's verdict/reason/issues,
the Codex editorial decision/reason, and a RECOMMENDATION slot to fill during
adjudication (verdict: keep_invalid | restore_pass | fix_reference | uncertain).

Usage:
    python scripts/build_adjudication.py            # writes dossier + labels skeleton
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "data" / "benchmarks" / "corpus-eval"

DOSSIER = BASE / "results" / "reference_audit_main" / "adjudication_dossier_20261016.md"
LABELS = BASE / "results" / "reference_audit_main" / "adjudication_labels_20261016.json"


def main() -> None:
    bench = {json.loads(l)["id"]: json.loads(l) for l in (BASE / "editorial_v1_20261015" / "revised_benchmark.jsonl").open(encoding="utf-8")}
    audit = [json.loads(l) for l in (BASE / "results" / "reference_audit_main" / "audit.jsonl").open(encoding="utf-8")]
    editorial = {json.loads(l)["id"]: json.loads(l) for l in (BASE / "editorial_v1_20261015" / "editorial_review.jsonl").open(encoding="utf-8")}

    todo = [a for a in audit if a.get("status") in ("invalid", "uncertain")]
    lines = ["# Reference audit 裁决材料（75 invalid + 2 uncertain）", "",
             "逐题材料：问题 / golden answer / 引文锚点 / 独立审计意见 / Codex 编辑意见。", ""]
    labels: dict[str, dict[str, str]] = {}
    for a in sorted(todo, key=lambda r: r["id"]):
        rid = a["id"]
        row = bench.get(rid, {})
        ed = editorial.get(rid, {})
        gold = row.get("golden_answer") or ""
        quotes = [g.get("quote", "")[:120] for g in (row.get("gold") or [])][:3]
        lines.append(f"## {rid} [{a['status']}] {row.get('level', '')} · {row.get('category', '')}")
        lines.append(f"- 问题：{row.get('question', '')}")
        lines.append(f"- golden answer：{str(gold)[:300]}")
        for i, q in enumerate(quotes, 1):
            lines.append(f"- 引文{i}：{q}")
        lines.append(f"- 独立审计（{a.get('reviewer', '')}）：{a.get('status')} — {a.get('reason', '')[:300]}")
        if a.get("issues"):
            lines.append(f"  - issues：{'; '.join(str(i) for i in a['issues'][:3])}")
        lines.append(f"- Codex 编辑：{ed.get('decision', '')} — {str(ed.get('reason', ''))[:240]}")
        lines.append("- 裁决：____________________（keep_invalid / restore_pass / fix_reference / uncertain）")
        lines.append("- 依据：____________________")
        lines.append("")
        labels[rid] = {"audit_status": a["status"], "verdict": "", "rationale": "", "action": ""}

    DOSSIER.parent.mkdir(parents=True, exist_ok=True)
    DOSSIER.write_text("\n".join(lines), encoding="utf-8")
    LABELS.write_text(json.dumps(labels, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"dossier: {DOSSIER} ({len(todo)} rows)")
    print(f"labels:  {LABELS}")


if __name__ == "__main__":
    main()
