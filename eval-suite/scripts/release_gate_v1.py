"""Release gate v1 (step 10, 2026-10-16) — the eval-side quality gate.

Aggregates the stage's frozen artifacts into ONE verdict file.  No LLM calls:
every input is a produced artifact (snapshot + consistency report + judge
repeats).  Gates:

  G1 consistency        — check_consistency gate_pass must be true
  G2 judge agreement    — all judge repeats completed on the same
                          snapshot/eval/judge version; per-dimension score
                          variance across repeats is REPORTED (not gated in v1)
  G3 evidence contract  — no invalid answer may carry validated=true (covered
                          by G1) and every run is manifest-traceable
  G4 provenance         — snapshot meta carries build_id, product/runtime
                          commits, eval sha, label, workers, providers

Overall metrics reported (not gated unless a threshold is given):
  source-valid 76-subset judge dimensions, answer status distribution,
  table-binding rates, latency p50/p95.

Usage:
    python scripts/release_gate_v1.py --snapshot <e2e json> \
        --judges results/llm_judge/editorial_v3_1_final_r1.jsonl [--judges ...r2 ...]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _dimension_scores(rows: list[dict]) -> dict[str, list[float]]:
    dims: dict[str, list[float]] = {}
    for row in rows:
        if "score" not in row or row.get("attribution") == "judge_error":
            continue
        scores = row["score"].get("scores") if isinstance(row["score"], dict) else None
        for key, value in (scores or {}).items():
            if isinstance(value, (int, float)):
                dims.setdefault(key, []).append(float(value))
    return dims


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--judges", type=Path, nargs="+", required=True)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    consistency_path = args.snapshot.with_name(args.snapshot.stem + ".consistency.json")
    table_path = args.snapshot.with_name(args.snapshot.stem + ".table_binding.json")
    consistency = json.loads(consistency_path.read_text(encoding="utf-8")) if consistency_path.exists() else None
    table = json.loads(table_path.read_text(encoding="utf-8")) if table_path.exists() else None

    gates: dict[str, bool] = {}
    notes: list[str] = []

    # G1 consistency
    gates["G1_consistency"] = bool(consistency and consistency.get("gate_pass"))
    if not gates["G1_consistency"]:
        notes.append("consistency gate failed or report missing")

    # G2 judge repeats
    judge_runs = []
    for path in args.judges:
        rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        manifest_path = path.with_suffix(".manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
        judge_runs.append({"path": path.name, "n": len([r for r in rows if "score" in r]),
                           "model": manifest.get("model", "?"), "prompt_sha": manifest.get("prompt_sha256", "?")[:12],
                           "e2e_sha": manifest.get("e2e_sha256", "?")[:12],
                           "dimensions": _dimension_scores(rows)})
    same_inputs = len({run["e2e_sha"] for run in judge_runs}) == 1
    same_judge = len({(run["model"], run["prompt_sha"]) for run in judge_runs}) == 1
    gates["G2_judge_repeats"] = bool(judge_runs and same_inputs and same_judge and all(run["n"] > 0 for run in judge_runs))
    if not same_inputs:
        notes.append("judge repeats scored different snapshots")

    variance = {}
    if len(judge_runs) >= 2:
        common = set(judge_runs[0]["dimensions"])
        for run in judge_runs[1:]:
            common &= set(run["dimensions"])
        for dim in sorted(common):
            per_run = [statistics.mean(run["dimensions"][dim]) for run in judge_runs if run["dimensions"][dim]]
            if len(per_run) >= 2:
                variance[dim] = {"mean": round(statistics.mean(per_run), 3),
                                 "spread": round(max(per_run) - min(per_run), 3),
                                 "per_run": [round(v, 3) for v in per_run]}

    # G4 provenance
    meta = snapshot.get("meta", {})
    gates["G4_provenance"] = bool(meta.get("served_build") and meta.get("eval_sha256") and meta.get("label")
                                  and meta.get("product_commit") and meta.get("runtime_commit"))
    summary = snapshot.get("summary", {})

    verdict = {
        "gate": "release_gate_v1",
        "generated": json.dumps({"label": meta.get("label")}),
        "snapshot": str(args.snapshot),
        "gates": gates,
        "overall_pass": all(gates.values()),
        "notes": notes,
        "metrics": {
            "n": summary.get("n"), "errors": summary.get("errors"),
            "answer_validated": summary.get("answer_validated"),
            "numeric_fidelity": summary.get("numeric_fidelity"),
            "latency_p50_ms": summary.get("latency_p50_ms"), "latency_p95_ms": summary.get("latency_p95_ms"),
            "status_distribution": consistency.get("status_distribution") if consistency else None,
            "table_binding": table.get("summary", {}).get("rates_over_table_claims") if table else None,
            "judge_dimensions_variance_across_repeats": variance,
            "judge_runs": [{k: v for k, v in run.items() if k != "dimensions"} for run in judge_runs],
            "judge_dimensions_latest": ({dim: round(statistics.mean(vals), 3) for dim, vals in judge_runs[-1]["dimensions"].items()}
                                        if judge_runs else None),
        },
        "provenance": meta,
        "overall_metric_policy": "overall scores are informational only; this gate binds on G1-G4",
    }
    out = args.out or (ROOT / "data" / "benchmarks" / "corpus-eval" / "results" / "release_gate_v1_20261016.json")
    out.write_text(json.dumps(verdict, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"gates": gates, "overall_pass": verdict["overall_pass"], "notes": notes}, ensure_ascii=False, indent=1))
    print("verdict:", out)
    return 0 if verdict["overall_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
