# -*- coding: utf-8 -*-
"""LLM 评审 CLI：语料核查代理 + golden answer 多维打分 + 失败归因。

  PYTHONIOENCODING=utf-8 PYTHONPATH=. ../llm-corpus-builder/.venv/Scripts/python.exe \
      scripts/llm_judge.py --e2e <e2e json> --eval <题集> \
      --corpus ../llm-corpus-builder/data/corpus/<build> [--sample-validated 25] [--all] [--ids a,b]
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evalsuite.llm_judge import run as judge_run  # noqa: E402

DEFAULT_OUT = ROOT / "data" / "benchmarks" / "corpus-eval" / "results" / "llm_judge"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--e2e", required=True)
    parser.add_argument("--eval", required=True)
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--out", default=None, help="jsonl 输出（断点续跑）")
    parser.add_argument("--report", default=None)
    parser.add_argument("--sample-validated", type=int, default=25)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--ids", default="")
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument("--repeats", type=int, default=1,
                        help="N independent scoring passes into <out>_rN.jsonl (variance via release_gate_v1)")
    args = parser.parse_args()

    e2e = Path(args.e2e)
    out = Path(args.out) if args.out else DEFAULT_OUT / f"judge__{e2e.stem}.jsonl"
    strategy = "all" if args.all else "interesting"
    ids = [x.strip() for x in args.ids.split(",") if x.strip()] or None
    stems = [out] if args.repeats <= 1 else [out.with_name(f"{out.stem}_r{i}.jsonl") for i in range(1, args.repeats + 1)]
    for stem in stems:
        results = judge_run.run_judge(e2e, Path(args.eval), stem,
                                      sample_validated=args.sample_validated, workers=args.workers,
                                      strategy=strategy, ids=ids)
        report = judge_run.aggregate(results)
        report_path = Path(args.report) if (args.report and len(stems) == 1) else stem.with_suffix(".report.md")
        report_path.write_text(report, encoding="utf-8")
        print(report)
        print(f"results: {stem}\nreport:  {report_path}")


if __name__ == "__main__":
    main()
