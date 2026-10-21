"""The answer pipeline — the product's one main flow, as explicit nodes.

``product.app._run_ask`` builds an :class:`AskContext` and runs PIPELINE in
order; every node below is a small function reading/writing that context.
Two mechanisms ship OFF the default product path and appear here as nodes
that no-op unless their feature flag is on (both stay for evaluation A/B):

  * ``optimize_queries``  — FEATURE_QUERY_OPTIMIZATION (OFF since the
    2026-10-16 six-arm ablation: variants pulled same-series docs and the
    conflict banner made the generator decline; zh 56->64%, en 64->69%)
  * ``follow_up``         — FEATURE_FOLLOW_UP_RETRIEVAL (explicit opt-in;
    the default request path is single-generation)

Order matters and is observable: nodes emit SSE progress events
(``optimizing_query → retrieving → retrieved → generating → answer_delta*
→ validating → done``, with ``query_optimized``/``follow_up`` only when
their node actually fires).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from fastapi import HTTPException

from product.config import RuntimeConfig, llm_enabled
from product.contracts import CONTRACT_VERSION, AnswerContextModel, ContractMismatch, parse_answer_context
from product.corpus_client import CorpusRuntimeClient, CorpusRuntimeError
from product.log import log_ask
from product.metrics import METRICS
from product.sessions import append_turn

from product.answer import generate_answer

if TYPE_CHECKING:  # app imports flow at runtime; import app types only for checkers
    from product.app import AskRequest


@dataclass
class AskContext:
    """Everything one ask carries through the pipeline."""

    request: "AskRequest"
    config: RuntimeConfig
    client: CorpusRuntimeClient
    progress: Callable[..., Any] | None
    request_id: str
    arm: str = ""
    started: float = 0.0
    retrieval_queries: list[str] | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)
    payload: AnswerContextModel | None = None
    builder_ms: float = 0.0
    llm_started: float = 0.0
    on_delta: Callable[[str], Any] | None = None
    result: dict[str, Any] = field(default_factory=dict)
    follow_ups: list[str] = field(default_factory=list)
    context_merged: bool = False
    timing: dict[str, Any] = field(default_factory=dict)
    response: dict[str, Any] = field(default_factory=dict)

    async def emit(self, stage: str, data: dict[str, Any] | None = None) -> None:
        if self.progress:
            await self.progress(stage, data)


AskNode = Callable[[AskContext], Any]


async def resolve_arm(ctx: AskContext) -> None:
    """Identity + arm override (benchmark surface) + the ask counter."""
    METRICS.inc("ask_total")
    from product.config import allowed_arms

    ctx.arm = ctx.config.arm
    if ctx.request.arm is not None:
        if ctx.request.arm not in allowed_arms():
            raise HTTPException(status_code=400, detail=f"arm {ctx.request.arm!r} not allowed; allowed: {list(allowed_arms())}")
        ctx.arm = ctx.request.arm
    ctx.started = time.perf_counter()
    await ctx.emit("optimizing_query")


async def optimize_queries(ctx: AskContext) -> None:
    """Turn the question into BM25-friendly keywords BEFORE the first
    retrieval (optional: FEATURE_QUERY_OPTIMIZATION, default off).

    The runtime uses the variants ONLY for the lexical search; IR,
    applicability and pack assembly all stay driven by the original
    question.  Local imports on purpose: tests monkeypatch
    ``providers.ApiProvider`` / ``answer._BREAKER`` at call time.
    """
    if not (ctx.config.query_optimization and llm_enabled(ctx.config)):
        return
    from product.providers import ApiProvider as _AP
    from product.answer import _BREAKER as _shared_breaker

    optimizer = _AP(ctx.config, breaker=_shared_breaker)
    optimized = await optimizer.optimize_queries(ctx.request.question)
    if optimized and optimized != [ctx.request.question]:
        ctx.retrieval_queries = list(dict.fromkeys([ctx.request.question, *optimized]))[:4]
        await ctx.emit("query_optimized", {"queries": optimized})


async def _call_runtime(ctx: AskContext, retrieval_queries: list[str] | None) -> dict[str, Any]:
    """One /v1/answer_context call (the ONLY retrieval call sites wrap this)."""
    return await ctx.client.answer_context(
        question=ctx.request.question,
        config=ctx.arm,
        today=ctx.request.today,
        user_context=ctx.request.user_context,
        k=ctx.request.top_k or ctx.config.top_k,
        pack_limit=ctx.config.pack_limit,
        request_id=ctx.request_id,
        retrieval_queries=retrieval_queries,
    )


def _retrieved_event(ctx: AskContext) -> dict[str, Any]:
    # One entry per source document, with its link, so the UI can list the
    # original materials while the answer is still being generated.
    documents: dict[str, dict[str, Any]] = {}
    for item in ctx.payload.evidence_pack.evidence:
        if item.doc_id in documents:
            continue
        provenance = item.provenance
        documents[item.doc_id] = {
            "title": item.title or item.doc_id, "doc_uid": item.doc_id,
            "source_url": provenance.source_url or "",
            "publisher": getattr(provenance, "publisher", "") or "",
            "published": getattr(provenance, "published", "") or "",
        }
        if len(documents) == 8:
            break
    return {
        "documents": list(documents.values()),
        "n_evidence": len(ctx.payload.evidence_pack.evidence),
        "decision": ctx.payload.decision.model_dump(),
    }


#: Deictic follow-up shapes: a 那/这/它 opener, a trailing 呢, "what/how about",
#: or simply very short (nothing retrievable of its own).
_DEICTIC_HEAD_RE = re.compile(r"^(那|那么|这个|那个|它|这|他|她|他们|她们)")
_ABOUT_RE = re.compile(r"\b(what|how)\s+about\b", re.IGNORECASE)


def followup_variants(question: str, context_question: str | None) -> list[str] | None:
    """Deterministic follow-up retrieval merge (reviewer round 1, item 4).

    A deictic short question (「那研究生呢？」) carries no retrievable terms of
    its own.  When the client supplies the PREVIOUS question, it joins the
    retrieval variants ONLY — the same variant pool the alias bridge uses —
    and generation still answers the current question from the current pack.
    Fires only when context is supplied, so single-turn asks (and the frozen
    benchmark harness, which never sends context) are byte-identical.
    """
    if not context_question:
        return None
    q = question.strip()
    deictic = (
        len(q) <= 20
        or bool(_DEICTIC_HEAD_RE.match(q))
        or bool(_ABOUT_RE.search(q))
        or q.endswith("呢") or q.endswith("呢？") or q.endswith("呢?")
    )
    if not deictic:
        return None
    return list(dict.fromkeys([question, f"{question} {context_question}"]))[:9]


async def retrieve(ctx: AskContext) -> None:
    """Fetch + contract-validate the evidence pack from the builder runtime."""
    await ctx.emit("retrieving")
    variants = followup_variants(ctx.request.question, ctx.request.context_question)
    if variants and not ctx.retrieval_queries:
        ctx.retrieval_queries = variants
        ctx.context_merged = True
        await ctx.emit("follow_up_context", {"queries": variants})
    builder_started = time.perf_counter()
    try:
        raw_payload = await _call_runtime(ctx, ctx.retrieval_queries)
        ctx.builder_ms = (time.perf_counter() - builder_started) * 1000
        payload = parse_answer_context(raw_payload)
    except CorpusRuntimeError as error:
        METRICS.inc("ask_error")
        METRICS.inc("error_builder")
        log_ask({"request_id": ctx.request_id, "question": ctx.request.question, "error": str(error), "stage": "builder", "latency_ms": round((time.perf_counter() - ctx.started) * 1000, 1)})
        raise HTTPException(status_code=502, detail=str(error)) from error
    except ContractMismatch as mismatch:
        METRICS.inc("ask_error")
        METRICS.inc("error_contract")
        log_ask({"request_id": ctx.request_id, "question": ctx.request.question, "error": f"contract: {mismatch}", "stage": "contract"})
        raise HTTPException(status_code=502, detail={"error": "dependency_contract_error", "request_id": ctx.request_id, "problems": mismatch.problems}) from mismatch
    ctx.raw_payload, ctx.payload = raw_payload, payload
    await ctx.emit("retrieved", _retrieved_event(ctx))


async def generate(ctx: AskContext) -> None:
    """Stream the structured LLM answer over the evidence pack."""
    await ctx.emit("generating")
    ctx.llm_started = time.perf_counter()

    async def _on_delta(text: str) -> None:
        await ctx.emit("answer_delta", {"text": text})

    ctx.on_delta = _on_delta if ctx.progress else None
    ctx.result = await generate_answer(ctx.payload, ctx.config, request_id=ctx.request_id, on_delta=ctx.on_delta, language=ctx.request.language or "")


async def follow_up(ctx: AskContext) -> None:
    """One autonomous supplementary round (optional: FEATURE_FOLLOW_UP_RETRIEVAL).

    Rebuild the whole pack in the runtime under the ORIGINAL question's IR —
    product-side appending bypassed applicability/decision recomputation and
    returned a stale pack.  A failed round must never lose the first answer.
    """
    ctx.follow_ups = list(ctx.result.get("follow_ups") or [])
    if not (ctx.config.follow_up_retrieval and ctx.follow_ups and not ctx.payload.decision.abstain):
        return
    await ctx.emit("follow_up", {"queries": ctx.follow_ups})
    try:
        base_queries = list(ctx.retrieval_queries or [ctx.request.question])
        expanded_queries = list(dict.fromkeys([*base_queries, *ctx.follow_ups[:2]]))[:6]
        raw_extra = await _call_runtime(ctx, expanded_queries)
        extra = parse_answer_context(raw_extra)
        if extra.provenance.build_id != ctx.payload.provenance.build_id:
            raise ContractMismatch(["build changed during follow-up; retaining original answer"])
        # Assign only after the new context has passed the contract check.
        ctx.raw_payload, ctx.payload = raw_extra, extra
        await ctx.emit("retrieved", _retrieved_event(ctx))
        await ctx.emit("generating")
        ctx.result = await generate_answer(ctx.payload, ctx.config, request_id=ctx.request_id, on_delta=ctx.on_delta, language=ctx.request.language or "")
        METRICS.inc("follow_up_round")
    except (CorpusRuntimeError, ContractMismatch) as error:
        log_ask({"request_id": ctx.request_id, "question": ctx.request.question, "error": f"follow_up skipped: {error}", "stage": "follow_up"})


async def finalize(ctx: AskContext) -> None:
    """Timings + decision/generation/status counters."""
    llm_ms = (time.perf_counter() - ctx.llm_started) * 1000
    await ctx.emit("validating")
    total_ms = (time.perf_counter() - ctx.started) * 1000
    ctx.timing = {"retrieval_ms": ctx.payload.latency_ms, "builder_ms": round(ctx.builder_ms, 1), "llm_ms": round(llm_ms, 1), "total_ms": round(total_ms, 1)}
    METRICS.inc(f"decision_{ctx.payload.decision.status}")
    METRICS.inc(f"generation_{ctx.result['generation']['mode']}")
    METRICS.inc(f"status_{ctx.result['status']}")
    METRICS.observe(total_ms, ctx.timing)


async def build_response(ctx: AskContext) -> None:
    """Assemble the API response body."""
    trace = ctx.payload.retrieval_trace or {}
    result = ctx.result
    ctx.response = {
        "ok": True,
        "request_id": ctx.request_id,
        "contract_version": CONTRACT_VERSION,
        "build_id": ctx.payload.provenance.build_id,
        "config": ctx.payload.config,
        "status": result["status"],
        "pack_hash": ctx.payload.pack_hash,
        "missing_requirements": result.get("missing_requirements", []),
        "decision": ctx.payload.decision.model_dump(),
        "diagnostics": {
            "decision_status": ctx.payload.decision.status,
            "generation_mode": result["generation"]["mode"],
            "validation_problems": result["generation"]["problems"],
            "pack_hash": ctx.payload.pack_hash,
            "build_id": ctx.payload.provenance.build_id,
            "retrieval_strategy": trace.get("strategy", ""),
        },
        "query_ir": ctx.raw_payload.get("query_ir", {}),
        "answer": result["answer"],
        "provider": result["provider"],
        "claims": result["claims"],
        "generation": result["generation"],
        "answer_disposition": result.get("answer_disposition", "unknown"),
        "citation_map": result.get("citation_map", {}),
        "retrieval_trace": ctx.raw_payload.get("retrieval_trace", {}),
        "follow_up_queries": ctx.follow_ups,
        "citations": result["citations"],
        "evidence_pack": ctx.payload.evidence_pack.model_dump(),
        "timing": ctx.timing,
    }


async def persist(ctx: AskContext) -> None:
    """Audit log + session turn (display/audit only, never retrieval context)."""
    result = ctx.result
    log_ask({
        "request_id": ctx.request_id,
        "session_id": ctx.request.session_id,
        "question": ctx.request.question,
        "build_id": ctx.payload.provenance.build_id,
        "pack_hash": ctx.payload.pack_hash,
        "status": result["status"],
        "config": ctx.payload.config,
        "decision": ctx.payload.decision.model_dump(),
        "answer_provider": result["provider"],
        "generation": result["generation"],
        "n_citations": len(result["citations"]),
        "n_claims": len(result["claims"]),
        # evidence-layer drop telemetry (2026-10-15): top candidates the pack
        # assembly discarded — makes "governance dropped the answer block"
        # visible in the audit log instead of indistinguishable from "nothing
        # in the corpus".
        "n_dropped_top": len(getattr(ctx.payload.evidence_pack, "dropped_candidates", None) or []),
        "context_merged": ctx.context_merged,
        "timing": ctx.timing,
    })
    if ctx.request.session_id:
        append_turn(ctx.request.session_id, {
            "request_id": ctx.request_id,
            "question": ctx.request.question,
            "decision": ctx.payload.decision.model_dump(),
            "answer": result["answer"][:2000],
            "provider": result["provider"],
            "build_id": ctx.payload.provenance.build_id,
            "config": ctx.payload.config,
            # status + citations (reviewer round 1, item 5): the 👎 review list
            # joins feedback→turn by request_id, so a rating shows the full ask.
            "status": result["status"],
            "citations": result["citations"][:8],
        })
    await ctx.emit("done")


PIPELINE: tuple[AskNode, ...] = (
    resolve_arm,        # request_id binding done by the driver; arm whitelist
    optimize_queries,   # optional — FEATURE_QUERY_OPTIMIZATION (default off)
    retrieve,           # /v1/answer_context + contract v1 validation
    generate,           # streaming structured answer + claim validation
    follow_up,          # optional — FEATURE_FOLLOW_UP_RETRIEVAL (default off)
    finalize,           # timings + metrics counters
    build_response,     # API response body
    persist,            # ask log + session turn + done event
)
