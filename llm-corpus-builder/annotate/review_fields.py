"""Field-level human verification for LLM annotations (P2).

与 eval/verify.py 同形态的两步式人工核验 + 指标命令：

``gen``      从 ``llm_annotations.jsonl``（和 ``llm_row_contexts.jsonl``）抽样
             (doc, field) 单元，渲染核验清单 ``field_review_<date>.md`` + 同名
             决策 ``.json``。每条列出字段值、状态、锚点 span 及其指向的原文块
             上下文，核验者不需要打开语料文件即可裁决。
``write``    读回决策，落 ``llm_field_review.jsonl``（doc_uid/field/decision/
             corrected_value/reason/reviewer/decided_at），并打印通过率。
``metrics``  字段级指标表：attempted / supported / unsupported / uncertain 率
             （来自 sidecar），加上人工 precision 与修正率（来自 review 文件，
             若存在）。可选 ``--ranked-split N`` 把样本切成"高价值前 N 篇"与
             其余（selection-bias 对照），``--compare <build>`` 输出两个 build
             的并排对照。

核验标准（每条 field 单元）：
① 值本身正确（doc_type/audience/terms/series 与文档实际内容一致）；
② 锚点 span 确实支撑该值（不是碰巧出现的字面）；
③ fix 时给出修正值；reject 表示该字段本不该标注/无法判断。
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
import random
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from annotate.schema import ALIAS_SUBFIELDS, EVIDENCE_FIELDS, status_at_least  # noqa: E402
from annotate.select import select_pilot_docs  # noqa: E402
from evidence.store import read_jsonl, write_jsonl  # noqa: E402

__all__ = ["main"]

DEFAULT_OUT = ROOT / "data" / "benchmarks" / "field-review"
SAMPLE_SEED = 20261014
DECISIONS = ("approve", "fix", "reject")
DEFAULT_FIELDS = EVIDENCE_FIELDS + ("row_context",)
ALL_REVIEWABLE = EVIDENCE_FIELDS + tuple(f"aliases.{sub}" for sub in ALIAS_SUBFIELDS) + ("summary_1s", "row_context")


# --------------------------------------------------------------------- item extraction


def _field_entries(record: dict, fields: list[str]) -> list[dict[str, Any]]:
    """Flatten one annotation record into reviewable (field, value, spans) units."""
    ann = record.get("llm_annotations") or {}
    out: list[dict[str, Any]] = []
    for name in fields:
        if name == "row_context":
            continue  # handled separately from llm_row_contexts.jsonl
        if name.startswith("aliases."):
            sub = name.split(".", 1)[1]
            field = (ann.get("aliases") or {}).get(sub) or {}
            value = field.get("value") or []
            spans = field.get("evidence_spans") or []
            if not value and not field.get("rejected_value"):
                continue
            out.append({"field": name, "value": value, "status": field.get("status"),
                        "rejected_value": field.get("rejected_value") or [], "spans": spans})
            continue
        field = ann.get(name)
        if not isinstance(field, dict):
            continue
        value = field.get("value")
        rejected = field.get("rejected_value")
        if (value in (None, [], "")) and (rejected in (None, [], "")):
            continue
        out.append({"field": name, "value": value, "status": field.get("status"),
                    "rejected_value": rejected, "spans": field.get("evidence_spans") or []})
    return out


def _row_context_entries(row_record: dict) -> list[dict[str, Any]]:
    """One reviewable unit per rule-rebuilt row context line."""
    out = []
    for row in row_record.get("rows") or []:
        out.append({
            "field": "row_context",
            "value": {"derived_text": row.get("derived_text"), "answer_bearing": row.get("answer_bearing")},
            "status": row.get("status"),
            "rejected_value": None,
            "spans": [{"text": row.get("derived_text") or "", "anchors": [
                {"block_id": row.get("block_id"), "table_id": row.get("table_id"), "row_index": row.get("row_index")},
            ], "match_count": 1}] if row.get("derived_text") else [],
        })
    return out


def _excerpt(blocks_by_uid: dict[str, dict[str, dict]], doc_uid: str, anchor: dict, limit: int = 160) -> str:
    block = blocks_by_uid.get(doc_uid, {}).get(anchor.get("block_id"))
    if not block:
        return "<锚点块不在 blocks.jsonl>"
    text = " ".join(str(block.get("text") or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def collect_items(corpus: Path, fields: list[str], sample: int, ids: list[str]) -> list[dict[str, Any]]:
    annotations = list(read_jsonl(corpus / "llm_annotations.jsonl"))
    rows_by_doc = {rec["doc_uid"]: rec for rec in read_jsonl(corpus / "llm_row_contexts.jsonl")} if (corpus / "llm_row_contexts.jsonl").is_file() else {}
    items: list[dict[str, Any]] = []
    for record in annotations:
        for entry in _field_entries(record, fields):
            items.append({"id": f"{record['doc_uid']}::{entry['field']}", "doc_uid": record["doc_uid"], **entry})
        if "row_context" in fields:
            for entry in _row_context_entries(rows_by_doc.get(record["doc_uid"], {})):
                items.append({"id": f"{record['doc_uid']}::row_context#{entry['value'].get('derived_text', '')[:24]}", "doc_uid": record["doc_uid"], **entry})
    if ids:
        wanted = set(ids)
        items = [item for item in items if item["id"] in wanted]
    elif sample and sample < len(items):
        # Row contexts expand per table row and would crowd out metadata
        # fields; split the quota evenly across fields first, then sample.
        by_field: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            by_field.setdefault(item["field"], []).append(item)
        quota = max(1, sample // max(1, len(by_field)))
        rng = random.Random(SAMPLE_SEED)
        picked: list[dict[str, Any]] = []
        leftover: list[dict[str, Any]] = []
        for field in sorted(by_field):
            pool = by_field[field]
            take = min(quota, len(pool))
            picked.extend(rng.sample(pool, take))
            leftover.extend(pool[take:] if take < len(pool) else [])
        rng.shuffle(leftover)
        picked.extend(leftover[: sample - len(picked)])
        items = picked
    return items


# --------------------------------------------------------------------- gen


def render_worksheet(items: list[dict], documents: dict[str, dict], blocks_by_uid: dict, *, build: str, generated: str, fields: list[str]) -> str:
    lines = [
        f"# 字段级人工核验 — build `{build}`（{len(items)} 条，生成于 {generated}）",
        "",
        "核验标准：① 值正确；② 锚点 span 确实支撑该值；③ fix 需给 corrected_value，reject 表示无法判断/不该标注。",
        f"字段范围：{', '.join(fields)}。rejected_value 是被门禁拦截的模型原值（仅供对照，不入索引）。",
        "填法：同名 `.json` 里把每条 `decision` 填 approve/fix/reject（fix 附 corrected_value），",
        "然后运行 `python -m annotate.review_fields write --review <同名.json> --corpus <build> --reviewer <你的名字>`。",
        "",
    ]
    for item in items:
        doc = documents.get(item["doc_uid"]) or {}
        lines.append(f"## {item['id']}")
        lines.append(f"**文档**：{doc.get('title') or item['doc_uid']}（{doc.get('site')}/{doc.get('doc_type')}；规则值 audience={doc.get('audience')}, terms={json.dumps(doc.get('terms'), ensure_ascii=False)}）")
        value = item["value"]
        if isinstance(value, dict):
            lines.append(f"**值**：{json.dumps(value, ensure_ascii=False)}")
        else:
            lines.append(f"**值**：{json.dumps(value, ensure_ascii=False)}")
        lines.append(f"**状态**：{item.get('status')}")
        if item.get("rejected_value") not in (None, [], ""):
            lines.append(f"**被拒原值**：{json.dumps(item['rejected_value'], ensure_ascii=False)}")
        for span in item.get("spans") or []:
            text = span.get("text") if isinstance(span, dict) else str(span)
            anchor = (span.get("anchors") or [{}])[0] if isinstance(span, dict) else {}
            where = anchor.get("block_id") or ""
            if anchor.get("table_id") is not None:
                where += f" table={anchor.get('table_id')}#{anchor.get('row_index')}"
            lines.append(f"> 锚点[{where}]：{text}")
            if where:
                lines.append(f"  原文块：{_excerpt(blocks_by_uid, item['doc_uid'], anchor)}")
        lines.append("决定：")
        lines.append("")
    return "\n".join(lines) + "\n"


def cmd_gen(args: argparse.Namespace) -> None:
    fields = [f.strip() for f in args.fields.split(",") if f.strip()]
    unknown = [f for f in fields if f not in ALL_REVIEWABLE]
    if unknown:
        raise SystemExit(f"不可核验字段 {unknown}；可用：{list(ALL_REVIEWABLE)}")
    ids = [i.strip() for i in args.ids.split(",") if i.strip()] if args.ids else []
    items = collect_items(args.corpus, fields, args.sample, ids)
    if not items:
        raise SystemExit("没有可核验条目（sidecar 为空或字段过滤后为空）")
    documents = {row["doc_uid"]: row for row in read_jsonl(args.corpus / "documents.jsonl")}
    blocks_by_uid: dict[str, dict[str, dict]] = {}
    for block in read_jsonl(args.corpus / "blocks.jsonl"):
        blocks_by_uid.setdefault(block["doc_uid"], {})[block["block_id"]] = block
    generated = date.today().isoformat()
    stem = f"field_review_{generated}"
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"{stem}.md").write_text(
        render_worksheet(items, documents, blocks_by_uid, build=args.corpus.name, generated=generated, fields=fields),
        encoding="utf-8")
    (args.out / f"{stem}.json").write_text(
        json.dumps([{"id": item["id"], "decision": None, "corrected_value": None, "reason": ""} for item in items],
                   ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"[review_fields] {len(items)} 条 → {args.out / (stem + '.md')}")
    print(f"[review_fields] decisions → {args.out / (stem + '.json')}")
    from collections import Counter
    print("[review_fields] 字段分布:", dict(Counter(item["field"] for item in items)))


# --------------------------------------------------------------------- write


def cmd_write(args: argparse.Namespace) -> None:
    decisions = json.loads(args.review.read_text(encoding="utf-8"))
    if isinstance(decisions, dict):
        decisions = decisions.get("decisions") or []
    corpus_items = {row["id"]: row for row in collect_items(args.corpus, list(ALL_REVIEWABLE), 0, [])}
    decided_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    records = []
    counts = {"approve": 0, "fix": 0, "reject": 0, "undecided": 0}
    for row in decisions:
        decision = str(row.get("decision") or "").strip().lower()
        if decision not in DECISIONS:
            counts["undecided"] += 1
            continue
        counts[decision] += 1
        item = corpus_items.get(row.get("id"), {})
        records.append({
            "doc_uid": item.get("doc_uid") or row.get("id", "").split("::")[0],
            "field": item.get("field") or row.get("id", "").split("::")[-1].split("#")[0],
            "decision": decision,
            "value": item.get("value"),
            "corrected_value": row.get("corrected_value"),
            "reason": row.get("reason") or "",
            "reviewer": args.reviewer,
            "decided_at": decided_at,
        })
    write_jsonl(args.corpus / "llm_field_review.jsonl", records)
    total = counts["approve"] + counts["fix"] + counts["reject"]
    precision = counts["approve"] / total if total else None
    print(f"[review_fields] approve {counts['approve']} / fix {counts['fix']} / reject {counts['reject']} / 未决 {counts['undecided']}")
    if precision is not None:
        print(f"[review_fields] 人工 precision（approve/已决）= {precision:.3f}；修正率 = {counts['fix'] / total:.3f}")
    print(f"[review_fields] → {args.corpus / 'llm_field_review.jsonl'}")


# --------------------------------------------------------------------- metrics


def _field_stats(annotations: list[dict], fields: list[str]) -> dict[str, dict[str, Any]]:
    """Per-field attempted/supported/unsupported/uncertain counts from sidecars."""
    stats: dict[str, dict[str, Any]] = {name: {"attempted": 0, "supported": 0, "unsupported": 0, "uncertain": 0} for name in fields}
    for record in annotations:
        ann = record.get("llm_annotations") or {}
        unsupported = set(record.get("validation", {}).get("unsupported_fields") or [])
        attempted = set(record.get("validation", {}).get("attempted_fields") or [])
        for name in fields:
            if name == "row_context":
                continue
            if name.startswith("aliases."):
                field = (ann.get("aliases") or {}).get(name.split(".", 1)[1]) or {}
                has_value = bool(field.get("value")) or bool(field.get("rejected_value"))
            else:
                field = ann.get(name)
                field = field if isinstance(field, dict) else {}
                has_value = (field.get("value") not in (None, [], "")) or bool(field.get("rejected_value"))
            if name in attempted or (has_value and name in unsupported):
                stats[name]["attempted"] += 1
                if name in unsupported:
                    stats[name]["unsupported"] += 1
                elif status_at_least(field.get("status")):
                    stats[name]["supported"] += 1
                else:
                    stats[name]["uncertain"] += 1
    return stats


def _review_stats(corpus: Path) -> dict[str, dict[str, Any]]:
    path = corpus / "llm_field_review.jsonl"
    if not path.is_file():
        return {}
    per_field: dict[str, dict[str, int]] = {}
    for row in read_jsonl(path):
        bucket = per_field.setdefault(row.get("field"), {"approve": 0, "fix": 0, "reject": 0})
        if row.get("decision") in bucket:
            bucket[row["decision"]] += 1
    out = {}
    for field, counts in per_field.items():
        decided = counts["approve"] + counts["fix"] + counts["reject"]
        out[field] = {**counts, "decided": decided,
                      "precision": round(counts["approve"] / decided, 4) if decided else None,
                      "correction_rate": round(counts["fix"] / decided, 4) if decided else None}
    return out


def _format_table(rows: list[tuple[str, ...]]) -> str:
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + "|".join(" --- " for _ in rows[0]) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows[1:]]
    return "\n".join(lines)


def _stats_rows(title: str, stats: dict[str, dict[str, Any]], review: dict[str, dict[str, Any]]) -> list[tuple[str, ...]]:
    rows = [(title, "attempted", "supported", "unsupported", "uncertain", "support%", "人工prec", "修正率")]
    for name, s in stats.items():
        if not s["attempted"] and name not in review:
            continue
        support = f"{s['supported'] / s['attempted']:.1%}" if s["attempted"] else "—"
        rev = review.get(name) or {}
        prec = f"{rev['precision']:.1%}" if rev.get("precision") is not None else "—"
        corr = f"{rev['correction_rate']:.1%}" if rev.get("correction_rate") is not None else "—"
        rows.append((name, str(s["attempted"]), str(s["supported"]), str(s["unsupported"]), str(s["uncertain"]), support, prec, corr))
    return rows


def cmd_metrics(args: argparse.Namespace) -> None:
    fields = [f.strip() for f in args.fields.split(",") if f.strip()] or list(ALL_REVIEWABLE)
    if args.ranked_split:
        annotations = list(read_jsonl(args.corpus / "llm_annotations.jsonl"))
        top = set(select_pilot_docs(args.corpus, limit=args.ranked_split))
        head = [r for r in annotations if r["doc_uid"] in top]
        tail = [r for r in annotations if r["doc_uid"] not in top]
        print(f"== 高价值前 {args.ranked_split} 篇（n={len(head)}）==")
        print(_format_table(_stats_rows("field", _field_stats(head, fields), _review_stats(args.corpus))))
        print()
        print(f"== 其余（n={len(tail)}）——selection-bias 对照 ==")
        print(_format_table(_stats_rows("field", _field_stats(tail, fields), {})))
        return
    stats = _field_stats(list(read_jsonl(args.corpus / "llm_annotations.jsonl")), fields)
    review = _review_stats(args.corpus)
    print(_format_table(_stats_rows(f"field@{args.corpus.name}", stats, review)))
    if args.compare:
        other_stats = _field_stats(list(read_jsonl(args.compare / "llm_annotations.jsonl")), fields)
        other_review = _review_stats(args.compare)
        print()
        print(_format_table(_stats_rows(f"field@{args.compare.name}", other_stats, other_review)))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("gen", help="生成字段级核验清单 field_review_<date>.md + 同名 .json")
    gen.add_argument("--corpus", type=Path, required=True)
    gen.add_argument("--fields", default=",".join(DEFAULT_FIELDS))
    gen.add_argument("--sample", type=int, default=200, help="随机抽取 N 条（固定种子）")
    gen.add_argument("--ids", default="", help="逗号分隔的条目 id（doc::field），优先于 --sample")
    gen.add_argument("--out", type=Path, default=DEFAULT_OUT)
    gen.set_defaults(func=cmd_gen)

    write = sub.add_parser("write", help="按决策 json 写 llm_field_review.jsonl 并打印通过率")
    write.add_argument("--review", type=Path, required=True)
    write.add_argument("--corpus", type=Path, required=True)
    write.add_argument("--reviewer", default="human")
    write.set_defaults(func=cmd_write)

    metrics = sub.add_parser("metrics", help="字段级指标表（含人工 precision，若有 review）")
    metrics.add_argument("--corpus", type=Path, required=True)
    metrics.add_argument("--fields", default=",".join(ALL_REVIEWABLE))
    metrics.add_argument("--ranked-split", type=int, default=0, help="按高价值前 N 篇 vs 其余切分（selection-bias 对照）")
    metrics.add_argument("--compare", type=Path, default=None, help="另一 build 的 sidecar 并排对照")
    metrics.set_defaults(func=cmd_metrics)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
