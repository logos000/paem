"""DocAnnotation JSON contract and field-level validation (P1 provenance contract v2).

项目契约（所有 LLM 标注必须遵守，违反任何一条即不可晋级）：

1. **LLM 只增结构不增事实**：标注只能给出原文可定位/可推出的结构化字段，
   不得引入原文没有的事实（日期、数字、学分、人名、课程代码一律禁止编造）。
2. **进入索引的每个增强字段都必须有原文锚点**：``doc_type`` / ``audience`` /
   ``terms`` / ``series_key_candidate`` / ``aliases.*`` / ``summary_1s`` /
   ``answer_bearing_spans`` / 表格行 ``context_line`` 的值必须能在文档 clean
   文本中逐字（规范化后）定位；定位失败一律 ``status="uncertain"`` 且
   **value 被清空**（原文值移入 ``rejected_value`` 留档），不得以模型置信度
   替代原文依据。
3. **annotation 不改 active corpus**：只写旁路文件
   （llm_annotations / llm_retrieval_text / llm_row_contexts / llm_conflicts /
   llm_uncertain.jsonl），是否晋级由字段级门禁决定。
4. **每条记录必须带完整 provenance**（``model`` / ``base_url`` / ``sampling`` /
   ``prompt_version`` / ``annotation_schema_version`` / ``prompt_hash`` /
   ``created_at``）。

字段状态机（P1）::

    candidate → source_supported → rule_checked → human_verified → promoted_to_index

* ``candidate``：sanitize 后未经锚点校验的原始候选；
* ``source_supported``：value 非空且 ≥1 条 evidence span 通过锚点校验；
* ``rule_checked``：source_supported 且通过确定性规则（枚举形状、term 形状、
  非泛化 series key）；
* ``human_verified`` / ``promoted_to_index``：由 P2 人工核验与导出阶段标记；
* ``uncertain``：**不在梯子上**——无 value、或 value 无原文依据（value 已清空）。

DocAnnotation dict 契约（v2）::

    {"doc_uid": "...",
     "llm_annotations": {
       "doc_type": {"value": "...", "confidence": 0.0,
                    "evidence_spans": [{"text": "...", "anchors": [...], "match_count": 1}],
                    "rejected_value": null, "status": "source_supported"},
       "audience": {"value": ["..."], ...},
       "terms": {"value": ["2026-fall"], ...},
       "series_key_candidate": {"value": "...", ...},
       "aliases": {"zh": {"value": [...], "rejected_value": [...],
                          "evidence_spans": [...], "status": "..."},
                   "en": {...}, "course_codes": {...}},
       "summary_1s": {"value": "...", "rejected_value": "", "status": "...",
                      "checks": {"supported": true, "novel_digits": [], "verbatim_anchor": true}},
       "boilerplate_spans": {"value": [...], "rejected_value": [...], "status": "..."},
       "answer_bearing_spans": {"value": [...], "rejected_value": [...], "status": "..."}},
     "validation": {"metadata_supported_by_source": null,
                    "attempted_fields": [], "unsupported_fields": [],
                    "rule_violations": [], "status": "candidate"},
     "provenance": {"build_id": "...", "model": "deepseek-flash", "base_url": "...",
                    "sampling": {...}, "prompt_version": "v1",
                    "annotation_schema_version": "v2", "prompt_hash": {...},
                    "created_at": "..."}}

``metadata_supported_by_source``：``True``=尝试过且全部有据，``False``=存在无据
字段，``None``=四个高风险字段全部未尝试（与"全有据"严格区分）。

表格行 context（llm_row_contexts.jsonl，每行）::

    {"doc_uid": "...", "rows": [
      {"row_ordinal": 3, "block_id": "...", "table_id": "p1t1", "row_index": 3,
       "header_cells": [...], "section_path": [...], "derived_text": "...",
       "answer_bearing": true, "llm_context_line": "...", "status": "rule_checked"}]}

``derived_text`` 由规则从表头/章节/原始行重建（LLM 的自由文本 context_line
仅留档对比，永不直接入索引）。
"""

from __future__ import annotations

import re
from typing import Any

from curate.versions import is_generic_series_key

__all__ = [
    "AUDIENCES",
    "ANNOTATION_SCHEMA_VERSION",
    "DOC_TYPES",
    "EVIDENCE_FIELDS",
    "FIELD_STATUSES",
    "PROMPT_VERSION",
    "STATUS_LADDER",
    "STATUS_RANK",
    "ALIAS_SUBFIELDS",
    "apply_rule_checks",
    "empty_annotation",
    "sanitize_doc_response",
    "sanitize_row_response",
    "status_at_least",
    "validate_annotation",
]

PROMPT_VERSION = "v1"
ANNOTATION_SCHEMA_VERSION = "v2"

DOC_TYPES = (
    "notice", "regulation", "calendar", "guide", "schedule", "course_catalog",
    "exam_schedule", "program_plan", "form", "news", "page", "other",
)
AUDIENCES = (
    "zjui_undergraduate", "undergraduate", "graduate", "faculty", "postdoc",
    "international", "exchange", "public", "unknown",
)

#: The promotion ladder.  ``uncertain`` is deliberately off-ladder: a field
#: without source support never climbs, no matter what later stages do.
STATUS_LADDER = ("candidate", "source_supported", "rule_checked", "human_verified", "promoted_to_index")
STATUS_RANK = {name: rank for rank, name in enumerate(STATUS_LADDER)}
FIELD_STATUSES = STATUS_LADDER + ("uncertain",)

#: Fields that carry a judgment and therefore MUST be evidence-anchored.
EVIDENCE_FIELDS = ("doc_type", "audience", "terms", "series_key_candidate")

#: Alias subfields verified by occurrence: an alias that never appears in the
#: source (normalized) is treated as a fabricated term and rejected.
ALIAS_SUBFIELDS = ("zh", "en", "course_codes")

_SNAKE_RE = re.compile(r"[^a-z0-9]+")
# A usable term mentions a year or a semester marker; anything else riding
# along in ``terms`` is treated as a rule violation.
_TERM_MARKER_RE = re.compile(r"\d{4}|学期|学年|春|秋|夏|冬|spring|fall|summer|winter", re.I)


def status_at_least(status: Any, level: str = "source_supported") -> bool:
    """True when ``status`` is at or above ``level`` on the promotion ladder."""
    return STATUS_RANK.get(str(status), -1) >= STATUS_RANK[level]


def empty_annotation(doc_uid: str, *, build_id: str, model: str, created_at: str,
                     base_url: str = "", sampling: dict[str, Any] | None = None,
                     prompt_hash: dict[str, str] | None = None) -> dict[str, Any]:
    """Return a contract-shaped record with every field empty/uncertain."""
    def anchored(value: Any) -> dict[str, Any]:
        return {"value": value, "evidence_spans": [], "rejected_value": None, "status": "uncertain"}

    return {
        "doc_uid": doc_uid,
        "llm_annotations": {
            "doc_type": {"value": None, "confidence": 0.0, "evidence_spans": [], "rejected_value": None, "status": "uncertain"},
            "audience": {"value": [], "confidence": 0.0, "evidence_spans": [], "rejected_value": None, "status": "uncertain"},
            "terms": {"value": [], "evidence_spans": [], "rejected_value": None, "status": "uncertain"},
            "series_key_candidate": {"value": None, "evidence_spans": [], "rejected_value": None, "status": "uncertain"},
            "aliases": {sub: anchored([]) for sub in ALIAS_SUBFIELDS},
            "summary_1s": {"value": "", "rejected_value": "", "status": "uncertain", "checks": {}},
            "boilerplate_spans": {"value": [], "rejected_value": [], "status": "uncertain"},
            "answer_bearing_spans": {"value": [], "rejected_value": [], "status": "uncertain"},
        },
        "validation": {"metadata_supported_by_source": None, "attempted_fields": [],
                       "unsupported_fields": [], "rule_violations": [], "status": "uncertain"},
        "provenance": {
            "build_id": build_id,
            "model": model,
            "base_url": base_url,
            "sampling": dict(sampling or {}),
            "prompt_version": PROMPT_VERSION,
            "annotation_schema_version": ANNOTATION_SCHEMA_VERSION,
            "prompt_hash": dict(prompt_hash or {}),
            "created_at": created_at,
        },
    }


# --------------------------------------------------------------------- sanitizing


def _span_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue  # spans must be verbatim source strings
        text = item.strip()
        if text and text not in out:
            out.append(text)
    return out


def _confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))


def _enum_list(value: Any, allowed: tuple[str, ...]) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item if item is not None else "").strip()
        if text in allowed and text not in out:
            out.append(text)
    return out


def _snake_case(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    slug = _SNAKE_RE.sub("_", value.strip().casefold()).strip("_")
    return slug or None


def sanitize_doc_response(raw: Any) -> dict[str, Any]:
    """Coerce a raw LLM JSON response into contract shapes (never raises).

    Unknown enum values become ``None``/dropped; confidences are clamped to
    [0, 1]; ``series_key_candidate`` is forced to english snake_case.  All
    fields default to "no claim" so a malformed response degrades into an
    all-uncertain annotation instead of crashing the pilot.  Output here is the
    *raw candidate* layer — anchor verification and value/status linkage happen
    in :func:`annotate.run_pilot.assemble_annotation`.
    """
    data = raw if isinstance(raw, dict) else {}

    def evidence_field(name: str, with_confidence: bool) -> dict[str, Any]:
        source = data.get(name)
        if not isinstance(source, dict):
            # tolerate a bare value ("terms": "2026-fall") instead of the object form
            source = {"value": source} if isinstance(source, (str, list)) else {}
        if with_confidence:
            return {
                "value": source.get("value"),
                "confidence": _confidence(source.get("confidence")),
                "evidence_spans": _span_list(source.get("evidence_spans")),
                "status": "candidate",
            }
        return {
            "value": source.get("value"),
            "evidence_spans": _span_list(source.get("evidence_spans")),
            "status": "candidate",
        }

    doc_type = evidence_field("doc_type", True)
    doc_type["value"] = doc_type["value"] if (
        isinstance(doc_type["value"], str) and doc_type["value"].strip() in DOC_TYPES
    ) else None

    audience = evidence_field("audience", True)
    audience["value"] = _enum_list(audience["value"], AUDIENCES)

    terms = evidence_field("terms", False)
    raw_terms = terms["value"]
    if isinstance(raw_terms, str):
        raw_terms = [raw_terms]
    if not isinstance(raw_terms, list):
        raw_terms = []
    terms["value"] = [str(item).strip() for item in raw_terms if str(item if item is not None else "").strip()]

    series = evidence_field("series_key_candidate", False)
    series["value"] = _snake_case(series["value"])

    aliases_raw = data.get("aliases")
    aliases_raw = aliases_raw if isinstance(aliases_raw, dict) else {}
    aliases = {key: _span_list(aliases_raw.get(key)) for key in ALIAS_SUBFIELDS}

    return {
        "doc_type": doc_type,
        "audience": audience,
        "terms": terms,
        "series_key_candidate": series,
        "aliases": aliases,
        "summary_1s": str(data.get("summary_1s") or "").strip(),
        "boilerplate_spans": _span_list(data.get("boilerplate_spans")),
        "answer_bearing_spans": _span_list(data.get("answer_bearing_spans")),
    }


def sanitize_row_response(raw: Any, n_rows: int | None = None) -> list[dict]:
    """Coerce a row-context LLM response into [{"row_ordinal", "context_line", "answer_bearing"}].

    ``row_ordinal`` must be a positive integer and, when ``n_rows`` is given,
    must fall inside ``[1, n_rows]`` — ordinals past the end of the payload's
    row list are dropped instead of silently pointing at nothing.  The
    ``context_line`` here is the model's *free-text* candidate; the indexable
        line is rebuilt from source by :func:`annotate.run_pilot.rebuild_row_contexts`.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            ordinal = int(item.get("row_ordinal"))
        except (TypeError, ValueError):
            continue
        if ordinal < 1 or (n_rows is not None and ordinal > n_rows):
            continue
        context = str(item.get("context_line") or "").strip()
        if not context:
            continue
        out.append({"row_ordinal": ordinal, "context_line": context, "answer_bearing": bool(item.get("answer_bearing"))})
    return out


# --------------------------------------------------------------------- rule layer


def apply_rule_checks(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Climb ``source_supported`` fields over the deterministic rule layer.

    Mutates the record in place and returns the violation list (also stored in
    ``validation.rule_violations``).  A rule violation does not erase the
    field's source anchor — the field stays ``source_supported`` (it remains
    auditable) but does not reach ``rule_checked``; downstreams that want the
    stricter guarantee gate on :func:`status_at_least` with ``"rule_checked"``.
    """
    annotations = record.get("llm_annotations") or {}
    violations: list[dict[str, Any]] = []

    for name in ("doc_type", "audience"):
        field = annotations.get(name) or {}
        if field.get("status") == "source_supported":
            field["status"] = "rule_checked"  # enum shape is the rule, enforced by sanitize

    terms = annotations.get("terms") or {}
    if terms.get("status") == "source_supported":
        bad = [term for term in terms.get("value") or [] if not _TERM_MARKER_RE.search(str(term))]
        if bad:
            violations.append({"field": "terms", "reason": "term_shape", "items": bad})
        else:
            terms["status"] = "rule_checked"

    series = annotations.get("series_key_candidate") or {}
    if series.get("status") == "source_supported":
        value = str(series.get("value") or "")
        # curate's generic-key guard matches space-separated titles; the
        # sanitized candidate is snake_case, so de-snake before matching.
        if is_generic_series_key(value.replace("_", " ").strip()):
            violations.append({"field": "series_key_candidate", "reason": "generic_series_key", "items": [value]})
        else:
            series["status"] = "rule_checked"

    for holder in (annotations.get("aliases") or {}).values():
        if isinstance(holder, dict) and holder.get("status") == "source_supported":
            holder["status"] = "rule_checked"  # occurrence-in-source is the rule
    summary = annotations.get("summary_1s") or {}
    if isinstance(summary, dict) and summary.get("status") == "source_supported":
        summary["status"] = "rule_checked"  # verify_summary checks are the rule
    return violations


# --------------------------------------------------------------------- validation


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _is_anchored_field(value: Any, *, with_confidence: bool = False) -> bool:
    if not isinstance(value, dict):
        return False
    spans = value.get("evidence_spans")
    if not isinstance(spans, list):
        return False
    for span in spans:
        if not isinstance(span, dict) or not isinstance(span.get("text"), str):
            return False
    if value.get("status") not in FIELD_STATUSES:
        return False
    if "rejected_value" not in value:
        return False
    if with_confidence:
        confidence = value.get("confidence")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0.0 <= confidence <= 1.0:
            return False
    return True


def validate_annotation(record: Any) -> list[str]:
    """Validate the dict contract by hand (jsonschema-style, no new deps).

    Returns a list of human-readable errors; an empty list means the record
    conforms to the contract.  Structural validation only — whether evidence
    spans actually appear in the source is checked by
    :func:`validate.anchor.verify_spans` at pilot time.
    """
    if not isinstance(record, dict):
        return ["record must be a JSON object"]
    errors: list[str] = []

    doc_uid = record.get("doc_uid")
    if not isinstance(doc_uid, str) or not doc_uid:
        errors.append("doc_uid must be a non-empty string")

    annotations = record.get("llm_annotations")
    if not isinstance(annotations, dict):
        return errors + ["llm_annotations must be an object"]

    for key in ("doc_type", "audience"):
        if not _is_anchored_field(annotations.get(key), with_confidence=True):
            errors.append(f"llm_annotations.{key} must be an anchored field with confidence")
    for key in ("terms", "series_key_candidate"):
        if not _is_anchored_field(annotations.get(key)):
            errors.append(f"llm_annotations.{key} must be an anchored field")

    doc_type = annotations.get("doc_type")
    if isinstance(doc_type, dict):
        value = doc_type.get("value")
        if value is not None and value not in DOC_TYPES:
            errors.append(f"llm_annotations.doc_type.value must be one of {list(DOC_TYPES)} or null")

    audience = annotations.get("audience")
    if isinstance(audience, dict):
        value = audience.get("value")
        if not isinstance(value, list):
            errors.append("llm_annotations.audience.value must be a list")
        elif any(item not in AUDIENCES for item in value):
            errors.append(f"llm_annotations.audience.value items must be one of {list(AUDIENCES)}")

    terms = annotations.get("terms")
    if isinstance(terms, dict) and not _is_str_list(terms.get("value")):
        errors.append("llm_annotations.terms.value must be a list of strings")

    series = annotations.get("series_key_candidate")
    if isinstance(series, dict):
        value = series.get("value")
        if value is not None and not isinstance(value, str):
            errors.append("llm_annotations.series_key_candidate.value must be a string or null")

    aliases = annotations.get("aliases")
    if not isinstance(aliases, dict) or any(not _is_anchored_field(aliases.get(sub)) for sub in ALIAS_SUBFIELDS):
        errors.append(f"llm_annotations.aliases must contain anchored fields for {list(ALIAS_SUBFIELDS)}")

    summary = annotations.get("summary_1s")
    if not isinstance(summary, dict) or not isinstance(summary.get("value"), str) or summary.get("status") not in FIELD_STATUSES:
        errors.append("llm_annotations.summary_1s must be an anchored field with a string value")

    for key in ("boilerplate_spans", "answer_bearing_spans"):
        field = annotations.get(key)
        ok = isinstance(field, dict) and field.get("status") in FIELD_STATUSES
        if ok:
            value = field.get("value")
            rejected = field.get("rejected_value")
            # value holds resolved span objects ({"text", "anchors", ...});
            # rejected_value holds the raw unsupported span strings.
            ok = (isinstance(value, list) and all(isinstance(item, dict) and isinstance(item.get("text"), str) for item in value)
                  and isinstance(rejected, list) and all(isinstance(item, str) for item in rejected))
        if not ok:
            errors.append(f"llm_annotations.{key} must be an anchored field of span objects")

    validation = record.get("validation")
    if not isinstance(validation, dict):
        errors.append("validation must be an object")
    else:
        if validation.get("metadata_supported_by_source") not in (None, True, False):
            errors.append("validation.metadata_supported_by_source must be null or boolean")
        if not _is_str_list(validation.get("unsupported_fields")):
            errors.append("validation.unsupported_fields must be a list of strings")
        if not _is_str_list(validation.get("attempted_fields")):
            errors.append("validation.attempted_fields must be a list of strings")
        if validation.get("status") not in FIELD_STATUSES:
            errors.append(f"validation.status must be one of {list(FIELD_STATUSES)}")

    provenance = record.get("provenance")
    if not isinstance(provenance, dict):
        errors.append("provenance must be an object")
    else:
        for key in ("build_id", "model", "created_at"):
            if not isinstance(provenance.get(key), str) or not provenance.get(key):
                errors.append(f"provenance.{key} must be a non-empty string")
        if provenance.get("prompt_version") != PROMPT_VERSION:
            errors.append(f"provenance.prompt_version must be {PROMPT_VERSION!r}")
        if provenance.get("annotation_schema_version") != ANNOTATION_SCHEMA_VERSION:
            errors.append(f"provenance.annotation_schema_version must be {ANNOTATION_SCHEMA_VERSION!r}")

    return errors
