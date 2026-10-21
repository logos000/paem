"""Construction-period quality metrics for a corpus build.

Pure aggregation over the build artifacts (documents.jsonl / blocks.jsonl /
dedup.json / series.json) — describes *what the builder produced*, independent
of any downstream retrieval score.  Task-period metrics (recall, ranking,
abstention) live in ``eval.harness``; the two groups must be reported
separately and never substituted for each other.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from curate.metadata import SITE_DEFAULTS
from evidence.store import read_jsonl


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def compute(build: Path, today: str | None = None) -> dict:
    documents = list(read_jsonl(build / "documents.jsonl"))
    blocks = list(read_jsonl(build / "blocks.jsonl"))
    dedup = json.loads((build / "dedup.json").read_text(encoding="utf-8"))
    series = json.loads((build / "series.json").read_text(encoding="utf-8"))

    status = Counter(d.get("status") for d in documents)
    active = [d for d in documents if d.get("status") == "active"]
    kinds = Counter(d.get("page_kind") for d in active)
    doc_types = Counter(d.get("doc_type") for d in active)
    audiences = Counter(d.get("audience") for d in active)

    # 受众"显式检出"代理：与站点默认值不同的 audience 视为正文/标题显式识别，
    # 等于默认值时无法区分"确认默认"与"未检出"（P1 修复项 1 的量化基线）。
    def explicit_audience(d: dict) -> bool:
        return d.get("audience") != SITE_DEFAULTS.get(d.get("site", ""), {}).get("audience")

    dup_members = [d for d in documents if d.get("duplicate_of")]
    series_members = [d for d in documents if d.get("series_id")]
    superseded_active = [d for d in active if d.get("superseded_by")]
    multi_latest = [
        s for s in series
        if isinstance(s, dict) and sum(1 for m in s.get("members", []) if m.get("is_latest")) > 1
    ] if series and isinstance(series[0], dict) else []

    with_terms = [d for d in active if d.get("terms")]
    term_sources = Counter((d.get("term_source") or "none") for d in active)
    with_published = [d for d in active if d.get("published")]
    future_published = [
        d for d in with_published
        if today and str(d.get("published", ""))[:10] > today
    ]
    warned = [d for d in active if d.get("warnings")]

    table_rows = [b for b in blocks if b.get("kind") == "table_row"]
    table_rows_with_context = [b for b in table_rows if b.get("heading_path")]
    blocks_with_context = [b for b in blocks if b.get("heading_path")]

    # 文档内重复块：同 doc 下规范化文本（去空白）出现 >1 次的块数。
    seen: dict[tuple[str, str], int] = Counter()
    for b in blocks:
        seen[(b["doc_uid"], "".join(b.get("text", "").split()))] += 1
    intra_dup_blocks = sum(c - 1 for c in seen.values() if c > 1)

    return {
        "build_id": build.name,
        "documents": {
            "total": len(documents),
            "by_status": dict(status),
            "active": len(active),
            "active_ratio": _rate(len(active), len(documents)),
            "page_kind_active": dict(kinds),
            "doc_type_active_top10": doc_types.most_common(10),
            "audience_active_top10": audiences.most_common(10),
            "explicit_audience": sum(1 for d in active if explicit_audience(d)),
            "explicit_audience_ratio": _rate(sum(1 for d in active if explicit_audience(d)), len(active)),
        },
        "redundancy": {
            "duplicate_members": len(dup_members),
            "duplicate_ratio": _rate(len(dup_members), len(documents)),
            "dedup_groups": len(dedup) if isinstance(dedup, list) else len(dedup.get("groups", [])),
            "intra_doc_duplicate_blocks": intra_dup_blocks,
            "intra_doc_duplicate_ratio": _rate(intra_dup_blocks, len(blocks)),
        },
        "versions": {
            "series": len(series) if isinstance(series, list) else len(series.get("series", [])),
            "series_members": len(series_members),
            "series_member_ratio": _rate(len(series_members), len(documents)),
            "superseded_active": len(superseded_active),
            "multi_latest_series": len(multi_latest),
        },
        "temporal": {
            "terms_coverage": _rate(len(with_terms), len(active)),
            "term_source": dict(term_sources),
            "published_coverage": _rate(len(with_published), len(active)),
            "published_in_future": len(future_published),
        },
        "structure": {
            "blocks_total": len(blocks),
            "table_rows": len(table_rows),
            "table_rows_with_heading": len(table_rows_with_context),
            "table_row_context_ratio": _rate(len(table_rows_with_context), len(table_rows)),
            "blocks_with_heading": len(blocks_with_context),
            "block_heading_ratio": _rate(len(blocks_with_context), len(blocks)),
        },
        "gates": {
            "warned_active_docs": len(warned),
            "warned_ratio": _rate(len(warned), len(active)),
        },
    }


def render(m: dict) -> str:
    d, r, v, t, s, g = m["documents"], m["redundancy"], m["versions"], m["temporal"], m["structure"], m["gates"]
    lines = [
        f"# 构建期质量指标 — build `{m['build_id']}`",
        "",
        "## 文档层",
        f"- 文档总数 {d['total']}（by status: {d['by_status']}），active {d['active']}（{d['active_ratio']}）",
        f"- active page_kind: {d['page_kind_active']}",
        f"- active doc_type top10: {d['doc_type_active_top10']}",
        f"- active audience top10: {d['audience_active_top10']}",
        f"- 显式受众（≠站点默认）: {d['explicit_audience']}（{d['explicit_audience_ratio']}）— P1-1 修复基线",
        "",
        "## 冗余层",
        f"- 重复成员 {r['duplicate_members']}（{r['duplicate_ratio']}），去重组 {r['dedup_groups']}",
        f"- 文档内重复块 {r['intra_doc_duplicate_blocks']}（{r['intra_doc_duplicate_ratio']}）— P1-2 修复基线",
        "",
        "## 版本层",
        f"- 版本链 {v['series']} 条，成员 {v['series_members']}（{v['series_member_ratio']}）",
        f"- active 但被 supersede: {v['superseded_active']}；多条 is_latest 的系列: {v['multi_latest_series']}",
        "",
        "## 时间层",
        f"- terms 覆盖 {t['terms_coverage']}（来源: {t['term_source']}）",
        f"- published 覆盖 {t['published_coverage']}；未来日期 {t['published_in_future']} 条",
        "",
        "## 结构层",
        f"- 块总数 {s['blocks_total']}，表格行 {s['table_rows']}（带表头/章节上下文 {s['table_row_context_ratio']}）",
        f"- 块级 heading 覆盖 {s['block_heading_ratio']}",
        "",
        "## 门禁层",
        f"- 带 warnings 的 active 文档 {g['warned_active_docs']}（{g['warned_ratio']}）",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True, help="build 目录")
    parser.add_argument("--out", type=Path, default=None, help="输出目录（默认 build 目录本身）")
    parser.add_argument("--today", default=None, help="未来日期判断基准，默认今天")
    args = parser.parse_args(argv)
    today = args.today or date.today().isoformat()
    metrics = compute(args.corpus, today)
    out = args.out or args.corpus
    out.mkdir(parents=True, exist_ok=True)
    (out / "build_metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "build_metrics.md").write_text(render(metrics), encoding="utf-8")
    print(render(metrics))


if __name__ == "__main__":
    main()
