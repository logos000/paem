"""Build-time cross-document fact-conflict candidates (P3).

两份现行来源不会被静默合并：对同一版本系列（确定性 series，或 LLM 候选
series key）内的现行文档抽取带锚点的事实断言（截止日期、学分、联系方式、
名额、绩点条件），同组内同一事实类型出现不同规范值即产出 conflict candidate
记录，携带双方来源、有效日期、authority 与验证状态（始终 ``candidate``——
是否成立由人工/门禁裁决，本模块只负责不漏不删地摆上台面）。

输出 ``conflict_candidates.jsonl``（每行一个冲突候选）::

    {"fact_kind": "deadline", "series_id": "s_…|candidate:<key>",
     "series_source": "deterministic" | "llm_candidate",
     "values": [{"doc_uid", "block_id", "value", "span", "published",
                 "is_latest", "authority_rank"}],
     "status": "candidate", "created_at": "…"}
"""

from __future__ import annotations

import json
import re
from domain_profile import load_profile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from evidence.store import read_jsonl

__all__ = ["extract_fact_claims", "detect_fact_conflicts", "scan_build_conflicts"]

# -- claim patterns ------------------------------------------------------------
# Each pattern yields (kind, canonical value, span).  Values are canonicalized
# so "2026年9月30日" and "2026-09-30" compare equal; the span stays verbatim.

_CONFLICT_PATTERNS = load_profile().section("build").get("conflicts", {})

def _conflict_re(key: str, extra_flags: int = 0):
    spec = _CONFLICT_PATTERNS.get(key, "")
    flags = extra_flags
    if isinstance(spec, dict):
        if str(spec.get("flags", "")).lower().find("i") >= 0:
            flags |= __import__("re").I
        spec = spec.get("pattern", "")
    return __import__("re").compile(str(spec), flags)
_DEADLINE_DATE_RE = _conflict_re("deadline_date", re.I)
_DEADLINE_HINT_RE = _conflict_re("deadline_hint", re.I)
_CREDITS_RE = _conflict_re("credits", re.I)
_EMAIL_RE = _conflict_re("email")
_PHONE_RE = _conflict_re("phone")
_QUOTA_RE = _conflict_re("quota")
_GPA_RE = _conflict_re("gpa", re.I)

_MONTHS = {name: index for index, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def _canon_date(text: str) -> str:
    text = text.strip()
    match = re.fullmatch(r"(\d{4})[年./-](\d{1,2})[月./-](\d{1,2})日?", text)
    if match:
        return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    match = re.fullmatch(r"(?i)([A-Za-z]+)\s+(\d{1,2})(?:,?\s+(\d{4}))?", text)
    if match:
        month = _MONTHS.get(match.group(1)[:3].casefold())
        year = match.group(3) or ""
        return f"{year}-{month:02d}-{int(match.group(2)):02d}" if month and year else text
    return text


def extract_fact_claims(blocks: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    """Anchored fact claims from one document's blocks.

    Deadline claims require a hint word in the same block (截止/deadline/…);
    a bare date is a start date, a period, or noise, and flagging it as a
    conflict candidate would bury the real ones.
    """
    claims: list[dict[str, str]] = []
    for block in blocks:
        text = str(block.get("text") or "")
        if not text:
            continue
        block_id = str(block.get("block_id") or "")
        if _DEADLINE_HINT_RE.search(text):
            for match in _DEADLINE_DATE_RE.finditer(text):
                value = _canon_date(match.group(0))
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    claims.append({"kind": "deadline", "value": value, "span": match.group(0), "block_id": block_id})
        for match in _CREDITS_RE.finditer(text):
            claims.append({"kind": "credits", "value": f"{float(match.group(1)):g}", "span": match.group(0), "block_id": block_id})
        for match in _EMAIL_RE.finditer(text):
            claims.append({"kind": "contact", "value": match.group(0).casefold(), "span": match.group(0), "block_id": block_id})
        for match in _PHONE_RE.finditer(text):
            claims.append({"kind": "contact", "value": re.sub(r"\D", "", match.group(0)), "span": match.group(0), "block_id": block_id})
        for match in _QUOTA_RE.finditer(text):
            number = match.group(1) or match.group(2)
            if number:
                claims.append({"kind": "quota", "value": str(int(number)), "span": match.group(0), "block_id": block_id})
        for match in _GPA_RE.finditer(text):
            claims.append({"kind": "condition", "value": match.group(1), "span": match.group(0), "block_id": block_id})
    return claims


def detect_fact_conflicts(
    docs: list[dict[str, Any]],
    claims_by_uid: dict[str, list[dict[str, str]]],
    *,
    llm_series: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    """Conflict candidates inside each series group (deterministic + LLM).

    Only *current* docs (``is_latest`` or no series) from the same group are
    compared: a superseded edition disagreeing with its successor is history,
    not a conflict.  Two current docs disagreeing on the same fact kind is
    exactly what must not be silently merged.
    """
    llm_series = llm_series or {}
    by_uid = {doc["doc_uid"]: doc for doc in docs}
    groups: dict[str, tuple[str, list[dict[str, Any]]]] = {}
    for doc in docs:
        if doc.get("status") != "active" or not doc.get("series_id"):
            continue
        groups.setdefault(doc["series_id"], ("deterministic", []))[1].append(doc)
    for key, uids in llm_series.items():
        members = [by_uid[uid] for uid in uids if uid in by_uid and by_uid[uid].get("status") == "active"]
        if len(members) >= 2:
            groups.setdefault(f"candidate:{key}", ("llm_candidate", []))[1].extend(members)

    out: list[dict[str, Any]] = []
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for group_key, (source, members) in sorted(groups.items()):
        current = [m for m in members if m.get("is_latest", True)]
        # Group members by the fact kind; a conflict needs ≥2 distinct values
        # among current docs with claims of that kind.
        by_kind: dict[str, list[tuple[dict[str, Any], dict[str, str]]]] = {}
        for doc in current:
            for claim in claims_by_uid.get(doc["doc_uid"], []):
                by_kind.setdefault(claim["kind"], []).append((doc, claim))
        for kind, pairs in sorted(by_kind.items()):
            values: dict[str, list[tuple[dict[str, Any], dict[str, str]]]] = {}
            for doc, claim in pairs:
                values.setdefault(claim["value"], []).append((doc, claim))
            if len(values) < 2:
                continue
            # ignore claims of the same value repeated across docs (agreement)
            sides = []
            for value in sorted(values):
                doc, claim = values[value][0]
                sides.append({
                    "doc_uid": doc["doc_uid"],
                    "block_id": claim["block_id"],
                    "value": value,
                    "span": claim["span"],
                    "published": doc.get("published", ""),
                    "effective_from": doc.get("effective_from", ""),
                    "authority_rank": doc.get("authority_rank", ""),
                    "title": doc.get("title", ""),
                })
            out.append({
                "fact_kind": kind,
                "series_id": group_key,
                "series_source": source,
                "values": sides,
                "status": "candidate",
                "created_at": created_at,
            })
    return out


def scan_build_conflicts(build_dir, llm_series: dict[str, list[str]] | None = None) -> list[dict[str, Any]]:
    """Run the whole scan over a build directory (documents + blocks)."""
    import collections
    from pathlib import Path

    build_dir = Path(build_dir)
    docs = list(read_jsonl(build_dir / "documents.jsonl"))
    # blocks are streamed per doc; extract claims doc-wise to keep memory flat
    pending: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for block in read_jsonl(build_dir / "blocks.jsonl"):
        pending[block["doc_uid"]].append(block)
    claims_by_uid = {uid: extract_fact_claims(blocks) for uid, blocks in pending.items()}
    return detect_fact_conflicts(docs, claims_by_uid, llm_series=llm_series)


def main(argv: list[str] | None = None) -> int:
    """CLI: ``python -m curate.conflicts --corpus <build>``.

    Scans fact-conflict candidates (deterministic series + gated LLM candidate
    groups from the sidecar), writes ``conflict_candidates.jsonl`` and the
    merged ``series_candidate_graph.json`` into the build directory.
    """
    import argparse

    from curate.series_graph import merge_series_graph
    from evidence.store import write_jsonl

    parser = argparse.ArgumentParser(prog="curate.conflicts", description=__doc__)
    parser.add_argument("--corpus", required=True)
    args = parser.parse_args(argv)

    build = Path(args.corpus)
    graph = merge_series_graph(build)
    llm_groups = {record["key"]: [m["doc_uid"] for m in record["members"]] for record in graph["candidates"]}
    conflicts = scan_build_conflicts(build, llm_series=llm_groups)
    write_jsonl(build / "conflict_candidates.jsonl", conflicts)
    (build / "series_candidate_graph.json").write_text(json.dumps(graph, ensure_ascii=False, indent=1), encoding="utf-8")

    from collections import Counter

    by_kind = Counter(record["fact_kind"] for record in conflicts)
    by_source = Counter(record["series_source"] for record in conflicts)
    print(f"[conflicts] {len(conflicts)} candidates → {build / 'conflict_candidates.jsonl'}")
    print(f"[conflicts] by kind: {dict(by_kind)}")
    print(f"[conflicts] by series source: {dict(by_source)}")
    print(f"[series_graph] {len(graph['deterministic'])} deterministic series, "
          f"{len(graph['candidates'])} llm candidates, {len(graph['cross_links'])} cross links "
          f"→ {build / 'series_candidate_graph.json'}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
