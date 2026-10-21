"""P2 LLM annotation pilot (CLI).

用法::

    PYTHONIOENCODING=utf-8 PYTHONPATH=. .venv/Scripts/python.exe -m annotate.run_pilot \\
        --corpus data/corpus/20261013-221420-a3fa1c [--limit 300] [--tasks doc,row] [--today 2026-10-13]

流程：选高价值文档 → 拼输入（缓存优先）→ 调 LLM → 逐字段锚点校验
（validate.anchor.verify_spans 对文档 clean 全文做规范化子串匹配）→ 组装
DocAnnotation → 写四个旁路文件到 build 目录（不改 active corpus）。

项目契约（见 annotate.schema 模块 docstring）：LLM 只增结构不增事实；
高风险字段（发布/生效日期、当前版本、受众、冲突裁决）必须带 evidence_spans
且原文可定位，否则标 uncertain；annotation 只写旁路文件；每条记录带
model/prompt_version/created_at/validation_status。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from annotate import cache as cache_mod
from annotate.prompts import DOC_ANNOTATE_PROMPT, ROW_CONTEXT_PROMPT
from annotate.runner import ApiRunner
from annotate.schema import (
    ALIAS_SUBFIELDS,
    ANNOTATION_SCHEMA_VERSION,
    EVIDENCE_FIELDS,
    PROMPT_VERSION,
    apply_rule_checks,
    empty_annotation,
    sanitize_doc_response,
    sanitize_row_response,
    status_at_least,
)
from annotate.select import select_pilot_docs
from evidence.store import read_jsonl, write_jsonl
from validate.anchor import locate_spans, norm_key, verify_spans, verify_summary

__all__ = ["main", "run_pilot", "sanitize_row_response", "SiteBlockedError"]


class SiteBlockedError(RuntimeError):
    """A cache miss would send a site whose content is not allowlisted to the API."""

DOC_BLOCK_LIMIT = 40
DOC_PAYLOAD_MAX_CHARS = 6000
ROW_LIMIT = 30
TASKS = ("doc", "row")

_KIND_TAGS = {"heading": "[标题]", "paragraph": "[段落]", "list_item": "[列表]", "table_row": "[表格行]"}


# --------------------------------------------------------------------- payload builders


def build_doc_payload(doc: dict, blocks: list[dict], *, max_blocks: int = DOC_BLOCK_LIMIT,
                      max_chars: int = DOC_PAYLOAD_MAX_CHARS) -> str:
    """Doc-level input text: title + breadcrumb + site + default metadata + leading blocks."""
    lines = [
        f"标题: {doc.get('title') or ''}",
        f"面包屑: {' > '.join(str(part) for part in (doc.get('breadcrumb') or []))}",
        f"站点: {doc.get('site') or ''}",
        "默认元数据: doc_type={}; audience={}; terms={}".format(
            doc.get("doc_type") or "", doc.get("audience") or "", ",".join(doc.get("terms") or []),
        ),
        "",
        "正文块:",
    ]
    for block in sorted(blocks, key=lambda row: row.get("ordinal", 0))[:max_blocks]:
        tag = _KIND_TAGS.get(block.get("kind"), "[文本]")
        path = " / ".join(str(part) for part in (block.get("heading_path") or []))
        lines.append(f"{tag} {f'({path}) ' if path else ''}{block.get('text') or ''}")
    return "\n".join(lines)[:max_chars]


def derive_table_header(table_rows: list[dict]) -> str:
    """Header = most common heading_path among table rows, else the first row text."""
    if not table_rows:
        return ""
    counts = Counter(tuple(row.get("heading_path") or []) for row in table_rows)
    top, _ = counts.most_common(1)[0]
    if top:
        return " / ".join(top)
    return str(table_rows[0].get("text") or "")


def build_row_payload(doc: dict, table_rows: list[dict], header: str) -> str:
    """Row-context input text: doc metadata + header + at most ROW_LIMIT numbered rows."""
    lines = [
        f"标题: {doc.get('title') or ''}",
        f"站点: {doc.get('site') or ''}",
        "默认元数据: doc_type={}; audience={}; terms={}".format(
            doc.get("doc_type") or "", doc.get("audience") or "", ",".join(doc.get("terms") or []),
        ),
        f"表头/所属章节: {header}",
        "",
        "表格行:",
    ]
    for index, row in enumerate(table_rows[:ROW_LIMIT], start=1):
        lines.append(f"{index}. {row.get('text') or ''}")
    return "\n".join(lines)


# sanitize_row_response moved to annotate.schema (P1): row ordinals are now
# range-checked there and the free-text context_line is treated as audit-only.


# --------------------------------------------------------------------- assembly


def assemble_annotation(
    doc: dict,
    blocks: list[dict],
    response: Any,
    *,
    build_id: str,
    model: str,
    created_at: str,
    stats: dict,
    provenance_extra: dict[str, Any] | None = None,
) -> tuple[dict, list[str], list[str]]:
    """Sanitize the LLM response, verify every field against the source, fill a
    DocAnnotation (P1 provenance contract v2).

    Returns ``(record, uncertain_fields, unsupported_fields)``.

    * High-risk fields (EVIDENCE_FIELDS): span verification against the clean
      text; supported spans are resolved to **structured block anchors**.
      A value without locatable evidence has its ``value`` cleared (kept in
      ``rejected_value``) and the field marked ``uncertain`` — 无原文依据的字段
      不再以原值留在记录里等下游误用。
    * Aliases: each alias must itself occur in the source; unlocatable aliases
      are rejected the same way, per subfield.
    * ``summary_1s``: must pass :func:`validate.anchor.verify_summary` (no
      novel digit runs + one verbatim anchor) or it is rejected.
    * ``boilerplate/answer_bearing`` spans: only locatable ones survive, now
      as anchored span objects with their own status.
    * ``validation.metadata_supported_by_source`` is ``None`` when no
      high-risk field was attempted (严格区分"未尝试"与"全有据").
    """
    record = empty_annotation(doc["doc_uid"], build_id=build_id, model=model, created_at=created_at,
                              **(provenance_extra or {}))
    clean = sanitize_doc_response(response)
    ordered = sorted(blocks, key=lambda row: row.get("ordinal", 0))
    full_text = "\n".join(str(block.get("text") or "") for block in ordered)
    full_key = norm_key(full_text)
    out = record["llm_annotations"]
    uncertain: list[str] = []
    unsupported: list[str] = []
    attempted: list[str] = []

    for name in EVIDENCE_FIELDS:
        field = clean[name]
        value = field["value"]
        if bool(value) or bool(field["evidence_spans"]):
            attempted.append(name)
            stats["field_support"][name]["attempted"] += 1
        verdict = verify_spans(full_text, field["evidence_spans"])
        stats["span_support"]["total"] += len(field["evidence_spans"])
        stats["span_support"]["supported"] += len(verdict["supported"])
        field["evidence_spans"] = locate_spans(ordered, verdict["supported"])
        field.setdefault("rejected_value", None)
        has_value = bool(value)
        has_evidence = bool(verdict["supported"])
        if has_value and has_evidence:
            field["status"] = "source_supported"
            stats["field_support"][name]["supported"] += 1
        else:
            if has_value:
                # Value/status linkage: an unsupported value must not survive
                # in the record where an index builder could pick it up.
                field["rejected_value"] = value
                field["value"] = [] if isinstance(value, list) else None
                unsupported.append(name)
            field["status"] = "uncertain"
            uncertain.append(name)
        out[name] = field

    # Aliases: the alias string itself is the span — it must occur in the source.
    for sub in ALIAS_SUBFIELDS:
        values = clean["aliases"].get(sub) or []
        if values:
            attempted.append(f"aliases.{sub}")
            stats["field_support"][f"aliases.{sub}"]["attempted"] += 1
        kept = [alias for alias in values if norm_key(alias) and norm_key(alias) in full_key]
        rejected = [alias for alias in values if alias not in kept]
        stats["span_support"]["total"] += len(values)
        stats["span_support"]["supported"] += len(kept)
        if kept:
            stats["field_support"][f"aliases.{sub}"]["supported"] += 1
        out["aliases"][sub] = {
            "value": kept,
            "rejected_value": rejected,
            "evidence_spans": locate_spans(ordered, kept),
            "status": "source_supported" if kept else "uncertain",
        }
        if values and not kept:
            unsupported.append(f"aliases.{sub}")

    # Summary: compression aid — no novel numbers, one verbatim anchor.
    summary = clean["summary_1s"]
    checks = verify_summary(full_text, summary)
    if summary:
        attempted.append("summary_1s")
        stats["field_support"]["summary_1s"]["attempted"] += 1
    if summary and checks["supported"]:
        out["summary_1s"] = {"value": summary, "rejected_value": "", "status": "source_supported", "checks": checks}
        stats["field_support"]["summary_1s"]["supported"] += 1
    else:
        out["summary_1s"] = {"value": "", "rejected_value": summary, "status": "uncertain", "checks": checks}
        if summary:
            uncertain.append("summary_1s")
            unsupported.append("summary_1s")

    for span_key in ("boilerplate_spans", "answer_bearing_spans"):
        spans = clean[span_key]
        verdict = verify_spans(full_text, spans)
        stats["span_support"]["total"] += len(spans)
        stats["span_support"]["supported"] += len(verdict["supported"])
        out[span_key] = {
            "value": locate_spans(ordered, verdict["supported"]),
            "rejected_value": verdict["unsupported"],
            "status": "source_supported" if verdict["supported"] else "uncertain",
        }

    violations = apply_rule_checks(record)
    # The doc-level bool covers the four high-risk *metadata* fields only:
    # an unsupported alias or summary is blocked by its own field-level gate
    # and must not demote the doc's verified metadata wholesale (the document
    # level must stay a summary, not a bottleneck).  None = nothing attempted.
    attempted_metadata = [name for name in attempted if name in EVIDENCE_FIELDS]
    unsupported_metadata = [name for name in unsupported if name in EVIDENCE_FIELDS]
    record["validation"] = {
        "metadata_supported_by_source": (not unsupported_metadata) if attempted_metadata else None,
        "attempted_fields": attempted,
        "unsupported_fields": unsupported,
        "rule_violations": violations,
        "status": "uncertain" if (unsupported or uncertain) else "rule_checked",
    }
    return record, uncertain, unsupported


def rebuild_row_contexts(doc: dict, table_rows: list[dict], llm_rows: list[dict]) -> list[dict]:
    """Rebuild table-row context lines from source; the LLM only flags rows.

    The indexable line (``derived_text``) is composed by rule from the block's
    own heading path and its self-contained row text — never from the model's
    free-text ``context_line`` (kept as ``llm_context_line`` for audit only).
    Rows whose ordinal fell outside the payload's range were already dropped
    by :func:`annotate.schema.sanitize_row_response`.
    """
    contexts: list[dict] = []
    for llm_row in llm_rows:
        ordinal = llm_row["row_ordinal"]
        if not 1 <= ordinal <= len(table_rows):
            continue
        row = table_rows[ordinal - 1]
        table = row.get("table") or {}
        section = " / ".join(str(part) for part in (row.get("heading_path") or []))
        derived = f"{section} | {row.get('text') or ''}".strip(" |")
        contexts.append({
            "row_ordinal": ordinal,
            "block_id": row.get("block_id"),
            "table_id": table.get("table_id"),
            "row_index": table.get("row_index"),
            "header_cells": table.get("header") or [],
            "section_path": row.get("heading_path") or [],
            "derived_text": derived,
            "answer_bearing": bool(llm_row.get("answer_bearing")),
            "llm_context_line": llm_row.get("context_line", ""),
            "status": "rule_checked",
        })
    return contexts


def build_retrieval_text(doc: dict, annotation: dict | None, row_contexts: list[dict]) -> str:
    """Compose the retrieval-aid text from **gated** annotations + rule-rebuilt rows.

    Only fields at or above ``source_supported`` can appear: unsupported values
    were cleared at assembly, and summary/aliases carry their own status.  When
    an LLM field did not clear the gate the deterministic rule value is used as
    fallback (doc metadata is source-backed by construction).  Row lines use
    the rule-rebuilt ``derived_text`` of ``rule_checked`` row contexts only.
    """
    llm = (annotation or {}).get("llm_annotations") or {}

    def anchored(field: str, fallback: Any) -> Any:
        entry = llm.get(field) or {}
        value = entry.get("value") if status_at_least(entry.get("status")) else None
        if value is None or value == "" or value == []:
            return fallback
        return value

    terms = ",".join(anchored("terms", doc.get("terms") or []))
    audience = anchored("audience", [doc["audience"]] if doc.get("audience") else [])
    aliases = llm.get("aliases") or {}

    def alias_values(sub: str) -> list[str]:
        entry = aliases.get(sub) or {}
        return list(entry.get("value") or []) if status_at_least(entry.get("status")) else []

    zh = "、".join(alias_values("zh"))
    en = ",".join(alias_values("en"))
    codes = ",".join(alias_values("course_codes"))
    summary_entry = llm.get("summary_1s") or {}
    summary = summary_entry.get("value") if status_at_least(summary_entry.get("status")) else ""

    parts = [f"文档：{doc.get('title') or ''}"]
    if terms:
        parts.append(f"学期：{terms}")
    if audience:
        parts.append(f"对象：{','.join(audience)}")
    alias_bits = [bit for bit in (f"中文:{zh}" if zh else "", f"英文:{en}" if en else "", f"课程码:{codes}" if codes else "") if bit]
    if alias_bits:
        parts.append(f"别名：{'; '.join(alias_bits)}")
    if summary:
        parts.append(f"摘要：{summary}")
    for row in row_contexts:
        if row.get("status") != "rule_checked":
            continue
        line = row["derived_text"]
        if row.get("answer_bearing"):
            line = f"{line} [答案相关]"
        parts.append(f"表格行{row['row_ordinal']}：{line}")
    return "\n".join(parts)


def detect_conflicts(annotations: list[dict], docs_by_uid: dict[str, dict]) -> list[dict]:
    """Pairs of *active* docs sharing a series_key_candidate but disagreeing on terms."""
    by_series: dict[str, list[tuple[str, frozenset]]] = {}
    for annotation in annotations:
        series = annotation["llm_annotations"]["series_key_candidate"]
        key = series.get("value")
        if not key:
            continue
        doc = docs_by_uid.get(annotation["doc_uid"]) or {}
        if doc.get("status") != "active":
            continue
        terms = set(series_terms(annotation, doc))
        by_series.setdefault(key, []).append((annotation["doc_uid"], frozenset(terms)))
    conflicts: list[dict] = []
    for key, members in sorted(by_series.items()):
        members.sort()
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                if members[i][1] != members[j][1]:
                    conflicts.append({
                        "doc_a": members[i][0],
                        "doc_b": members[j][0],
                        "series_key": key,
                        "conflict": "term_mismatch",
                    })
    return conflicts


def series_terms(annotation: dict, doc: dict) -> list[str]:
    value = annotation["llm_annotations"]["terms"].get("value")
    if value:
        return list(value)
    return list(doc.get("terms") or [])


# --------------------------------------------------------------------- pilot run


def _cached_response(runner, cache_dir: Path | None, task: str, payload: str, stats: dict,
                     site: str = "", allowed_sites: frozenset[str] = frozenset()) -> Any:
    key = cache_mod.cache_key(payload, task=task, model=runner.model, prompt_version=PROMPT_VERSION)
    record = cache_mod.load(key, cache_dir)
    if record is not None:
        stats["cache_hits"] += 1
        return record.get("response")
    # Cache-only replay never leaves the machine; a *send* does.  The site
    # allowlist (ANNOTATE_API_ALLOWED_SITES) is enforced right here so the
    # SharePoint red line does not depend on documentation discipline.
    if site and allowed_sites is not None and site not in allowed_sites:
        raise SiteBlockedError(site)
    prompt = DOC_ANNOTATE_PROMPT if task == "doc" else ROW_CONTEXT_PROMPT
    messages = [{"role": "system", "content": prompt}, {"role": "user", "content": payload}]
    if task == "doc":
        response = runner.complete_json(messages)
    else:
        response = runner.complete_json_array(messages)
    stats["api_calls"] += 1
    cache_mod.save(key, response, model=runner.model, prompt_version=PROMPT_VERSION, cache_dir=cache_dir)
    return response


def _allowed_sites_from_env() -> frozenset[str]:
    """ANNOTATE_API_ALLOWED_SITES, comma separated.  Unset → empty: sends are
    denied for every site (cache-only replay still works)."""
    import os

    raw = os.environ.get("ANNOTATE_API_ALLOWED_SITES", "")
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


SIDECAR_FILES = (
    "llm_annotations.jsonl", "llm_retrieval_text.jsonl", "llm_row_contexts.jsonl",
    "llm_conflicts.jsonl", "llm_uncertain.jsonl", "llm_pilot_report.json",
)


def _load_prior(path: Path, keep) -> list[dict]:
    """Rows of an existing sidecar whose doc_uid passes ``keep`` (P6 carry-over)."""
    if not path.is_file():
        return []
    return [row for row in read_jsonl(path) if keep(row.get("doc_uid"))]


def _archive_sidecars(corpus: Path) -> Path | None:
    """Copy existing sidecars into ``llm_history/<report-stamp>/`` before a rewrite.

    The stamp comes from the previous run's report (model + created_at) so a
    history dir identifies *which* generation was archived even after several
    runs; returns None when there was nothing to archive.
    """
    import shutil

    present = [name for name in SIDECAR_FILES if (corpus / name).is_file()]
    if not present:
        return None
    stamp = "unknown"
    report = corpus / "llm_pilot_report.json"
    if report.is_file():
        try:
            prior = json.loads(report.read_text(encoding="utf-8"))
            stamp = f"{prior.get('model', 'model')}-{prior.get('created_at', '').replace(':', '')}"
        except (OSError, ValueError):
            pass
    target = corpus / "llm_history" / stamp
    target.mkdir(parents=True, exist_ok=True)
    for name in present:
        shutil.copy2(corpus / name, target / name)
    return target


def _prompt_hashes() -> dict[str, str]:
    import hashlib

    return {task: hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
            for task, prompt in (("doc", DOC_ANNOTATE_PROMPT), ("row", ROW_CONTEXT_PROMPT))}


def run_pilot(
    corpus: Path,
    *,
    limit: int = 300,
    tasks: tuple[str, ...] = ("doc", "row"),
    today: str | None = None,
    runner,
    cache_dir: Path | None = None,
    allowed_sites: frozenset[str] | None = None,
    selected_uids: list[str] | None = None,
    stratified: bool = False,
) -> dict:
    """Run the annotation pilot over ``corpus`` and write the sidecar files.

    Returns the report dict (also written to ``llm_pilot_report.json``).
    ``runner`` is any object with ``model`` / ``complete_json`` /
    ``complete_json_array`` — :class:`annotate.runner.ApiRunner` for the real
    pilot, :class:`annotate.runner.FakeRunner` in tests.

    ``allowed_sites``: sites whose content may be *sent* to the API on a cache
    miss (``None`` → read ``ANNOTATE_API_ALLOWED_SITES``; unset → deny all
    sends, replay from cache only).  Blocked docs are skipped and reported.

    Selection: ``selected_uids`` wins when given (already-validated list);
    ``stratified=True`` uses P2 proportional sampling over
    site × doc_type × has-table × in-series strata (``limit`` = sample size,
    composition written to ``llm_sample_uids.txt``); otherwise the classic
    high-value ranking truncated to ``limit``.
    """
    started = time.perf_counter()
    corpus = Path(corpus)
    unknown = [task for task in tasks if task not in TASKS]
    if unknown:
        raise ValueError(f"unknown tasks {unknown}; expected subset of {list(TASKS)}")
    if allowed_sites is None:
        allowed_sites = _allowed_sites_from_env()

    docs_by_uid = {doc["doc_uid"]: doc for doc in read_jsonl(corpus / "documents.jsonl")}
    blocks_by_uid: dict[str, list[dict]] = {}
    for block in read_jsonl(corpus / "blocks.jsonl"):
        blocks_by_uid.setdefault(block["doc_uid"], []).append(block)

    selection_mode = "uids" if selected_uids else ("stratified" if stratified else "ranked")
    strata_counts: dict[str, int] = {}
    if selected_uids is not None:
        selected = [uid for uid in selected_uids if uid in docs_by_uid and docs_by_uid[uid].get("status") == "active"]
    elif stratified:
        from annotate.select import stratified_sample_docs

        selected, strata_counts = stratified_sample_docs(corpus, limit)
        (corpus / "llm_sample_uids.txt").write_text("\n".join(selected) + "\n", encoding="utf-8")
    else:
        selected = select_pilot_docs(corpus, limit=limit)
    build_id = corpus.name
    created_at = today if today else datetime.now(timezone.utc).isoformat(timespec="seconds")
    runner_prov = runner.provenance() if hasattr(runner, "provenance") else {}
    provenance_extra = {
        "base_url": str(runner_prov.get("base_url", "")),
        "sampling": runner_prov.get("sampling", {}),
        "prompt_hash": _prompt_hashes(),
    }

    field_keys = list(EVIDENCE_FIELDS) + [f"aliases.{sub}" for sub in ALIAS_SUBFIELDS] + ["summary_1s"]
    stats: dict = {
        "api_calls": 0,
        "cache_hits": 0,
        "docs_errored": 0,
        "docs_blocked": 0,
        "field_support": {name: {"attempted": 0, "supported": 0} for name in field_keys},
        "span_support": {"total": 0, "supported": 0},
    }

    annotations: list[dict] = []
    retrieval_rows: list[dict] = []
    uncertain_rows: list[dict] = []
    row_context_records: list[dict] = []
    blocked_docs: list[dict] = []
    row_docs = 0

    for uid in selected:
        doc = docs_by_uid.get(uid)
        if doc is None:
            continue
        blocks = sorted(blocks_by_uid.get(uid, []), key=lambda row: row.get("ordinal", 0))
        site = str(doc.get("site") or "")

        annotation: dict | None = None
        if "doc" in tasks:
            payload = build_doc_payload(doc, blocks)
            try:
                response = _cached_response(runner, cache_dir, "doc", payload, stats, site=site, allowed_sites=allowed_sites)
            except SiteBlockedError:
                blocked_docs.append({"doc_uid": uid, "site": site, "reason": "blocked_by_policy"})
                continue
            except Exception as exc:  # noqa: BLE001 — one bad doc must not kill the pilot
                print(f"[warn] doc task failed for {uid}: {exc}", file=sys.stderr)
                stats["docs_errored"] += 1
                response = {}
            annotation, uncertain, unsupported = assemble_annotation(
                doc, blocks, response, build_id=build_id, model=runner.model,
                created_at=created_at, stats=stats, provenance_extra=provenance_extra,
            )
            annotations.append(annotation)
            if uncertain or unsupported:
                uncertain_rows.append({
                    "doc_uid": uid,
                    "uncertain_fields": uncertain,
                    "unsupported_fields": unsupported,
                    "rule_violations": annotation["validation"]["rule_violations"],
                })

        row_contexts: list[dict] = []
        if "row" in tasks and doc.get("n_table_rows", 0) > 0:
            table_rows = [block for block in blocks if block.get("kind") == "table_row"]
            header = derive_table_header(table_rows)
            payload = build_row_payload(doc, table_rows, header)
            try:
                response = _cached_response(runner, cache_dir, "row", payload, stats, site=site, allowed_sites=allowed_sites)
            except SiteBlockedError:
                blocked_docs.append({"doc_uid": uid, "site": site, "reason": "blocked_by_policy"})
                continue
            except Exception as exc:  # noqa: BLE001
                print(f"[warn] row task failed for {uid}: {exc}", file=sys.stderr)
                stats["docs_errored"] += 1
                response = []
            row_contexts = rebuild_row_contexts(doc, table_rows, sanitize_row_response(response, n_rows=len(table_rows)))
            if row_contexts:
                row_docs += 1
                row_context_records.append({"doc_uid": uid, "rows": row_contexts})

        retrieval_rows.append({
            "doc_uid": uid,
            "retrieval_text": build_retrieval_text(doc, annotation, row_contexts),
        })

    conflicts = detect_conflicts(annotations, docs_by_uid)
    stats["docs_blocked"] = len(blocked_docs)

    # P6 incremental annotations: keep the previous generation's records for
    # docs outside this run's selection (source unchanged, annotation still
    # valid) so re-running a subset never drops the rest of the coverage.
    annotated_set = {record["doc_uid"] for record in annotations}
    prior_annotations = _load_prior(corpus / "llm_annotations.jsonl", keep=lambda uid: uid not in annotated_set and uid in docs_by_uid)
    prior_retrieval = _load_prior(corpus / "llm_retrieval_text.jsonl", keep=lambda uid: uid not in annotated_set and uid in docs_by_uid)
    prior_rows = _load_prior(corpus / "llm_row_contexts.jsonl", keep=lambda uid: uid not in annotated_set and uid in docs_by_uid)
    stats["carried_over_annotations"] = len(prior_annotations)
    annotated_this_run = len(annotations)
    annotations = prior_annotations + annotations
    retrieval_rows = prior_retrieval + retrieval_rows
    row_context_records = prior_rows + row_context_records

    # P6: overwriting sidecars invalidates the previous annotation generation.
    # Archive it first — model/prompt/rule changes must never silently mix old
    # records into a new build, but history stays auditable.
    _archive_sidecars(corpus)
    write_jsonl(corpus / "llm_annotations.jsonl", annotations)
    write_jsonl(corpus / "llm_retrieval_text.jsonl", retrieval_rows)
    write_jsonl(corpus / "llm_row_contexts.jsonl", row_context_records)
    write_jsonl(corpus / "llm_conflicts.jsonl", conflicts)
    write_jsonl(corpus / "llm_uncertain.jsonl", uncertain_rows)

    duration = time.perf_counter() - started
    field_support = {
        name: {
            "supported": counts["supported"],
            "attempted": counts["attempted"],
            "rate": round(counts["supported"] / counts["attempted"], 4) if counts["attempted"] else None,
        }
        for name, counts in stats["field_support"].items()
    }
    report = {
        "build_id": build_id,
        "corpus": str(corpus),
        "model": runner.model,
        "prompt_version": PROMPT_VERSION,
        "annotation_schema_version": ANNOTATION_SCHEMA_VERSION,
        "allowed_sites": sorted(allowed_sites),
        "created_at": created_at,
        "tasks": list(tasks),
        "selection": selection_mode,
        "limit": limit,
        "strata": len(strata_counts) or None,
        "selected_docs": len(selected),
        "annotated_docs": annotated_this_run,
        "row_docs": row_docs,
        "row_context_rows": sum(len(rec["rows"]) for rec in row_context_records),
        "api_calls": stats["api_calls"],
        "cache_hits": stats["cache_hits"],
        "docs_errored": stats["docs_errored"],
        "docs_blocked": stats["docs_blocked"],
        "carried_over_annotations": stats.get("carried_over_annotations", 0),
        "blocked_sites": sorted({entry["site"] for entry in blocked_docs}),
        "field_support": field_support,
        "span_support": stats["span_support"],
        "uncertain_docs": len(uncertain_rows),
        "conflicts": len(conflicts),
        "duration_seconds": round(duration, 2),
    }
    report_path = corpus / "llm_pilot_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    _print_summary(report, report_path)
    return report


def _fmt_rate(supported: int, attempted: int) -> str:
    return f"{supported}/{attempted}" + (f" ({supported / attempted:.1%})" if attempted else "")


def _print_summary(report: dict, report_path: Path) -> None:
    print("== LLM pilot 统计 ==")
    print(f"build: {report['build_id']}  corpus: {report['corpus']}")
    print(f"model: {report['model']}  prompt_version: {report['prompt_version']}")
    print(
        f"selected docs: {report['selected_docs']}  annotated docs: {report['annotated_docs']}"
        f"  row docs: {report['row_docs']}"
    )
    print(
        f"api calls: {report['api_calls']}  cache hits: {report['cache_hits']}"
        f"  docs errored: {report['docs_errored']}"
    )
    print("字段级 evidence 支持率 (supported/attempted):")
    for name, counts in report["field_support"].items():
        print(f"  {name:<22} {_fmt_rate(counts['supported'], counts['attempted'])}")
    spans = report["span_support"]
    print(f"span 支持率: {_fmt_rate(spans['supported'], spans['total'])}")
    print(f"uncertain docs: {report['uncertain_docs']}  conflicts: {report['conflicts']}")
    print(f"耗时: {report['duration_seconds']}s")
    print(f"report: {report_path}")


# --------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="annotate.run_pilot",
        description="P2 LLM 标注 pilot：选高价值文档，LLM 标注 + 锚点校验，写旁路文件（不改 active corpus）。",
    )
    parser.add_argument("--corpus", required=True, type=Path, help="build 目录（含 documents.jsonl / blocks.jsonl）")
    parser.add_argument("--limit", type=int, default=300, help="最多标注的文档数（默认 300；--stratified 时为抽样规模）")
    parser.add_argument("--stratified", action="store_true", help="P2 分层抽样（site×doc_type×有表×版本链成员，比例配额）替代高价值排名")
    parser.add_argument("--uids-file", type=Path, default=None, help="显式 doc_uid 列表（每行一个），优先于其它选择方式")
    parser.add_argument("--tasks", default="doc,row", help="逗号分隔任务子集：doc,row（默认 doc,row）")
    parser.add_argument("--today", default=None, help="覆盖 created_at（如 2026-10-13），用于确定性重跑")
    parser.add_argument("--cache-dir", type=Path, default=None, help="LLM 缓存目录（默认 data/llm-cache/annotations）")
    parser.add_argument("--env", type=Path, default=None, help=".env 路径（默认 ./.env）")
    args = parser.parse_args(argv)

    tasks = tuple(task.strip() for task in args.tasks.split(",") if task.strip())
    selected_uids = None
    if args.uids_file is not None:
        selected_uids = [line.strip() for line in args.uids_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    runner = ApiRunner(env_path=args.env)
    try:
        run_pilot(
            args.corpus,
            limit=args.limit,
            tasks=tasks,
            today=args.today,
            runner=runner,
            cache_dir=args.cache_dir,
            selected_uids=selected_uids,
            stratified=args.stratified,
        )
    finally:
        runner.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
