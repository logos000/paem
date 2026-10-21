"""Retrieval error taxonomy builder (step 6, 2026-10-16).

Scans one E2E snapshot and groups candidate rows into the five converged
error classes:

  wrong-year      — the question pins a year the retrieved evidence does not
                    carry (or carries a different one)
  wrong-audience  — the question's audience differs from the evidence's
                    applicable audience
  table-context   — numeric evidence without table context (no heading /
                    bare digits / kind=table without header binding)
  multi-hop       — C4 rows (multi-premise coverage failures)
  dangling-id     — claim evidence_ids outside the final pack

Heuristics propose; the analyst confirms.  Output: one JSON with per-class
candidate ids, per-row evidence notes, and a label sheet
(``labels.{class}[id] = confirmed|rejected|unsure``) to fill in.

Usage:
    python scripts/build_taxonomy.py --snapshot <e2e json> [--labels labels.json]
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

YEAR_RE = re.compile(r"\b(20\d{2})\b")
TABLE_KINDS = {"table", "table_row", "table_cell"}
_BARE_NUMERIC_RE = re.compile(r"^[\s\d.,|%：:；;()（）-]+$")


def _years_in(text: str) -> set[str]:
    return set(YEAR_RE.findall(str(text or "")))


def _question_years(item: dict[str, Any]) -> set[str]:
    years = _years_in(item.get("question", ""))
    ir = item.get("query_ir") or {}
    years |= {str(y)[-4:] for y in ir.get("academic_years") or [] if str(y)[-4:].isdigit()}
    return years


def _audience_of(item: dict[str, Any]) -> str:
    ir = item.get("query_ir") or {}
    return str(ir.get("audience") or ir.get("topic_audience") or "")


def classify_row(row: dict[str, Any]) -> dict[str, Any]:
    question_years = _question_years(row)
    audience = _audience_of(row)
    pack = row.get("evidence_pack") or {}
    usable = set(pack.get("usable_evidence_ids") or [])
    pack_ids = {e.get("evidence_id", "") for e in pack.get("evidence") or []}

    wrong_year_notes: list[str] = []
    wrong_audience_notes: list[str] = []
    table_notes: list[str] = []
    for ev in pack.get("evidence") or []:
        eid = ev.get("evidence_id", "")
        if eid not in usable:
            continue
        ev_years = _years_in(ev.get("title", "")) | _years_in(ev.get("heading", "")) or _years_in(ev.get("text", "")[:400])
        if question_years and ev_years and not (question_years & ev_years):
            wrong_year_notes.append(f"{eid[:24]} years={sorted(ev_years)} vs q={sorted(question_years)}")
        elif question_years and not ev_years and ev.get("missing_facts", {}).get("years"):
            wrong_year_notes.append(f"{eid[:24]} carries no year while q pins {sorted(question_years)}")
        applicable = ev.get("applicable_to") or {}
        if audience and applicable:
            aud_values = {str(v).lower() for v in applicable.values() if v}
            if aud_values and audience.lower() not in aud_values and not any(audience.lower() in v or v in audience.lower() for v in aud_values):
                wrong_audience_notes.append(f"{eid[:24]} applicable_to={sorted(aud_values)} vs q.audience={audience}")
        kind = str(ev.get("kind") or "")
        text = str(ev.get("text") or "")
        if kind in TABLE_KINDS and not ev.get("heading"):
            table_notes.append(f"{eid[:24]} table kind without heading")
        if _BARE_NUMERIC_RE.match(text[:80]) and kind not in TABLE_KINDS:
            table_notes.append(f"{eid[:24]} bare-numeric paragraph (likely detached table row): {text[:40]!r}")

    dangling = [
        f"claim{i}:{eid}"
        for i, claim in enumerate(row.get("claims") or [], 1)
        for eid in (claim.get("evidence_ids") or []) if str(eid) not in pack_ids
    ]
    multi_hop = str(row.get("level")) == "C4"
    return {
        "id": row.get("id", "?"),
        "level": row.get("level", ""),
        "wrong_year": wrong_year_notes,
        "wrong_audience": wrong_audience_notes,
        "table_context": table_notes,
        "multi_hop": [f"C4 row, {len(row.get('claims') or [])} claims"] if multi_hop else [],
        "dangling_id": dangling,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--labels", type=Path, default=None, help="optional filled label sheet {class: {id: confirmed|rejected|unsure}}")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    data = json.loads(args.snapshot.read_text(encoding="utf-8"))
    rows = [r for r in data.get("rows") or [] if not r.get("error")]
    classified = [classify_row(r) for r in rows]
    labels = json.loads(args.labels.read_text(encoding="utf-8")) if args.labels and args.labels.is_file() else {}

    classes = ("wrong_year", "wrong_audience", "table_context", "multi_hop", "dangling_id")
    report: dict[str, Any] = {
        "snapshot": str(args.snapshot),
        "n_rows": len(rows),
        "classes": {},
    }
    for cls in classes:
        candidates = {c["id"]: c[cls] for c in classified if c[cls]}
        confirmed = {qid: notes for qid, notes in candidates.items()
                     if labels.get(cls, {}).get(qid) != "rejected"}
        report["classes"][cls] = {
            "n_candidates": len(candidates),
            "n_after_labels": len(confirmed),
            "rows": confirmed,
        }
    out = args.out or args.snapshot.with_name(args.snapshot.stem + ".taxonomy.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    summary = {cls: report["classes"][cls]["n_candidates"] for cls in classes}
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print("report:", out)


if __name__ == "__main__":
    main()
