"""Academic-affairs product API (P0–P5).

One audited path: /v1/ask(+/stream) → runtime /v1/answer_context (validated
against contract v1) → structured LLM answer with claim validation →
response with answers, claims, citations, refusals, build_id, per-stage
timings.  The flow itself lives in ``product.flow`` as an explicit ordered
node pipeline; this module keeps only HTTP/SSE wiring.  P5 adds sessions
(display/audit only — never retrieval context), feedback, an SSE stage
stream, an admin proxy and the built web UI.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, AsyncIterator, Callable

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from product.answer import breaker_state
from product.config import RuntimeConfig, classify_runtime_error, llm_enabled, load_config
from product.contracts import CONTRACT_VERSION
from product.corpus_client import CorpusRuntimeClient, CorpusRuntimeError
from product.flow import PIPELINE, AskContext
from product.materials import list_materials, save_upload
from product.metrics import METRICS
from product.ratelimit import RateLimiter, capacity_guard
from product.sessions import append_feedback, feedback_rows, find_turn, history, new_session_id

PRODUCT_VERSION = "0.5.0"
REPO_ROOT = Path(__file__).resolve().parents[1]

_MAX_BODY_BYTES = 64 * 1024
_ASK_LIMITER = RateLimiter(rate_per_minute=float(os.getenv("RATE_LIMIT_PER_MINUTE", "30")), burst=int(os.getenv("RATE_LIMIT_BURST", "10")))
_ASK_SEMAPHORE = asyncio.Semaphore(int(os.getenv("MAX_CONCURRENT_ASKS", "20")))


async def _capacity_dependency(request: Request):
    async with capacity_guard(request, max_body_bytes=_MAX_BODY_BYTES, limiter=_ASK_LIMITER, semaphore=_ASK_SEMAPHORE):
        yield


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    today: str | None = Field(default=None, description="YYYY-MM-DD; runtime server date when omitted")
    user_context: dict[str, Any] | None = Field(default=None, description="audience/cohort/term/academic_year/program/course_codes")
    top_k: int | None = Field(default=None, ge=1, le=50)
    session_id: str | None = Field(default=None, description="P5: record this turn in a session (display/audit only)")
    context_question: str | None = Field(default=None, max_length=2000, description="previous turn's question; RETRIEVAL-ONLY context merged for deictic follow-ups (那研究生呢？) — the answer is still generated for the current question from the current pack")
    arm: str | None = Field(default=None, description="override the runtime arm (benchmark; must be in the product whitelist)")
    language: Literal["zh", "en"] | None = Field(default=None, description="answer language chosen in the UI; None keeps the persona default")


class FeedbackRequest(BaseModel):
    request_id: str
    helpful: bool
    comment: str = Field(default="", max_length=2000)


class Product:
    config: RuntimeConfig | None = None
    client: CorpusRuntimeClient | None = None
    runtime_health: dict[str, Any] = {}


product = Product()


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        config = load_config()
    except Exception as error:  # ConfigError, categorized
        raise RuntimeError(f"product configuration invalid: {error}") from error
    client = CorpusRuntimeClient(config.builder_url, timeout=config.request_timeout)
    try:
        product.runtime_health = await client.health()
    except CorpusRuntimeError as error:
        await client.aclose()
        kind = classify_runtime_error(str(error))
        raise RuntimeError(f"[{kind}] corpus runtime not available at {config.builder_url} — start it first: python -m runtime.service ({error})") from error
    product.config = config
    product.client = client
    METRICS.set_build(str(product.runtime_health.get("build_id", "")), config.arm)
    yield
    await client.aclose()


app = FastAPI(title="Academic Affairs Product", version=PRODUCT_VERSION, lifespan=lifespan)


@app.get("/health/live")
def health_live() -> dict[str, Any]:
    return {"ok": True, "service": "product", "version": PRODUCT_VERSION}


@app.get("/health")
@app.get("/health/ready")
async def health_ready() -> dict[str, Any]:
    if product.client is None or product.config is None:
        raise HTTPException(status_code=503, detail="product is starting or misconfigured")
    try:
        runtime = await product.client.health()
    except CorpusRuntimeError as error:
        raise HTTPException(status_code=503, detail=f"[{classify_runtime_error(str(error))}] {error}") from error
    return {
        "ok": True,
        "arm": product.config.arm,
        "llm_enabled": llm_enabled(product.config),
        "contract_version": CONTRACT_VERSION,
        "runtime": {key: runtime.get(key) for key in ("build_id", "config", "n_units", "n_documents", "version")},
    }


@app.get("/version")
async def version() -> dict[str, Any]:
    return {
        "product": PRODUCT_VERSION,
        "contract_version": CONTRACT_VERSION,
        "arm": product.config.arm if product.config else None,
        "runtime": {key: product.runtime_health.get(key) for key in ("build_id", "config", "version")},
    }


@app.get("/metrics")
def metrics() -> dict[str, Any]:
    snapshot = METRICS.snapshot()
    snapshot["llm_breaker"] = breaker_state()
    return snapshot


@app.middleware("http")
async def _admin_gate(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Password gate for owner-only surfaces (2026-10-17 public-tunnel hardening).

    PRODUCT_ADMIN_PASSWORD unset → open (local default, unchanged behavior).
    Set → HTTP Basic on /admin/* (build switch/rollback/smoke) AND
    /v1/materials (upload → auto-build → auto-switch is switch-equivalent
    power through the public link) while /v1/ask, the web UI and sessions
    stay passwordless — testers keep asking.  The runtime additionally
    enforces X-Admin-Token (RUNTIME_ADMIN_TOKEN).
    """
    password = os.getenv("PRODUCT_ADMIN_PASSWORD", "")
    if password and (request.url.path.startswith("/admin") or request.url.path.startswith("/v1/materials")):
        import base64
        import hmac as _hmac

        expected = "Basic " + base64.b64encode(f"admin:{password}".encode()).decode()
        supplied = request.headers.get("Authorization", "")
        if not _hmac.compare_digest(expected, supplied):
            return JSONResponse({"detail": "admin unauthorized"}, status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="zjui-qa-admin"'})
    return await call_next(request)


# ------------------------------------------------------------------ ask core

async def _run_ask(request: AskRequest, progress: Callable[[str], Any] | None = None) -> dict[str, Any]:
    """The one answer flow shared by /v1/ask and /v1/ask/stream: run the
    ordered nodes of product.flow.PIPELINE over a fresh AskContext."""
    if product.client is None or product.config is None:
        raise HTTPException(status_code=503, detail="product is starting or misconfigured")
    ctx = AskContext(
        request=request,
        config=product.config,
        client=product.client,
        progress=progress,
        request_id=uuid.uuid4().hex[:16],
    )
    for node in PIPELINE:
        await node(ctx)
    return ctx.response


@app.post("/v1/ask")
async def ask(request: AskRequest, _: None = Depends(_capacity_dependency)) -> dict[str, Any]:
    return await _run_ask(request)


@app.post("/v1/ask/stream")
async def ask_stream(request: AskRequest, _: None = Depends(_capacity_dependency)) -> StreamingResponse:
    """SSE stage stream: retrieving → generating → validating → result."""

    async def events() -> AsyncIterator[str]:
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        async def progress(stage: str, data: dict[str, Any] | None = None) -> None:
            # rich progress events get their own SSE type; plain stages stay
            # {"type": "stage", "name": ...} for the progress chips
            event_type = stage if stage in ("retrieved", "answer_delta", "follow_up", "query_optimized") else "stage"
            event = {"type": event_type, "name": stage, **(data or {})}
            await queue.put("data: " + json.dumps(event, ensure_ascii=False) + "\n\n")

        async def worker() -> None:
            try:
                response = await _run_ask(request, progress=progress)
                await queue.put("data: " + json.dumps({"type": "result", "payload": response}, ensure_ascii=False) + "\n\n")
            except HTTPException as error:
                detail = error.detail if isinstance(error.detail, dict) else {"error": str(error.detail)}
                await queue.put("data: " + json.dumps({"type": "error", "status": error.status_code, **detail}, ensure_ascii=False) + "\n\n")
            except Exception as error:  # pragma: no cover — never leak a stack trace
                await queue.put("data: " + json.dumps({"type": "error", "status": 500, "error": "internal_error"}, ensure_ascii=False) + "\n\n")
            finally:
                await queue.put(None)

        task = asyncio.create_task(worker())
        while True:
            item = await queue.get()
            if item is None:
                break
            yield item
        await task

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ------------------------------------------------------------------ sessions & feedback (P5)

@app.post("/v1/session")
def create_session() -> dict[str, str]:
    return {"session_id": new_session_id()}


@app.get("/v1/session/{session_id}")
def get_session(session_id: str) -> dict[str, Any]:
    return {"session_id": session_id, "turns": history(session_id)}


@app.post("/v1/feedback")
def feedback(request: FeedbackRequest) -> dict[str, Any]:
    append_feedback({"request_id": request.request_id, "helpful": request.helpful, "comment": request.comment[:500]})
    METRICS.inc("feedback_helpful" if request.helpful else "feedback_unhelpful")
    return {"ok": True}


# ------------------------------------------------------------------ material import (Stage 5 V3)

@app.post("/v1/materials")
async def upload_material(
    file: UploadFile,
    kind: str = Form(...),
    site: str = Form("sharepoint"),
    title: str = Form(""),
    url: str = Form(""),
    published: str = Form(""),
    path: str = Form(""),
) -> dict[str, Any]:
    result = await save_upload(file, kind=kind, site=site, title=title, url=url, published=published, path=path)
    METRICS.inc("materials_uploaded")
    return result


@app.get("/v1/materials")
def materials_status() -> dict[str, Any]:
    return list_materials()


# ------------------------------------------------------------------ admin proxy (P5, read-mostly)

def _admin_token() -> str:
    return os.getenv("RUNTIME_ADMIN_TOKEN", "")


async def _runtime_admin(method: str, path: str, json_body: Any = None) -> Any:
    """Forward an admin call to the runtime (localhost or shared token)."""
    if product.config is None:
        raise HTTPException(status_code=503, detail="product is starting")
    headers = {"X-Admin-Token": _admin_token()} if _admin_token() else {}
    async with httpx.AsyncClient(base_url=product.config.builder_url, timeout=120) as client:
        response = await client.request(method, path, json=json_body, headers=headers)
    if response.status_code != 200:
        raise HTTPException(status_code=response.status_code, detail=response.text[:300])
    return response.json()


@app.get("/admin/overview")
async def admin_overview() -> dict[str, Any]:
    runtime = await _runtime_admin("GET", "/admin/builds")
    return {"product": metrics(), "runtime": runtime}


@app.get("/admin/builds")
async def admin_builds() -> dict[str, Any]:
    return await _runtime_admin("GET", "/admin/builds")


@app.post("/admin/builds")
async def admin_load_build(body: dict[str, Any]) -> dict[str, Any]:
    return await _runtime_admin("POST", "/admin/builds", body)


@app.post("/admin/builds/{key}/smoke")
async def admin_smoke(key: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    return await _runtime_admin("POST", f"/admin/builds/{key}/smoke", body or {})


@app.post("/admin/switch")
async def admin_switch(body: dict[str, Any]) -> dict[str, Any]:
    result = await _runtime_admin("POST", "/admin/switch", body)
    if product.client is not None:  # refresh build tag shown in metrics/health
        try:
            health = await product.client.health()
            METRICS.set_build(str(health.get("build_id", "")), product.config.arm if product.config else "")
        except CorpusRuntimeError:
            pass
    return result


@app.post("/admin/rollback")
async def admin_rollback() -> dict[str, Any]:
    result = await _runtime_admin("POST", "/admin/rollback", {})
    if product.client is not None:
        try:
            health = await product.client.health()
            METRICS.set_build(str(health.get("build_id", "")), product.config.arm if product.config else "")
        except CorpusRuntimeError:
            pass
    return result


@app.get("/admin/rejected")
async def admin_rejected(limit: int = 50) -> dict[str, Any]:
    return await _runtime_admin("GET", f"/admin/rejected?limit={limit}")


@app.get("/admin/feedback")
async def admin_feedback(limit: int = 50) -> dict[str, Any]:
    """👎 review list (reviewer round 1, item 5): feedback rows joined with the
    ask turn by request_id — question, answer, status, build and citations on
    one screen, so a rating becomes an eval-question lead instead of a counter."""
    rows = feedback_rows(limit)
    joined = []
    for row in rows:
        turn = find_turn(str(row.get("request_id", ""))) or {}
        joined.append({
            "ts": row.get("ts", ""),
            "request_id": row.get("request_id", ""),
            "helpful": bool(row.get("helpful")),
            "comment": row.get("comment", ""),
            "question": turn.get("question", ""),
            "answer": turn.get("answer", ""),
            "status": turn.get("status", ""),
            "build_id": turn.get("build_id", ""),
            "citations": turn.get("citations", []),
        })
    return {"n": len(joined), "rows": joined}


# ------------------------------------------------------------------ web UI (built by P5; served same-origin)

_web_dist = REPO_ROOT / "web" / "dist"
if _web_dist.is_dir():
    app.mount("/", StaticFiles(directory=str(_web_dist), html=True), name="web")
