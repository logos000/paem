# -*- coding: utf-8 -*-
"""Ablation v1 analysis: per-arm × per-level × per-qshape × per-language
comparison with judge dimensions and Wilson 95% CIs on good-answer rates.

Usage:  python scripts/ablation_report_v1.py [--arms full,no-dense,...]
Writes data/benchmarks/corpus-eval/results/ablation/ablation_v1_report.md
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "data" / "benchmarks" / "corpus-eval"
ARMS = ["full", "no-dense", "no-queryopt", "lexical", "no-governance", "template"]
DIMS = ["correctness", "completeness", "groundedness", "clarity", "citation_quality"]

BENCH = BASE / "benchmark_superset_v1_275.jsonl"


def wilson(p: float, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def load_bench() -> dict[str, dict]:
    return {json.loads(l)["id"]: json.loads(l) for l in open(BENCH, encoding="utf-8")}


def load_arm(arm: str) -> tuple[dict[str, dict], dict[str, dict]]:
    judge_path = BASE / "results" / "llm_judge" / f"judge__abl-{arm}.jsonl"
    snap_glob = sorted((BASE / "results_e2e_product").glob(f"e2e_product__abl-{arm}__*.json"))
    snap_glob = [p for p in snap_glob if "consistency" not in p.name]
    judge = {}
    if judge_path.is_file():
        judge = {json.loads(l)["id"]: json.loads(l) for l in open(judge_path, encoding="utf-8")}
    snap = {}
    if snap_glob:
        snap = {r["id"]: r for r in json.loads(snap_glob[-1].read_text(encoding="utf-8"))["rows"]}
    return judge, snap


def score_of(row: dict) -> dict[str, float]:
    return (row.get("score") or {}).get("scores") or {}


def good(row: dict) -> bool:
    sc = score_of(row)
    vals = [sc.get("groundedness"), sc.get("citation_quality"), sc.get("correctness")]
    return all(v is not None and v >= 4 for v in vals)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arms", default=",".join(ARMS))
    args = parser.parse_args()
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    bench = load_bench()

    lines = ["# 消融 v1 报告（275 题超集，2026-10-16）", ""]

    # ---- per-arm headline
    header = "| 臂 | n(judge) | good% [95%CI] | grounded | citation | correctness | refusal | validated% |"
    lines += [header, "|---|---|---|---|---|---|---|---|"]
    table: dict[str, dict] = {}
    for arm in arms:
        judge, snap = load_arm(arm)
        ids = [i for i in judge if i in bench]
        n = len(ids)
        goods = sum(1 for i in ids if good(judge[i]))
        p = goods / n if n else 0.0
        lo, hi = wilson(p, n)
        dims = {d: (sum(score_of(judge[i]).get(d) or 0 for i in ids) / max(1, sum(1 for i in ids if score_of(judge[i]).get(d)))) for d in DIMS}
        refusals = sum(1 for i in ids if judge[i].get("answer_disposition") == "evidence_refusal")
        val = 0
        if snap:
            val = sum(1 for i in ids if (snap.get(i, {}).get("generation") or {}).get("validated"))
        table[arm] = {"n": n, "good": p, "lo": lo, "hi": hi, "dims": dims, "refusal": refusals, "val": val}
        lines.append(f"| {arm} | {n} | {p*100:.1f} [{lo*100:.0f},{hi*100:.0f}] | {dims['groundedness']:.2f} | {dims['citation_quality']:.2f} | {dims['correctness']:.2f} | {refusals} | {val/max(1,n)*100:.0f}% |")
    lines.append("")

    # ---- per-level good rate (full vs naive + biggest deltas)
    levels = ["C1", "C2", "C3", "C4", "C5"]
    lines += ["## 分层级 good 率（full → naive）", "", "| 层级 | n | full | no-governance | Δ |", "|---|---|---|---|---|"]
    for lv in levels:
        lv_ids = [i for i, r in bench.items() if str(r.get("level")) == lv]
        cells = {}
        for arm in ("full", "no-governance"):
            judge, _ = load_arm(arm)
            sub = [i for i in lv_ids if i in judge]
            cells[arm] = (sum(1 for i in sub if good(judge[i])) / len(sub)) if sub else float("nan")
        d = cells["no-governance"] - cells["full"]
        lines.append(f"| {lv} | {len(lv_ids)} | {cells['full']*100:.0f}% | {cells['no-governance']*100:.0f}% | {d*100:+.0f}pp |")
    lines.append("")

    # ---- qshape view (fold capability attribution)
    lines += ["## 分题型 good 率（foldcap 子集）", "", "| qshape | n | " + " | ".join(arms) + " |", "|---|---|" + "---|" * len(arms)]
    for shape in ("enumeration", "aggregation", "point"):
        shape_ids = [i for i, r in bench.items() if r.get("qshape") == shape]
        row = [f"| {shape} | {len(shape_ids)} |"]
        for arm in arms:
            judge, _ = load_arm(arm)
            sub = [i for i in shape_ids if i in judge]
            row.append(f" {(sum(1 for i in sub if good(judge[i])) / len(sub) * 100):.0f}% |" if sub else " - |")
        lines.append("".join(row))
    lines.append("")

    # ---- language split
    lines += ["## 分语言 good 率（full → naive）", "", "| 语言 | n | full | no-governance |", "|---|---|---|---|"]
    for lang in ("zh", "en", "mixed"):
        lang_ids = [i for i, r in bench.items() if str(r.get("language")) == lang]
        cells = {}
        for arm in ("full", "no-governance"):
            judge, _ = load_arm(arm)
            sub = [i for i in lang_ids if i in judge]
            cells[arm] = (sum(1 for i in sub if good(judge[i])) / len(sub) * 100) if sub else float("nan")
        lines.append(f"| {lang} | {len(lang_ids)} | {cells['full']:.0f}% | {cells['no-governance']:.0f}% |")

    out = BASE / "results" / "ablation"
    out.mkdir(parents=True, exist_ok=True)
    (out / "ablation_v1_report.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
