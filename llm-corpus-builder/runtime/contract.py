"""The versioned answer-context contract between the corpus runtime and its consumers.

`CorpusEngine.answer_context` returns one JSON-serializable dict whose shape is
frozen here (``answer_context.schema.json`` is the normative document; the
hand-rolled validator keeps the package dependency-free).  The online product
layer (academic-affairs-product) receives exactly this payload over HTTP and
must not re-derive retrieval, applicability, conflict or refusal decisions.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from export.arms import LLM_CONFIGS, VALIDATED_CONFIGS
from runtime.indexing import DENSE_CONFIGS

ANSWER_CONTEXT_VERSION = 2

#: Identity schema tag embedded in every pack hash (bump when the hashed
#: projection below changes meaning — old hashes never compare against new).
PACK_HASH_SCHEMA = "answer_context/2/pack"

_PACK_HASH_RE = re.compile(r"[0-9a-f]{64}")

#: Bare-start default.  ``active_ir`` is boot-safe on EVERY build (lexical+IR,
#: CPU-only, no LLM-annotation sidecars) — the SP-domain builds carry no
#: ``llm_annotations.jsonl``, so the previous default
#: ``active_llm_validated_ir_rr`` failed fast on them (caught live 2026-10-17).
#: The per-request config the product sends is what actually serves traffic;
#: this default only picks the prewarm/fail-fast index.
DEFAULT_CONFIG = "active_ir"

#: Every configuration the runtime may serve.  ``oracle_doc`` is eval-only
#: (it consumes gold documents) and is deliberately absent.
BASE_CONFIGS = ("raw", "clean", "active", "active_ir", "active_ir_rr")
SERVICE_CONFIGS = BASE_CONFIGS + LLM_CONFIGS + DENSE_CONFIGS

DECISION_STATUSES = ("sufficient", "no_material", "wrong_term", "wrong_audience", "stale_or_uncertain", "conflict")

QUERY_IR_KEYS = ("raw", "language", "terms", "academic_years", "cohorts", "audience", "course_codes")

PACK_KEYS = ("query", "query_ir", "evidence", "requirements", "coverage", "conflicts", "usable_evidence_ids", "evidence_status", "abstain", "abstain_reason")


def _canonical_json(value: Any) -> str:
    """Deterministic serialization: key-sorted, UTF-8, no whitespace."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compute_pack_hash(question: str, query_ir: dict[str, Any], decision: dict[str, Any], pack: dict[str, Any]) -> str:
    """Stable identity of one EvidencePack (schema ``answer_context/2/pack``).

    Hashed projection: question, merged query_ir, decision, the ordered usable
    list, and each evidence item's identity (evidence_id, doc/block, page,
    text, usable, blocking).  Retrieval trace and timings are deliberately
    excluded — the same evidence reached through different retrieval paths is
    the same pack.  Any change to evidence content, applicability or decision
    changes the hash; the product binds its claims to this immutability
    (2026-10-16 P0 evidence contract).
    """
    usable = list(pack.get("usable_evidence_ids") or [])
    usable_set = set(usable)
    evidence = []
    for item in pack.get("evidence") or []:
        provenance = item.get("provenance") if isinstance(item.get("provenance"), dict) else {}
        evidence.append({
            "evidence_id": item.get("evidence_id", ""),
            "doc_id": item.get("doc_id", ""),
            "block_id": provenance.get("block_id", ""),
            "page": item.get("page", 0),
            "text": item.get("text", ""),
            "usable": item.get("evidence_id") in usable_set,
            "blocking": bool(item.get("blocking", False)),
        })
    identity = {
        "schema": PACK_HASH_SCHEMA,
        "query": question,
        "query_ir": query_ir,
        "decision": decision,
        "usable_evidence_ids": usable,
        "evidence": evidence,
    }
    return hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()


def assemble_answer_context(
    *,
    question: str,
    today: str | None,
    user_context: dict[str, Any] | None,
    merged_ir,
    pack: dict[str, Any],
    engine_meta: dict[str, Any],
    latency_ms: float,
) -> dict[str, Any]:
    """Build the contract payload from one engine turn.

    ``decision`` is a projection of the pack's already-computed status — the
    runtime never re-decides; ``evidence_status``/``abstain`` come from
    ``decision.status.decide`` inside ``build_evidence_pack``.
    """
    provided = {key: value for key, value in (user_context or {}).items() if value not in (None, "", [])}
    decision = {"status": pack["evidence_status"], "abstain": bool(pack["abstain"]), "reason": pack["abstain_reason"]}
    payload = {
        "version": ANSWER_CONTEXT_VERSION,
        "question": question,
        "today": today,
        "config": engine_meta["config"],
        "query_ir": merged_ir.to_json(),
        "user_context": provided,
        "decision": decision,
        "pack_hash": compute_pack_hash(question, merged_ir.to_json(), decision, pack),
        "evidence_pack": pack,
        "provenance": {
            "build_id": engine_meta["build_id"],
            "corpus_root": engine_meta["corpus_root"],
            "config": engine_meta["config"],
            "rerank_model": engine_meta.get("rerank_model", ""),
            "dense_model": engine_meta.get("dense_model", ""),
            "n_units": engine_meta["n_units"],
            "n_documents": engine_meta["n_documents"],
            "n_known_conflicts": engine_meta["n_known_conflicts"],
            "gated_arm": engine_meta["config"] in VALIDATED_CONFIGS,
        },
        "latency_ms": round(latency_ms, 1),
    }
    return payload


def validate_answer_context(payload: Any) -> list[str]:
    """Return a list of contract violations; an empty list means valid.

    Structural check only (mirrors answer_context.schema.json); semantic
    correctness of the retrieval itself is the engine's tests' job.
    """
    problems: list[str] = []
    if not isinstance(payload, dict):
        return ["payload must be an object"]
    if payload.get("version") != ANSWER_CONTEXT_VERSION:
        problems.append(f"version must be {ANSWER_CONTEXT_VERSION}")
    pack_hash = payload.get("pack_hash")
    if not isinstance(pack_hash, str) or not _PACK_HASH_RE.fullmatch(pack_hash):
        problems.append("pack_hash must be a sha256 hex string (64 lowercase hex chars)")
    for key in ("question", "config"):
        if not isinstance(payload.get(key), str) or not payload[key]:
            problems.append(f"{key} must be a non-empty string")
    today = payload.get("today")
    if today is not None and not isinstance(today, str):
        problems.append("today must be a string (YYYY-MM-DD) or null")
    ir = payload.get("query_ir")
    if not isinstance(ir, dict):
        problems.append("query_ir must be an object")
    else:
        for key in QUERY_IR_KEYS:
            if key not in ir:
                problems.append(f"query_ir.{key} missing")
    user_context = payload.get("user_context")
    if not isinstance(user_context, dict):
        problems.append("user_context must be an object")
    decision = payload.get("decision")
    if not isinstance(decision, dict):
        problems.append("decision must be an object")
    else:
        if decision.get("status") not in DECISION_STATUSES:
            problems.append(f"decision.status must be one of {DECISION_STATUSES}")
        if not isinstance(decision.get("abstain"), bool):
            problems.append("decision.abstain must be a boolean")
        if not isinstance(decision.get("reason"), str):
            problems.append("decision.reason must be a string")
    pack = payload.get("evidence_pack")
    if not isinstance(pack, dict):
        problems.append("evidence_pack must be an object")
    else:
        for key in PACK_KEYS:
            if key not in pack:
                problems.append(f"evidence_pack.{key} missing")
        if not isinstance(pack.get("evidence"), list):
            problems.append("evidence_pack.evidence must be a list")
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict) or not provenance.get("build_id"):
        problems.append("provenance must be an object with build_id")
    if not isinstance(payload.get("latency_ms"), (int, float)):
        problems.append("latency_ms must be a number")
    return problems
