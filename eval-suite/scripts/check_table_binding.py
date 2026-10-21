"""Table-fact binding check (step 8, 2026-10-16).

For every numeric claim whose cited evidence carries table_context, check the
binding chain the plan demands:

  target_row_hit — the claim's object term (course code / name) appears in the
                   cited row's cells
  header_hit     — the unit of the claim's number matches a column header
                   (credits in a Credits column, not a Seats column)
  unit_match     — claim number+unit equals a cell value+header unit
  numeric_entailment — the claim's digits appear in the cited row's cells

Reports per-question binding rates plus the aggregate; unbound numeric claims
(claims citing non-table evidence) are counted separately — numeric_fidelity
stays a drift alarm, these are the fact-binding measures.

Usage:
    python scripts/check_table_binding.py --snapshot <e2e json>
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)")
_UNIT_WORDS = {"credit": ("学分", "credit", "credits"), "person": ("人", "名"), "course": ("门",),
               "point": ("分", "point", "points"), "hour": ("小时", "hour", "hours"), "week": ("周", "week", "weeks")}
_CODE_RE = re.compile(r"\b[A-Za-z]{2,}[ -]?\d{2,}[A-Za-z0-9-]*\b")


def _claim_unit(text: str) -> str:
    lowered = str(text).lower()
    for unit, words in _UNIT_WORDS.items():
        if any(w in lowered for w in words):
            return unit
    return ""


def check_row(row: dict[str, Any]) -> dict[str, Any]:
    claims = row.get("claims") or []
    pack = row.get("evidence_pack") or {}
    by_id = {e.get("evidence_id"): e for e in pack.get("evidence") or []}
    numeric_claims = 0
    table_claims = 0
    binding = {"target_row_hit": 0, "header_hit": 0, "unit_match": 0, "numeric_entailment": 0}
    for claim in claims:
        text = str(claim.get("text", ""))
        numbers = _NUM_RE.findall(text)
        if not numbers:
            continue
        numeric_claims += 1
        cited = [by_id[eid] for eid in (claim.get("evidence_ids") or []) if eid in by_id]
        tables = [e.get("table_context") or {} for e in cited if e.get("table_context")]
        if not tables:
            continue  # numeric claim on non-table evidence — counted as unbound
        table_claims += 1
        cells_blob = " ".join(str(c) for t in tables for c in (t.get("cells") or []))
        header_blob = " ".join(str(h) for t in tables for h in (t.get("header") or [])).lower()
        codes = _CODE_RE.findall(text) or [w for w in re.findall(r"[A-Za-z]{3,}", text)]
        if any(str(code).lower() in cells_blob.lower() for code in codes):
            binding["target_row_hit"] += 1
        unit = _claim_unit(text)
        if unit and (unit in header_blob or any(w in header_blob for w in _UNIT_WORDS.get(unit, ()))):
            binding["header_hit"] += 1
        if unit:
            header_units = {u for u, words in _UNIT_WORDS.items() if any(w in header_blob for w in words)}
            cell_numbers = {n for n in _NUM_RE.findall(cells_blob)}
            if unit in header_units and any(n in cell_numbers for n in numbers):
                binding["unit_match"] += 1
        if any(n in cells_blob for n in numbers):
            binding["numeric_entailment"] += 1
    return {"id": row.get("id", "?"), "numeric_claims": numeric_claims, "table_claims": table_claims, **binding}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot", type=Path, required=True)
    args = parser.parse_args()
    data = json.loads(args.snapshot.read_text(encoding="utf-8"))
    rows = [r for r in data.get("rows") or [] if not r.get("error")]
    per_row = [check_row(r) for r in rows]
    numeric_rows = [r for r in per_row if r["numeric_claims"]]
    table_rows = [r for r in per_row if r["table_claims"]]
    total_table_claims = sum(r["table_claims"] for r in per_row)
    summary = {
        "n_rows": len(rows),
        "rows_with_numeric_claims": len(numeric_rows),
        "rows_with_table_backed_numeric_claims": len(table_rows),
        "table_backed_numeric_claims": total_table_claims,
        "target_row_hit": sum(r["target_row_hit"] for r in per_row),
        "header_hit": sum(r["header_hit"] for r in per_row),
        "unit_match": sum(r["unit_match"] for r in per_row),
        "numeric_entailment": sum(r["numeric_entailment"] for r in per_row),
        "rates_over_table_claims": {
            key: round(sum(r[key] for r in per_row) / total_table_claims, 4) if total_table_claims else None
            for key in ("target_row_hit", "header_hit", "unit_match", "numeric_entailment")
        },
    }
    out = args.snapshot.with_name(args.snapshot.stem + ".table_binding.json")
    out.write_text(json.dumps({"summary": summary, "rows": per_row}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print("report:", out)


if __name__ == "__main__":
    main()
