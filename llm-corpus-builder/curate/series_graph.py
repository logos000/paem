"""Series candidate graph: deterministic series × LLM candidate keys (P3).

LLM ``series_key_candidate`` 值**不覆盖**确定性版本链（documents.jsonl 的
series_id/superseded_by 不动）。本模块把两侧合并成一张候选关系图：

- 确定性侧：series.json 的每条链（权威）；
- 候选侧：sidecar 里 ``status ≥ source_supported`` 的 series key 分组
  （附每条边的 provenance：模型/锚点）；
- 跨侧链接：候选 key 的成员若落在 ≥2 条不同确定性链里（或含无链文档），
  即为"可能的漏链/误拆分"信号，输出到 ``cross_links`` 供人工与后续门禁
  裁决——绝不自动改链。

输出 ``series_candidate_graph.json``::

    {"deterministic": [{"series_id", "key", "size", "members": [doc_uid]}],
     "candidates": [{"key", "size", "members": [{"doc_uid", "status",
                   "anchors": [block_id]}]}],
     "cross_links": [{"candidate_key", "doc_uids_by_series":
                      {"s_…": [...], "(none)": [...]}}]}
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from annotate.schema import status_at_least
from evidence.store import read_jsonl

__all__ = ["merge_series_graph"]


def _llm_series(annotations: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """candidate key -> member records, gated at source_supported."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for record in annotations:
        field = (record.get("llm_annotations") or {}).get("series_key_candidate") or {}
        key = field.get("value")
        if not key or not status_at_least(field.get("status")):
            continue  # unanchored candidates never enter the graph
        anchors = sorted({anchor.get("block_id") for span in field.get("evidence_spans") or [] for anchor in span.get("anchors") or [] if anchor.get("block_id")})
        groups.setdefault(key, []).append({"doc_uid": record["doc_uid"], "status": field.get("status"), "anchors": anchors})
    return groups


def merge_series_graph(build_dir: Path) -> dict[str, Any]:
    build_dir = Path(build_dir)
    series = json.loads((build_dir / "series.json").read_text(encoding="utf-8")) if (build_dir / "series.json").is_file() else []
    annotations = list(read_jsonl(build_dir / "llm_annotations.jsonl")) if (build_dir / "llm_annotations.jsonl").is_file() else []
    series_by_doc: dict[str, str] = {}
    for entry in series:
        for member in entry.get("members", []):
            series_by_doc[member.get("doc_uid", "")] = entry.get("series_id", "")

    candidates = _llm_series(annotations)
    candidate_records = [
        {"key": key, "size": len(members), "members": members}
        for key, members in sorted(candidates.items())
    ]
    cross_links = []
    for key, members in sorted(candidates.items()):
        by_series: dict[str, list[str]] = {}
        for member in members:
            by_series.setdefault(series_by_doc.get(member["doc_uid"], "(none)"), []).append(member["doc_uid"])
        # A candidate spanning several deterministic chains (or mixing chained
        # with unchained docs) is a missed-chain / mis-split signal.
        if len(by_series) >= 2 or ("(none)" in by_series and len(by_series) >= 2) or ("(none)" in by_series and len(by_series) == 1 and len(next(iter(by_series.values()))) >= 2):
            cross_links.append({"candidate_key": key, "doc_uids_by_series": by_series})
    return {
        "deterministic": [
            {"series_id": entry.get("series_id", ""), "key": entry.get("key", ""), "size": entry.get("size", 0),
             "members": [m.get("doc_uid", "") for m in entry.get("members", [])]}
            for entry in series
        ],
        "candidates": candidate_records,
        "cross_links": cross_links,
    }
