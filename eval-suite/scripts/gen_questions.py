"""Question-set generation CLI (sub-project workflow, 2026-10-15).

Stages (default: packets → generate → lint → review):

  python scripts/gen_questions.py --corpus data/corpus/<build> generate
  python scripts/gen_questions.py --eval <generated.jsonl> lint
  python scripts/gen_questions.py --eval <generated.jsonl> review
  python scripts/gen_questions.py --report <harness.json> --eval <generated.jsonl> --config active_ir

Artifacts live under data/benchmarks/corpus-eval/ (gitignored data dirs).
The frozen verified_eval_v3 is never touched.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from evalsuite import builder_root  # noqa: E402
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evidence.store import read_jsonl  # noqa: E402
from retrieve.lexical import Corpus  # noqa: E402

EVAL_DIR = ROOT / "data" / "benchmarks" / "corpus-eval"
DEFAULT_OUT = EVAL_DIR / "eval_sp_generated_v1.jsonl"


def stage_generate(args: argparse.Namespace) -> None:
    """Five independent level tasks, each writing its own sub-question-set
    file: <stem>_C1.jsonl … <stem>_C5.jsonl (+ _variants siblings)."""
    from evalsuite.questiongen import generate as gen
    from evalsuite.questiongen.provider import QuestionGenProvider

    corpus = Corpus(Path(args.corpus)) if args.corpus else Corpus.latest(builder_root() / "data" / "corpus")
    provider = QuestionGenProvider()
    import datetime as dt

    default_today = dt.date.today().isoformat()
    stem = Path(args.eval)
    levels = [x.strip().upper() for x in args.levels.split(",") if x.strip()]
    summary: dict[str, Any] = {"build_id": corpus.build_id, "levels": {}}
    for level in levels:
        raw = gen.generate_level(corpus, provider, level, seed=args.seed, limit_packets=args.limit_packets)
        rows = gen.finalize_level(raw, corpus, default_today=default_today)
        out = stem.with_name(f"{stem.stem}_{level}.jsonl")
        stats = gen.write_rows(rows, out)
        stats["raw"] = len(raw)
        summary["levels"][level] = {"out": str(out), **stats}
        print(json.dumps({"level": level, **stats}, ensure_ascii=False), flush=True)
    print(json.dumps(summary, ensure_ascii=False))


def stage_lint(args: argparse.Namespace) -> None:
    from evalsuite.questiongen.lint_ext import lint_generated

    eval_path = Path(args.eval)
    variant_path = eval_path.with_name(eval_path.stem + "_variants.jsonl")
    targets = [p for p in (eval_path, variant_path) if p.is_file()]
    for target in targets:
        base = subprocess.run(
            [sys.executable, "-m", "evalsuite.lint", str(target), *(["--corpus", args.corpus] if args.corpus else [])],
            cwd=ROOT, capture_output=True, text=True,
        )
        if base.returncode != 0:
            print(f"eval.lint (frozen base rules) FAILED for {target.name}:\n" + base.stdout[-4000:] + base.stderr[-2000:])
            sys.exit(1)
    items = [row for target in targets for row in read_jsonl(target)]
    existing = [EVAL_DIR / name for name in ("verified_eval_v3.jsonl", "eval.jsonl", "eval_domain_48.jsonl")]
    errors = lint_generated(items, existing_files=existing)
    if errors:
        print("generated-field lint FAILED:")
        for line in errors[:80]:
            print(" ", line)
        sys.exit(1)
    print(json.dumps({"lint": "ok", "items": len(items), "files": [t.name for t in targets]}))


def stage_review(args: argparse.Namespace) -> None:
    from evalsuite.questiongen.review import review_rows, write_review
    from evalsuite.questiongen.provider import QuestionGenProvider

    items = list(read_jsonl(Path(args.eval)))
    provider = QuestionGenProvider()
    decisions, reasons = review_rows(items, provider, batch=args.batch)
    stamp = args.stamp or dt.date.today().isoformat()
    review_path, reasons_path = write_review(decisions, reasons, EVAL_DIR, stamp=stamp)
    counts = {}
    for d in decisions:
        counts[d["decision"]] = counts.get(d["decision"], 0) + 1
    print(json.dumps({"review": str(review_path), "reasons": str(reasons_path), "counts": counts}, ensure_ascii=False))


def stage_refine(args: argparse.Namespace) -> None:
    """Autofix mechanical field gaps, then drop rows the frozen base lint rejects."""
    import re as _re

    eval_path = Path(args.eval)
    variant_path = eval_path.with_name(eval_path.stem + "_variants.jsonl")
    rows = list(read_jsonl(eval_path))
    variants = list(read_jsonl(variant_path)) if variant_path.is_file() else []
    default_today = dt.date.today().isoformat()
    autofixed = 0
    kept: list[dict[str, Any]] = []
    for row in rows:
        category = row.get("category", "")
        if category == "version_latest":
            row.setdefault("today", default_today)
            autofixed += 1
        if category == "conflict_flag":
            row.setdefault("today", default_today)
            row["expected_behavior"] = "flag_conflict"
            if len(row.get("gold") or []) < 2:
                continue  # a one-sided conflict question is not a conflict question
            autofixed += 1
        kept.append(row)

    # lint-filter: drop rows the frozen base lint still rejects
    tmp = eval_path.with_suffix(".tmp.jsonl")
    with tmp.open("w", encoding="utf-8") as handle:
        for row in kept:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    base = subprocess.run(
        [sys.executable, "-m", "evalsuite.lint", str(tmp), *(["--corpus", args.corpus] if args.corpus else [])],
        cwd=ROOT, capture_output=True, text=True,
    )
    tmp.unlink()
    bad_ids: set[str] = set()
    if base.returncode != 0:
        match = _re.search(r"\{.*\}", base.stdout, _re.S)
        report = json.loads(match.group(0)) if match else {}
        for error in report.get("errors", []):
            ident = error.split(":", 1)[0].strip()
            if ident.startswith("qg-"):
                bad_ids.add(ident)
    survivors = [row for row in kept if row.get("id") not in bad_ids]
    good_variants = [v for v in variants if v.get("id") not in bad_ids]
    with eval_path.open("w", encoding="utf-8") as handle:
        for row in survivors:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    with variant_path.open("w", encoding="utf-8") as handle:
        for row in good_variants:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps({"refine": "ok", "kept": len(survivors), "dropped_by_lint": len(bad_ids),
                      "autofixed": autofixed, "variants": len(good_variants)}, ensure_ascii=False))


def stage_report(args: argparse.Namespace) -> None:
    from evalsuite.questiongen.report import dimension_report

    text = dimension_report(Path(args.report), Path(args.eval), args.config)
    out = Path(args.out) if args.out else (EVAL_DIR / "results" / f"qg_report_{Path(args.report).stem}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"\nwritten: {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="stage", required=True)

    p = sub.add_parser("generate")
    p.add_argument("--corpus", default=None)
    p.add_argument("--eval", default=str(DEFAULT_OUT), help="stem: writes <stem>_C<n>.jsonl per level")
    p.add_argument("--seed", type=int, default=20261015)
    p.add_argument("--limit-packets", type=int, default=None, help="smoke run on the first N packets")
    p.add_argument("--levels", default="C1,C2,C3,C4,C5")
    p.add_argument("--workers", type=int, default=20)
    p.set_defaults(func=stage_generate)

    p = sub.add_parser("lint")
    p.add_argument("--eval", default=str(DEFAULT_OUT))
    p.add_argument("--corpus", default=None)
    p.set_defaults(func=stage_lint)

    p = sub.add_parser("refine")
    p.add_argument("--eval", default=str(DEFAULT_OUT))
    p.add_argument("--corpus", default=None)
    p.set_defaults(func=stage_refine)

    p = sub.add_parser("review")
    p.add_argument("--eval", default=str(DEFAULT_OUT))
    p.add_argument("--batch", type=int, default=20)
    p.add_argument("--stamp", default=None)
    p.set_defaults(func=stage_review)

    p = sub.add_parser("report")
    p.add_argument("--report", required=True, help="harness results JSON")
    p.add_argument("--eval", default=str(DEFAULT_OUT))
    p.add_argument("--config", default="active_ir")
    p.add_argument("--out", default=None)
    p.set_defaults(func=stage_report)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
