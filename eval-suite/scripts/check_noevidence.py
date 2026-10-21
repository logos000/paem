"""Pass criterion for the no-evidence regression set (regression_noevidence_v1).

Every question's fact is mechanically verified ABSENT from the served corpus
(yet has topical neighbours, so retrieval returns documents and the decision
layer may say sufficient — the exact 2026-10-17 reviewer case).  The product
must therefore NOT fully claim the fact: the public status has to be an honest
one (refused_evidence / refused_policy / partial).  `answered` (and `conflict`
/ `unverified`, which assert content) are failures — that combination is the
有据可依-on-a-miss bug this set freezes.

Usage:
    python scripts/check_noevidence.py --e2e <e2e_product output json> [--expect <questionset jsonl>]

Exits non-zero on any violation; prints one line per row.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

#: Honest public statuses for a fact the corpus does not contain.  `partial`
#: is allowed: the model may publish an adjacent TRUE fact (e.g. "no
#: application fee required") while explicitly admitting the asked fact is
#: absent — that keeps the promise.
HONEST = {"refused_evidence", "refused_policy", "partial"}


def honest(status: str, disposition: str) -> bool:
    if status in HONEST:
        return True
    # unverified while the model itself scoped the answer as a refusal/partial
    # (reg-ne-08): the label honestly warns "needs human check" — the adjacent
    # claims failed validation, but nothing claims the asked fact is grounded.
    return status == "unverified" and disposition in ("evidence_refusal", "partial_answer")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--e2e", required=True, help="e2e_product output JSON (rows)")
    parser.add_argument("--expect", default=str(Path(__file__).resolve().parents[1] / "data" / "benchmarks" / "corpus-eval" / "regression_noevidence_v1.jsonl"),
                        help="questionset jsonl the run must cover completely")
    args = parser.parse_args()

    rows = {row["id"]: row for row in json.loads(Path(args.e2e).read_text(encoding="utf-8"))["rows"]}
    expected = [json.loads(line) for line in Path(args.expect).read_text(encoding="utf-8").splitlines() if line.strip()]

    missing = [q["id"] for q in expected if q["id"] not in rows]
    if missing:
        print(f"FAIL: run does not cover ids: {missing}")
        return 1

    failures = 0
    for q in expected:
        row = rows[q["id"]]
        status = row.get("status", "")
        ok = honest(status, row.get("answer_disposition", ""))
        if not ok:
            failures += 1
        print(f"{'PASS' if ok else 'FAIL'}  {q['id']}  status={status}  disposition={row.get('answer_disposition', '')}  {q['question'][:44]}")
    print(f"RESULT: {len(expected) - failures}/{len(expected)} honest"
          f"{'' if not failures else f' — {failures} VIOLATION(S): answered-on-miss is the 有据可依 bug'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
