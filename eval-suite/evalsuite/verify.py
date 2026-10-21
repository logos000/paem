"""Manual human-verification workflow for a corpus retrieval eval JSONL.

``gen``   renders a review worksheet (``review_<date>.md``) plus a companion
          decision file (``review_<date>.json``) from an eval file and the
          corpus build its anchors point into.  Every worksheet states the
          three verification criteria up front and gives each question a
          section with the question, the gold anchors with document metadata,
          trap evidence, expected behavior and a ``决定：`` line to fill.
``write`` reads the decisions from the companion ``.json`` and rewrites the
          eval file's ``verified`` flag: approve → true, reject → false (the
          row is kept), fix / no decision → the original value plus a warning.
"""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import random
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evidence.store import read_jsonl, write_jsonl  # noqa: E402

DEFAULT_OUT = ROOT / "data" / "benchmarks" / "corpus-eval"
# Fixed seed so a `--sample N` worksheet and its companion .json stay
# reproducible when the command is re-run on the same eval file.
SAMPLE_SEED = 20261013
DECISIONS = ("approve", "reject", "fix")

CRITERIA = (
    "① 答案必须完整出现在 gold quote 内（不能只是相关，必须包含答案本身）；",
    "② 问法要像真学生会问的（无标题/文件名泄露、无模板腔）；",
    "③ gold 指向正确版本与适用范围的文档；不可答题（gold 为空）核对 expected_behavior 是否该拒。",
)


def _load_documents(corpus: Path) -> dict[str, dict[str, Any]]:
    """Documents of the build directory; only documents.jsonl is needed."""
    path = corpus / "documents.jsonl"
    if not path.is_file():
        raise SystemExit(f"corpus build has no documents.jsonl: {corpus}")
    return {row["doc_uid"]: row for row in read_jsonl(path)}


def _build_id(corpus: Path) -> str:
    manifest = corpus / "manifest.json"
    if manifest.is_file():
        try:
            return str(json.loads(manifest.read_text(encoding="utf-8")).get("build_id") or corpus.name)
        except (OSError, ValueError):
            pass
    return corpus.name


def _meta_line(doc: dict[str, Any] | None) -> str:
    """One-line terms/audience metadata in the old spotcheck style."""
    terms = doc.get("terms") if doc else None
    audience = doc.get("audience") if doc else None
    return f"（{json.dumps(terms, ensure_ascii=False) if terms else ''} {audience or ''}）".strip()


def _doc_title(documents: dict[str, dict[str, Any]], uid: str) -> str:
    doc = documents.get(uid)
    if not doc:
        return f"<语料中无此文档 {uid}>"
    return str(doc.get("title") or uid)


def _clip(text: str, limit: int = 80) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def select_items(items: list[dict[str, Any]], sample: int, ids: list[str]) -> list[dict[str, Any]]:
    if ids:
        by_id = {item.get("id"): item for item in items}
        missing = [ident for ident in ids if ident not in by_id]
        if missing:
            raise SystemExit(f"--ids not in eval file: {', '.join(missing)}")
        return [by_id[ident] for ident in ids]
    if sample and sample < len(items):
        return random.Random(SAMPLE_SEED).sample(items, sample)
    return items


def render_review(items: list[dict[str, Any]], documents: dict[str, dict[str, Any]], *, eval_name: str, build: str, generated: str) -> str:
    lines = [
        f"# 人工核验清单 — eval `{eval_name}`, build `{build}`（{len(items)} 题，生成于 {generated}）",
        "",
        "每题请核三点：",
        *CRITERIA,
        "",
        f"填法：在每题末行「决定：」后写 approve / reject / fix，并把同名 `.json` 里对应 `decision` 填成一致；",
        f"然后运行 `python -m evalsuite.verify write --review <本文件同名.json> --eval <原eval.jsonl>` 生成 verified_eval.jsonl。",
        "",
    ]
    for item in items:
        lines.append(f"## {item.get('id')} [{item.get('category', '')}]")
        lines.append(f"**问**：{item.get('question', '')}")
        for gold in item.get("gold") or []:
            uid = gold.get("doc_uid", "")
            doc = documents.get(uid)
            lines.append(f"**gold**：{_doc_title(documents, uid)} {_meta_line(doc)}")
            lines.append(f"> {gold.get('quote', '')}")
        for trap in item.get("trap_gold") or []:
            lines.append(f"**干扰证据**：{_doc_title(documents, trap.get('doc_uid', ''))} — {_clip(trap.get('quote', ''))}")
        if item.get("expected_behavior"):
            today = f"（today: {item['today']}）" if item.get("today") else ""
            lines.append(f"**类型**：陷阱题（预期行为：{item['expected_behavior']}）{today}")
        elif item.get("today"):
            lines.append(f"**today**：{item['today']}")
        if item.get("notes"):
            lines.append(f"**备注**：{item['notes']}")
        lines.append("决定：")
        lines.append("")
    return "\n".join(lines) + "\n"


def cmd_gen(args: argparse.Namespace) -> None:
    items = list(read_jsonl(args.eval))
    if not items:
        raise SystemExit(f"no items in {args.eval}")
    ids = [ident.strip() for ident in args.ids.split(",") if ident.strip()] if args.ids else []
    selected = select_items(items, args.sample, ids)
    documents = _load_documents(args.corpus)
    generated = date.today().isoformat()
    stem = f"review_{generated}"
    args.out.mkdir(parents=True, exist_ok=True)
    md_path, json_path = args.out / f"{stem}.md", args.out / f"{stem}.json"
    review = render_review(
        selected,
        documents,
        eval_name=args.eval.name,
        build=_build_id(args.corpus),
        generated=generated,
    )
    decisions = [{"id": item.get("id"), "decision": None} for item in selected]
    md_path.write_text(review, encoding="utf-8")
    json_path.write_text(json.dumps(decisions, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"[verify] {len(selected)} 题 → {md_path}")
    print(f"[verify] decisions → {json_path}")
    if len(selected) < len(items):
        print(f"[verify] 抽样 {len(selected)}/{len(items)} 题，其余 {len(items) - len(selected)} 题不在本次核验范围")


def cmd_write(args: argparse.Namespace) -> None:
    decisions = json.loads(Path(args.review).read_text(encoding="utf-8"))
    if isinstance(decisions, dict):  # tolerate {"decisions": [...]}
        decisions = decisions.get("decisions") or []
    by_id: dict[str, Any] = {row.get("id"): row.get("decision") for row in decisions}
    items = list(read_jsonl(args.eval))
    unknown = sorted(str(i) for i in by_id if i not in {item.get("id") for item in items})
    warnings: list[str] = []
    counts = {"approve": 0, "reject": 0, "kept": 0}
    for item in items:
        decision = by_id.get(item.get("id"))
        if isinstance(decision, str):
            decision = decision.strip().lower()
        if decision == "approve":
            item["verified"] = True
            counts["approve"] += 1
        elif decision == "reject":
            item["verified"] = False
            counts["reject"] += 1
        else:
            counts["kept"] += 1
            reason = "无决定" if decision in (None, "") else f"决定为 {decision!r}"
            warnings.append(f"{item.get('id')}: {reason}，verified 保持 {item.get('verified')}")
    out = Path(args.out) if args.out else args.eval.parent / "verified_eval.jsonl"
    if out.parent:
        out.parent.mkdir(parents=True, exist_ok=True)
    count = write_jsonl(out, items)
    print(f"[verify] approve {counts['approve']} / reject {counts['reject']} / 保持原值 {counts['kept']} → {out}（{count} 行）")
    for warning in warnings:
        print(f"[warn] {warning}", file=sys.stderr)
    if unknown:
        print(f"[warn] review 中的 id 不在 eval 文件里：{', '.join(unknown)}", file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("gen", help="生成人工核验 review_<date>.md + 同名 .json")
    gen.add_argument("--eval", type=Path, required=True, help="eval JSONL")
    gen.add_argument("--corpus", type=Path, required=True, help="build 目录（documents.jsonl 所在）")
    gen.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"输出目录（默认 {DEFAULT_OUT}）")
    gen.add_argument("--sample", type=int, default=0, help="随机抽取 N 题（固定种子，可复现）")
    gen.add_argument("--ids", default="", help="逗号分隔的题目 id 列表，优先于 --sample")
    gen.set_defaults(func=cmd_gen)

    write = sub.add_parser("write", help="按 review json 的 decision 更新 verified 并写出新 eval JSONL")
    write.add_argument("--review", type=Path, required=True, help="gen 生成的 review_<date>.json")
    write.add_argument("--eval", type=Path, required=True, help="原 eval JSONL")
    write.add_argument("--out", type=Path, default=None, help="输出文件（默认原 eval 同目录 verified_eval.jsonl）")
    write.set_defaults(func=cmd_write)

    args = parser.parse_args(argv)
    if args.command == "gen" and args.sample and args.ids:
        raise SystemExit("--sample 与 --ids 互斥，请只给一个")
    args.func(args)


if __name__ == "__main__":
    main()
