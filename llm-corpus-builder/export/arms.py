"""Experimental arm exports: LLM-augmented index texts (P3, docs/PLAN.md §9).

Arms are **additive index-text augmentations** over the deterministic ``active``
corpus — block text is never modified, and a retrieval hit still returns the
original block (the four-text layering contract: ``retrieval_text`` may help
matching, citations must go back to ``raw``/``clean`` text).

Arm ladder (each level is a superset of the previous):

``active_llm_metadata``
    Doc-level anchored annotation fields (doc_type / audience / terms /
    series_key_candidate) appended to every unit of that document.
``active_llm_retrieval_text``
    Metadata plus the generated ``retrieval_text`` (Chinese/English/course-code
    aliases, one-sentence summary, table-row context lines).
``active_llm_validated``
    Metadata-level fields restricted to records whose validation says every
    attempted high-risk field is source-supported (``metadata_supported_by_source``).
``active_llm_validated_ir``
    ``active_llm_validated`` plus the Query-IR applicability/version filtering
    of ``active_ir``.

Version-discrimination guard: augmentation lines always carry the doc's own
term/audience values so adding shared aliases does not dilute the signals the
ir layer needs to separate editions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from annotate.schema import status_at_least
from evidence.store import read_jsonl

LLM_CONFIGS = ("active_llm_metadata", "active_llm_retrieval_text", "active_llm_validated", "active_llm_validated_ir", "active_llm_metadata_ir", "active_llm_metadata_ir_rr", "active_llm_metadata_ir_rr_raw", "active_llm_validated_ir_rr")
RR_CONFIGS = ("active_ir_rr", "active_llm_metadata_ir_rr", "active_llm_metadata_ir_rr_raw", "active_llm_validated_ir_rr")
IR_CONFIGS = ("active_ir", "active_llm_validated_ir", "active_llm_metadata_ir") + RR_CONFIGS

# P1 provenance contract: only the ``active_llm_validated*`` family is a gated
# arm (field-level status + doc-level gate).  The ``metadata``/``retrieval_text``
# families are exploratory — report tables must label them as such, and formal
# results (P4/P7.2) use the validated family.
VALIDATED_CONFIGS = ("active_llm_validated", "active_llm_validated_ir", "active_llm_validated_ir_rr")
EXPLORATORY_CONFIGS = tuple(config for config in LLM_CONFIGS if config not in VALIDATED_CONFIGS)

METADATA_FIELDS = ("doc_type", "audience", "terms", "series_key_candidate")


def load_annotations(build: Path) -> dict[str, dict[str, Any]]:
    """doc_uid -> annotation record from the pilot sidecar (absent → {})."""
    path = Path(build) / "llm_annotations.jsonl"
    if not path.is_file():
        return {}
    return {rec["doc_uid"]: rec for rec in read_jsonl(path)}


def load_retrieval_texts(build: Path) -> dict[str, str]:
    path = Path(build) / "llm_retrieval_text.jsonl"
    if not path.is_file():
        return {}
    return {rec["doc_uid"]: rec.get("retrieval_text", "") for rec in read_jsonl(path)}


def _field_lines(record: dict[str, Any], *, validated_only: bool) -> str:
    """Human-readable, index-friendly lines for anchored metadata fields.

    P1 field-level gate: a line is emitted only when the field's own
    ``status`` is at or above ``source_supported`` AND (for validated arms)
    the doc-level gate ``metadata_supported_by_source is True`` — ``None``
    ("nothing attempted") no longer passes as verified.  Unsupported values
    were already cleared at assembly, so this is defense in depth against
    v1-style records where an ``uncertain`` field kept its value.
    """
    annotations = record.get("llm_annotations", {})
    if validated_only and record.get("validation", {}).get("metadata_supported_by_source") is not True:
        return ""
    lines: list[str] = []
    for name, joiner in (("doc_type", None), ("audience", ","), ("terms", ","), ("series_key_candidate", None)):
        field = annotations.get(name) or {}
        value = field.get("value")
        if not value:
            continue
        if not status_at_least(field.get("status")):
            continue
        if joiner:
            text = joiner.join(value) if isinstance(value, list) else value
        else:
            text = value
        lines.append({
            "doc_type": f"文档类型：{text}",
            "audience": f"适用对象：{text}",
            "terms": f"学期：{text}",
            "series_key_candidate": f"系列：{text}",
        }[name])
    return "\n".join(lines)


def doc_augmentation(doc_uid: str, config: str, annotations: dict[str, dict[str, Any]],
                     retrieval_texts: dict[str, str]) -> str:
    """Index-text lines appended to every unit of ``doc_uid`` under ``config``."""
    record = annotations.get(doc_uid)
    if not record:
        return ""
    parts: list[str] = []
    metadata = _field_lines(record, validated_only=config.startswith("active_llm_validated"))
    if metadata:
        parts.append(metadata)
    if config == "active_llm_retrieval_text":
        extra = retrieval_texts.get(doc_uid, "")
        if extra:
            parts.append(extra)
    return "\n".join(parts)


def augment_index_text(units: Iterable, config: str, annotations: dict[str, dict[str, Any]],
                       retrieval_texts: dict[str, str]) -> list:
    """Return new units whose ``index_text`` carries the arm's augmentation.

    Units are the ``Unit`` dataclasses of ``retrieve.lexical``; only
    ``index_text`` changes (``text`` and ids are untouched).
    """
    import dataclasses

    cache: dict[str, str] = {}

    def extra_for(doc_uid: str) -> str:
        if doc_uid not in cache:
            cache[doc_uid] = doc_augmentation(doc_uid, config, annotations, retrieval_texts)
        return cache[doc_uid]

    augmented = []
    for unit in units:
        extra = extra_for(unit.doc_uid)
        if not extra or not getattr(unit, "index_text", None):
            augmented.append(unit)
            continue
        augmented.append(dataclasses.replace(unit, index_text=f"{unit.index_text}\n{extra}"))
    return augmented
