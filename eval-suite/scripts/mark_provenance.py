"""P6: mark eval items with review provenance levels.

Levels (weakest → strongest): ``machine_checked`` (lint/schema only) <
``ai_reviewed`` (AI adjudication approved) < ``human_verified`` (a human
verified the gold) < ``user_approved`` (the project owner signed off).
Formal tables must state which level they rely on — "verified: true" alone
no longer means "human-checked".

Sources: the two-step verify tool outputs (``review_*.json``, decisions
``approve``/``reject``) and an optional manual override file
(``review_manual.json``: [{id, level}] or [{id, decision: approve, level}]).
Output: a sibling eval file with a ``provenance`` field per item + counts.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

LEVELS = ("machine_checked", "ai_reviewed", "human_verified", "user_approved")
ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--eval", type=Path, default=ROOT / "data" / "benchmarks" / "corpus-eval" / "verified_eval_v3.jsonl")
    parser.add_argument("--reviews", nargs="*", type=Path, default=[
        ROOT / "data" / "benchmarks" / "corpus-eval" / "review_2026-10-13.json",
        ROOT / "data" / "benchmarks" / "corpus-eval" / "review_2026-10-14.json",
    ])
    parser.add_argument("--manual", type=Path, default=ROOT / "data" / "benchmarks" / "corpus-eval" / "review_manual.json", help="optional human/user overrides [{id, level}]")
    parser.add_argument("--out", type=Path, default=None, help="default: <eval stem>_provenance.jsonl")
    args = parser.parse_args(argv)

    ai_approved: set[str] = set()
    for review in args.reviews:
        if not review.is_file():
            continue
        for row in json.loads(review.read_text(encoding="utf-8")):
            if row.get("decision") == "approve":
                ai_approved.add(str(row["id"]))
    manual: dict[str, str] = {}
    if args.manual.is_file():
        for row in json.loads(args.manual.read_text(encoding="utf-8")):
            level = str(row.get("level") or ("user_approved" if row.get("decision") == "approve" else ""))
            if level in LEVELS:
                manual[str(row["id"])] = level

    items, counts = [], Counter()
    for line in args.eval.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        item_id = str(row.get("id"))
        if item_id in manual:
            level = manual[item_id]
        elif row.get("verified") and item_id in ai_approved:
            level = "ai_reviewed"
        elif row.get("verified"):
            level = "machine_checked"  # verified flag without a recorded review pass
        else:
            level = "machine_checked"
        row["provenance"] = level
        counts[level] += 1
        items.append(row)

    out = args.out or args.eval.with_name(args.eval.stem + "_provenance.jsonl")
    with out.open("w", encoding="utf-8") as handle:
        for row in items:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"{out.name}: {len(items)} items — " + ", ".join(f"{level}={counts[level]}" for level in LEVELS if counts[level]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
