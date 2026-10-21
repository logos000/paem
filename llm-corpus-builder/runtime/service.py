"""HTTP façade over CorpusEngine.answer_context (Stage 3A integration form).

The product layer (academic-affairs-product) is a thin client of
``POST /v1/answer_context``; retrieval, applicability, conflicts, the
EvidencePack and the refusal decision all live in this process.  Models load
once at startup and stay resident; per-request config switches lazily load
another engine (first request per config pays the index build).

Run from the repository root::

    python -m runtime.service --config active_ir --port 8772
    # per-request configs (e.g. active_hybrid, the product sp arm) load lazily

Two-stage boot (the product deployment design, 2026-10-17):

1. The service boots with the CHEAP universal config (``active_ir``: lexical+IR,
   CPU-only, loads on any build) — it is the fail-fast contract; if it cannot
   load, startup refuses.  A dense boot would die on machines without the
   bge-m3 model directory or a GPU, and would hold VRAM from boot.
2. The product launcher (``academic-affairs-product/scripts/launch.ps1``)
   prewarms the SERVING config (``active_hybrid``) right after boot — the
   model + dense matrix load once, so the first user question is warm.  A bare
   CLI runtime instead pays that one-time lazy load on its first hybrid
   request.  Docker (deploy/docker-compose.yml) does not prewarm: mount the
   models volume for hybrid, or the first request pays the load.
"""

from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
import json
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from runtime.contract import ANSWER_CONTEXT_VERSION, DEFAULT_CONFIG, SERVICE_CONFIGS, validate_answer_context
from runtime.engine import CorpusEngine
from runtime.indexing import DENSE_MODEL
from runtime.user_context import USER_CONTEXT_KEYS
from evidence.store import read_jsonl

REPO_ROOT = Path(__file__).resolve().parents[1]

#: P2 capacity guard: at most N concurrent engine turns; a request that waits
#: longer than the queue timeout is rejected with 503 instead of queueing
#: forever behind the reranker/dense model lock.
_MAX_CONCURRENT = int(os.environ.get("ENGINE_MAX_CONCURRENCY", "4"))
_QUEUE_WAIT_SECONDS = float(os.environ.get("ENGINE_QUEUE_WAIT_SECONDS", "20"))
_ENGINE_SLOTS = threading.BoundedSemaphore(_MAX_CONCURRENT)


class _EngineSlot:
    """acquire(timeout=...) as a context manager; raises 503 on timeout."""

    def __enter__(self) -> "_EngineSlot":
        if not _ENGINE_SLOTS.acquire(timeout=_QUEUE_WAIT_SECONDS):
            raise HTTPException(status_code=503, detail=f"runtime at capacity ({_MAX_CONCURRENT} concurrent turns); retry later")
        return self

    def __exit__(self, *exc: Any) -> None:
        _ENGINE_SLOTS.release()


@dataclass
class ServiceSettings:
    build_root: Path | None = None
    config: str = DEFAULT_CONFIG
    rerank_model: str = "bge-base"
    device: str = ""
    dense_model: str = DENSE_MODEL
    log_path: Path | None = None


class AnswerContextRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    today: str | None = Field(default=None, description="YYYY-MM-DD; server date when omitted")
    user_context: dict[str, Any] | None = None
    config: str | None = None
    k: int = Field(default=10, ge=1, le=50)
    pack_limit: int = Field(default=8, ge=1, le=16)
    retrieval_query: str | None = Field(default=None, max_length=2000, description="LLM-optimized BM25 keywords; question stays authoritative for IR/pack")
    retrieval_queries: list[Annotated[str, Field(min_length=1, max_length=2000)]] | None = Field(default=None, max_length=6, description="Separate query variants fused under the original question's applicability constraints")


class AdminLoadRequest(BaseModel):
    build_dir: str = Field(description="build directory path (manifest.json inside) or a LATEST pointer directory")


class AdminSwitchRequest(BaseModel):
    key: str


class AdminSmokeRequest(BaseModel):
    questions: list[str] = Field(default_factory=list)


@dataclass
class BuildSlot:
    """One build loaded (or loading) into the pool; engines per config."""

    key: str
    root: Path | None
    loading: bool = False
    error: str = ""
    build_id: str = ""
    engines: dict[str, CorpusEngine] = field(default_factory=dict)


class EnginePool:
    """Multi-build pool (P4): a default build serves traffic; other builds
    load in the background, pass a smoke, then switch atomically.  A request
    may pin a build via ``X-Target-Build`` for canary checks."""

    def __init__(self, settings: ServiceSettings):
        self.settings = settings
        self.builds: dict[str, BuildSlot] = {}
        self.default_key = "default"
        self.history: list[str] = ["default"]
        self.lock = threading.Lock()
        self.log_lock = threading.Lock()

    # -- engine access ------------------------------------------------------

    def _load_engine(self, slot: BuildSlot, config: str) -> CorpusEngine:
        engine = CorpusEngine.load(
            slot.root,
            config=config,
            rerank_model=self.settings.rerank_model,
            device=self.settings.device,
            dense_model=self.settings.dense_model,
            # Windows: ProcessPoolExecutor spawned from a background (daemon)
            # thread deadlocks — service-side index builds tokenize serially.
            index_workers=1,
        )
        slot.engines[config] = engine
        slot.build_id = engine.corpus.build_id
        return engine

    def get(self, config: str | None, target_build: str | None = None) -> CorpusEngine:
        name = config or self.settings.config
        if name not in SERVICE_CONFIGS:
            raise HTTPException(status_code=400, detail=f"unknown config {name!r}; allowed: {list(SERVICE_CONFIGS)}")
        key = target_build or self.default_key
        if key == "default" and key not in self.builds:
            with self.lock:  # lazy self-heal when the lifespan did not run (tests)
                if "default" not in self.builds:
                    self.start_default()
        slot = self.builds.get(key)
        if slot is None:
            raise HTTPException(status_code=404, detail=f"unknown build {key!r}; loaded: {list(self.builds)}")
        if slot.loading:
            raise HTTPException(status_code=503, detail=f"build {key!r} is still loading")
        if slot.error:
            raise HTTPException(status_code=503, detail=f"build {key!r} failed to load: {slot.error[:200]}")
        engine = slot.engines.get(name)
        if engine is not None:
            return engine
        with self.lock:
            engine = slot.engines.get(name)
            if engine is None:
                engine = self._load_engine(slot, name)
        return engine

    # -- admin: load / switch / rollback (P4) -------------------------------

    def start_default(self) -> None:
        slot = BuildSlot(key="default", root=self.settings.build_root, loading=True)
        self.builds["default"] = slot
        self._load_engine(slot, self.settings.config)  # startup: fail fast
        slot.loading = False

    def start_load(self, key: str, root: Path) -> dict[str, Any]:
        with self.lock:
            if key in self.builds and key != "default" and not self.builds[key].loading and not self.builds[key].error:
                return {"key": key, "status": "already_loaded", "build_id": self.builds[key].build_id}
            self.builds[key] = BuildSlot(key=key, root=root, loading=True)

        def _worker() -> None:
            slot = self.builds[key]
            try:
                self._load_engine(slot, self.settings.config)
                slot.loading = False
                self.audit("load", key=key, detail={"build_id": slot.build_id, "root": str(root)})
            except BaseException as error:  # SystemExit (missing sidecar) too; the old default keeps serving
                slot.loading = False
                slot.error = f"{type(error).__name__}: {error}"
                self.audit("load_failed", key=key, detail={"error": slot.error[:300]})

        threading.Thread(target=_worker, daemon=True, name=f"load-{key}").start()
        return {"key": key, "status": "loading"}

    def switch(self, key: str) -> dict[str, Any]:
        slot = self.builds.get(key)
        if slot is None or slot.loading or (slot.error and not slot.engines):
            raise HTTPException(status_code=409, detail=f"build {key!r} is not loaded healthy; loaded: {self.status()}")
        previous = self.default_key
        with self.lock:
            self.default_key = key
            self.history.append(key)
            del self.history[:-20]
        self.audit("switch", key=key, detail={"from": previous, "to": key, "build_id": slot.build_id})
        return {"previous": previous, "current": key, "build_id": slot.build_id}

    def rollback(self) -> dict[str, Any]:
        if len(self.history) < 2:
            raise HTTPException(status_code=409, detail="no previous build to roll back to")
        target = self.history[-2]
        result = self.switch(target)
        self.history = self.history[:-2] + [target]  # collapse the failed forward
        return {**result, "rolled_back": True}

    def smoke(self, key: str, questions: list[str]) -> dict[str, Any]:
        canned = dict(SMOKE_QUESTIONS)
        results = []
        ok_flags = []
        for question in questions or [q for q, _ in SMOKE_QUESTIONS]:
            payload = self.get(None, target_build=key).answer_context(question)
            status = payload["decision"]["status"]
            expected = canned.get(question, _ANY_STATUS)
            ok_flags.append(status in expected)
            results.append({"question": question[:60], "status": status, "build_id": payload["provenance"]["build_id"], "latency_ms": payload["latency_ms"]})
        all_ok = bool(results) and all(ok_flags) and all(item["build_id"] == results[0]["build_id"] for item in results)
        summary = {"results": results, "all_ok": all_ok, "build_id": results[0]["build_id"] if results else ""}
        self.audit("smoke", key=key, detail={"all_ok": all_ok, "n": len(results)})
        return summary

    def status(self) -> dict[str, Any]:
        return {
            "default": self.default_key,
            "builds": [
                {"key": slot.key, "loading": slot.loading, "error": slot.error[:200], "build_id": slot.build_id,
                 "root": str(slot.root) if slot.root else "", "engines": sorted(slot.engines)}
                for slot in self.builds.values()
            ],
        }

    # -- logging ------------------------------------------------------------

    def audit(self, action: str, *, key: str = "", detail: dict[str, Any] | None = None) -> None:
        path = self.settings.log_path.parent / "admin_audit.jsonl" if self.settings.log_path else None
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "action": action, "key": key, **(detail or {})}, ensure_ascii=False) + "\n")

    def log(self, record: dict[str, Any]) -> None:
        path = self.settings.log_path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")


#: Canned release smoke: (question, acceptable decision statuses).  The
#: third question MUST abstain — losing the future-term refusal is a
#: release-blocking regression.
SMOKE_QUESTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ZJUI 2026 fall undergraduate course withdrawal deadline?", ("sufficient", "conflict")),
    ("What is the add/drop deadline for FA 2026?", ("sufficient", "conflict")),
    ("What is the withdrawal deadline for 2031 spring term?", ("wrong_term", "no_material")),
)
_ANY_STATUS = ("sufficient", "conflict", "no_material", "wrong_term", "wrong_audience", "stale_or_uncertain")


def create_app(settings: ServiceSettings) -> FastAPI:
    pool = EnginePool(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        pool.start_default()  # fail fast when the default engine cannot load
        yield

    app = FastAPI(title="llm-corpus-builder retrieval runtime", version=f"v{ANSWER_CONTEXT_VERSION}", lifespan=lifespan)

    def _local_only(request: Request, x_admin_token: str | None = Header(default=None, alias="X-Admin-Token")) -> None:
        host = request.client.host if request.client else ""
        token = os.environ.get("RUNTIME_ADMIN_TOKEN", "")
        if host not in ("127.0.0.1", "::1", "localhost", "testclient"):
            if not token or x_admin_token != token:
                raise HTTPException(status_code=403, detail="admin endpoints are localhost-only (or require RUNTIME_ADMIN_TOKEN)")

    @app.get("/health")
    def health() -> dict[str, Any]:
        try:
            engine = pool.get(None)
        except FileNotFoundError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return engine.health()

    @app.post("/v1/answer_context")
    def answer_context(request: AnswerContextRequest, x_request_id: str | None = Header(default=None), x_target_build: str | None = Header(default=None, alias="X-Target-Build")) -> dict[str, Any]:
        request_id = x_request_id or uuid.uuid4().hex[:16]
        try:
            engine = pool.get(request.config, target_build=x_target_build)
        except FileNotFoundError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        try:
            today = date.fromisoformat(request.today) if request.today else None
        except ValueError as error:
            raise HTTPException(status_code=400, detail=f"today must be YYYY-MM-DD: {error}") from error
        unknown = [key for key in (request.user_context or {}) if key not in USER_CONTEXT_KEYS]
        if unknown:
            raise HTTPException(status_code=400, detail=f"unknown user_context keys: {unknown}; allowed: {list(USER_CONTEXT_KEYS)}")
        with _EngineSlot():
            payload = engine.answer_context(
                request.question,
                today=today,
                user_context=request.user_context,
                k=request.k,
                pack_limit=request.pack_limit,
                retrieval_query=request.retrieval_query,
                retrieval_queries=request.retrieval_queries,
            )
        problems = validate_answer_context(payload)
        if problems:
            raise HTTPException(status_code=500, detail=f"contract violation: {problems}")
        payload["request_id"] = request_id
        pool.log({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "request_id": request_id,
            "question": request.question,
            "today": payload["today"],
            "config": payload["config"],
            "build_id": payload["provenance"]["build_id"],
            "decision": payload["decision"],
            "n_evidence": len(payload["evidence_pack"]["evidence"]),
            "user_context_filled": payload["user_context_filled"],
            "latency_ms": payload["latency_ms"],
        })
        return payload

    # ------------------------------------------------------------ admin (P4)

    @app.post("/admin/builds")
    def admin_load_build(request: AdminLoadRequest, _: None = Depends(_local_only)) -> dict[str, Any]:
        root = Path(request.build_dir)
        if not root.is_absolute():
            root = REPO_ROOT / root
        if not (root / "manifest.json").is_file() and not (root / "LATEST").is_file():
            raise HTTPException(status_code=400, detail=f"{root} is neither a build directory nor a LATEST pointer directory")
        return pool.start_load(key=root.resolve().name, root=root)

    @app.get("/admin/builds")
    def admin_list_builds(_: None = Depends(_local_only)) -> dict[str, Any]:
        return pool.status()

    @app.post("/admin/builds/{key}/smoke")
    def admin_smoke(key: str, request: AdminSmokeRequest, _: None = Depends(_local_only)) -> dict[str, Any]:
        questions = request.questions or [question for question, _ in SMOKE_QUESTIONS]
        return pool.smoke(key, questions)

    @app.post("/admin/switch")
    def admin_switch(request: AdminSwitchRequest, _: None = Depends(_local_only)) -> dict[str, Any]:
        return pool.switch(request.key)

    @app.post("/admin/rollback")
    def admin_rollback(_: None = Depends(_local_only)) -> dict[str, Any]:
        return pool.rollback()

    @app.get("/admin/rejected")
    def admin_rejected(limit: int = 50, _: None = Depends(_local_only)) -> dict[str, Any]:
        """Rejected-material queue (P5/P7): the default build's rejected.jsonl tail."""
        engine = pool.get(None)
        path = Path(engine.corpus.root) / "rejected.jsonl"
        rows = list(read_jsonl(path)) if path.is_file() else []
        return {"build_id": engine.corpus.build_id, "n": len(rows), "rows": rows[-max(1, min(limit, 500)):]}

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="runtime.service", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--build", type=Path, default=None, help="build directory or LATEST pointer directory (default: data/corpus/LATEST)")
    parser.add_argument("--config", default=os.environ.get("ENGINE_CONFIG", DEFAULT_CONFIG))
    parser.add_argument("--rerank-model", default=os.environ.get("ENGINE_RERANK_MODEL", "bge-base"))
    parser.add_argument("--device", default=os.environ.get("ENGINE_DEVICE", ""))
    parser.add_argument("--dense-model", default=os.environ.get("ENGINE_DENSE_MODEL", ""), help="dense model path override (ablation: bge-small-en)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("ENGINE_PORT", "8771")))
    parser.add_argument("--log", type=Path, default=REPO_ROOT / "data" / "service" / "answer_context.jsonl")
    args = parser.parse_args(argv)

    import uvicorn

    settings = ServiceSettings(
        build_root=args.build,
        config=args.config,
        rerank_model=args.rerank_model,
        device=args.device,
        dense_model=args.dense_model or DENSE_MODEL,
        log_path=args.log,
    )
    uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
