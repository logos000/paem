"""Consistency auto-check over one E2E product snapshot (P0 gate, 2026-10-16).

Six counts that must all be zero before the evidence-contract stage exits:

1. dangling_claim_ids      — claim.evidence_ids not in the final evidence pack
2. blocking_nonusable_refs — claim.evidence_ids outside usable_evidence_ids or blocking
3. trace_missing           — no retrieval_trace strategy/queries in the row
4. claims_citations_mismatch — citation ids not a subset of claim-used ids
5. invalid_validated       — unverified rows carrying validated=true (or
                             validated rows carrying validation problems)
6. unmapped_display_ids    — claim ids still shaped like display numbers
                             (E1/1/[1]) — the mapping to real ids never ran

Usage:
    python scripts/check_consistency.py --snapshot data/benchmarks/corpus-eval/results_e2e_product/<file>.json
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

_DISPLAY_ID_RE = re.compile(r"^\[?\s*[Ee]?\s*\d{1,2}\s*\]?$")

#: Mirror of product.answer._NO_EVIDENCE_RE (keep the two in sync): honest-miss
#: phrasings the prompt red line pins down.
NO_EVIDENCE_RE = re.compile(
    r"证据[中里]?未(包含|提及|提到|提供|覆盖)|未在证据[中里](找到|包含|提及)|证据[中里]没有"
    r"|未(提及|提到)任何|没有(提及|提到|找到)|未提供"
    r"|not\s+(?:be\s+)?(?:found|mentioned|contained|included|provided|covered)\s+in\s+(?:the\s+)?(?:evidence|documents|materials|retrieved)"
    r"|(?:evidence|documents|materials)\s+(?:do|does)\s+not\s+(?:contain|mention|include|provide|cover)"
    r"|not\s+mention(?:ed)?\s+(?:any|in)\b|no\s+information\s+(?:on|about)\b",
    re.IGNORECASE,
)


def check_row(row: dict[str, Any]) -> dict[str, Any]:
    pack = row.get("evidence_pack") or {}
    evidence = pack.get("evidence") or []
    pack_ids = {item.get("evidence_id", "") for item in evidence}
    blocking_ids = {item.get("evidence_id", "") for item in evidence if item.get("blocking")}
    usable_ids = set(pack.get("usable_evidence_ids") or [])
    claims = row.get("claims") or []
    trace = row.get("retrieval_trace") or {}

    dangling: list[str] = []
    nonusable: list[str] = []
    display_ids: list[str] = []
    claim_used: set[str] = set()
    for index, claim in enumerate(claims, 1):
        for raw in claim.get("evidence_ids") or []:
            eid = str(raw)
            claim_used.add(eid)
            if eid not in pack_ids:
                dangling.append(f"claim{index}:{eid}")
            elif eid not in usable_ids or eid in blocking_ids:
                nonusable.append(f"claim{index}:{eid}")
            if _DISPLAY_ID_RE.match(eid):
                display_ids.append(f"claim{index}:{eid}")

    citation_ids = {str(c.get("id")) for c in row.get("citations") or []}
    citation_strays = sorted(citation_ids - claim_used)

    generation = row.get("generation") or {}
    validated = bool(generation.get("validated"))
    problems = generation.get("problems") or []
    mode = generation.get("mode", "")
    invalid_validated = bool(validated and mode == "unverified") or bool(validated and problems and mode != "refusal")
    # Trustworthy statuses must carry checkable claims; a claims-empty answer
    # is only acceptable when labeled unverified / refused (publish-but-label
    # ruling 2026-10-15).
    status = derived_status(row)
    no_claim_trusted = bool(not claims and status in ("answered", "partial", "conflict"))

    return {
        "id": row.get("id", "?"),
        "dangling": dangling,
        "nonusable": nonusable,
        "display_ids": display_ids,
        "citation_strays": citation_strays,
        "trace_missing": not (trace.get("strategy") and trace.get("queries")),
        "invalid_validated": invalid_validated,
        "no_claim_trusted": no_claim_trusted,
        "status": status,
        "stored_status": row.get("status", ""),
        "pack_hash": row.get("pack_hash", ""),
    }


def derived_status(row: dict[str, Any]) -> str:
    """Re-derive the public status (product.answer.compute_status v2 rules).

    Snapshots may predate a status-mapping fix; the checker reports BOTH the
    stored and the re-derived distribution so drift is visible instead of
    silently baked into old rows."""
    generation = row.get("generation") or {}
    mode = generation.get("mode", "")
    disposition = row.get("answer_disposition", "")
    abstain = bool((row.get("decision") or {}).get("abstain"))
    validated = bool(generation.get("validated"))
    decision_status = (row.get("decision") or {}).get("status", "")
    has_claims = bool(row.get("claims"))
    if disposition == "policy_refusal" or mode == "policy_refusal":
        return "refused_policy"
    if abstain or mode == "refusal":
        return "refused_evidence"
    if disposition == "evidence_refusal" and not has_claims:
        return "refused_evidence"
    if not validated:
        return "unverified"
    # Mirror of the product's text-level miss guard (2026-10-17, reg-ne-04):
    # a miss-with-claims answer treats the decision as sufficient — the pack's
    # conflict flag is about NEIGHBOUR facts, not the asked-absent one.
    if has_claims and NO_EVIDENCE_RE.search(str(row.get("answer_text") or "")):
        decision_status = "sufficient"
    if decision_status == "conflict":
        return "conflict"
    if disposition in ("partial_answer", "evidence_refusal"):
        return "partial"
    if row.get("missing_requirements"):
        return "partial"
    return "answered"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=None, help="optional JSON report path (default: alongside the snapshot)")
    args = parser.parse_args()

    data = json.loads(args.snapshot.read_text(encoding="utf-8"))
    rows = data.get("rows") or []
    per_row = [check_row(row) for row in rows if not row.get("error")]

    counts = {
        "dangling_claim_ids": sum(len(r["dangling"]) for r in per_row),
        "rows_with_dangling": sum(1 for r in per_row if r["dangling"]),
        "blocking_nonusable_refs": sum(len(r["nonusable"]) for r in per_row),
        "rows_with_nonusable": sum(1 for r in per_row if r["nonusable"]),
        "trace_missing": sum(1 for r in per_row if r["trace_missing"]),
        "claims_citations_mismatch": sum(len(r["citation_strays"]) for r in per_row),
        "invalid_validated": sum(1 for r in per_row if r["invalid_validated"]),
        "unmapped_display_ids": sum(len(r["display_ids"]) for r in per_row),
        "no_claim_trusted_status": sum(1 for r in per_row if r["no_claim_trusted"]),
        "missing_pack_hash": sum(1 for r in per_row if not r["pack_hash"]),
        "missing_status": sum(1 for r in per_row if not r["status"]),
    }
    status_dist = dict(Counter(r["status"] or "<missing>" for r in per_row))
    stored_status_dist = dict(Counter(r["stored_status"] or "<missing>" for r in per_row))
    gate_pass = all(counts[k] == 0 for k in (
        "dangling_claim_ids", "blocking_nonusable_refs", "trace_missing",
        "claims_citations_mismatch", "invalid_validated", "unmapped_display_ids",
        "no_claim_trusted_status", "missing_pack_hash", "missing_status"))

    report = {
        "snapshot": str(args.snapshot),
        "meta": data.get("meta", {}),
        "n_rows": len(per_row),
        "n_errors": sum(1 for row in rows if row.get("error")),
        "counts": counts,
        "status_distribution": status_dist,
        "gate_pass": gate_pass,
        "offenders": {
            key: [r["id"] for r in per_row if r[flag]]
            for key, flag in (
                ("dangling_claim_ids", "dangling"), ("blocking_nonusable_refs", "nonusable"),
                ("trace_missing", "trace_missing"), ("invalid_validated", "invalid_validated"),
                ("no_claim_trusted_status", "no_claim_trusted"), ("missing_pack_hash", "pack_hash"),
            )
        },
    }
    out = args.out or args.snapshot.with_name(args.snapshot.stem + ".consistency.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"counts": counts, "status_distribution": status_dist,
                      "stored_status_distribution": stored_status_dist, "gate_pass": gate_pass}, ensure_ascii=False, indent=1))
    print("report:", out)


if __name__ == "__main__":
    main()
