"""Product configuration (P0: explicit, validated, auditable).

Everything the product needs to run comes from one validated RuntimeConfig —
no implicit runtime defaults.  Which builder arm, which build strategy, which
LLM provider and every timeout are pinned here; startup fails with a
categorized reason (build / index / model / config) instead of a stack trace.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The only arms the product may serve.  ``active_ir`` is the deterministic
#: arm (works on any build incl. SharePoint-only without LLM annotations).
#: ``active_llm_validated_ir`` and ``_rr`` require llm_annotations.jsonl.
ALLOWED_ARMS = ("active_ir", "active_hybrid", "active_llm_validated_ir", "active_llm_validated_ir_rr")

#: Benchmark/dev extension (P6): extra runtime configs the product may be
#: asked to serve per request — e.g. PRODUCT_EXTRA_ARMS=active_ir,active_hybrid
#: for the four-arm product benchmark.  Never enabled in production .env.
EXTRA_ARMS = tuple(item.strip() for item in os.getenv("PRODUCT_EXTRA_ARMS", "").split(",") if item.strip())

#: Which arm a display name maps to (config matrix documentation).
ARM_MATRIX = {
    "light": "active_llm_validated_ir",        # BM25 + Query IR, no online models
    "sp": "active_hybrid",                      # BM25+IR fused with dense (bge-m3) — SP-domain product arm since 2026-10-16
    "sp_lexical": "active_ir",                  # rollback: pure BM25+IR
    "quality": "active_llm_validated_ir_rr",   # + bge reranker (needs the local model)
}


def allowed_arms() -> tuple[str, ...]:
    return ALLOWED_ARMS + tuple(arm for arm in EXTRA_ARMS if arm not in ALLOWED_ARMS)


class ConfigError(RuntimeError):
    """Invalid configuration; ``kind`` is one of build/index/model/config."""

    def __init__(self, kind: str, message: str):
        super().__init__(f"[{kind}] {message}")
        self.kind = kind


def load_dotenv_once() -> None:
    load_dotenv(REPO_ROOT / ".env")


load_dotenv_once()


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class RuntimeConfig:
    builder_url: str
    arm: str
    top_k: int
    pack_limit: int
    request_timeout: float
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_timeout: float
    ask_log_path: Path
    # Latency hardening (streaming): httpx's read timeout bounds only the gap
    # between chunks, so the stream additionally gets a total wall-clock
    # deadline, and generation output gets a max_tokens cap.
    llm_stream_idle_seconds: float = 30.0
    llm_stream_total_seconds: float = 120.0
    llm_max_tokens: int = 1500
    # Backend-specific request extras merged into every LLM call body —
    # e.g. LLM_EXTRA_BODY={"think": false} for Ollama hybrid-reasoning models
    # or {"chat_template_kwargs": {"enable_thinking": false}} for llama.cpp.
    llm_extra_body: dict[str, Any] = field(default_factory=dict)
    # Explicit feature flags for the capabilities the 2026-10-16 convergence
    # moved off the default request path (one retrieval → one immutable pack
    # → single generation).  follow_up_retrieval is OFF by default: it fired
    # on 98/153 product requests in the editorial-v1-final run — a multi-round
    # state branch on most requests, exactly what the convergence removed
    # (flip with FEATURE_FOLLOW_UP_RETRIEVAL=1 / the eval runner for A/B).
    # query_optimization is OFF (user ruling 2026-10-16, after the six-arm
    # ablation on the 275-question superset): generalized variants pulled
    # same-series docs of OTHER terms into the pack (108 vs 78 questions
    # carried version-conflict markers), the conflict prompt then made the
    # generator decline more (97 vs 72 refusal dispositions; zh 56%→64%,
    # en 64%→69% good with it off).  bge-m3 dense retrieval covers the
    # cross-lingual bridge the variants existed for.  FEATURE_QUERY_OPTIMIZATION=1
    # restores it for A/B.
    query_optimization: bool = False
    # Table provenance line in the evidence prompt (ablation switch).
    table_context: bool = True
    follow_up_retrieval: bool = False


def load_config() -> RuntimeConfig:
    """Read + validate the environment; raise ConfigError(kind, ...) on any fault."""
    arm_name = _env("PRODUCT_ARM", "light")
    arm = ARM_MATRIX.get(arm_name, arm_name)
    if arm not in allowed_arms():  # EXTRA_ARMS widens startup too (eval-only
        # arms like active_dense; the display matrix stays the prod surface)
        raise ConfigError(
            "config",
            f"PRODUCT_ARM={arm_name!r} is not allowed; use one of {sorted(ARM_MATRIX)} "
            f"or an explicit arm in {allowed_arms()}",
        )
    builder_url = _env("BUILDER_URL", "http://127.0.0.1:8772").rstrip("/")
    if not builder_url.startswith(("http://", "https://")):
        raise ConfigError("config", f"BUILDER_URL must be http(s), got {builder_url!r}")

    def _bounded(name: str, default: float, low: float, high: float) -> float:
        raw = _env(name)
        try:
            value = float(raw) if raw else default
        except ValueError as error:
            raise ConfigError("config", f"{name}={raw!r} is not a number") from error
        if not low <= value <= high:
            raise ConfigError("config", f"{name}={value} outside [{low}, {high}]")
        return value

    def _int(name: str, default: int, low: int, high: int) -> int:
        return int(_bounded(name, default, low, high))

    def _extra_body(name: str) -> dict[str, Any]:
        raw = _env(name)
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except ValueError as error:
            raise ConfigError("config", f"{name} must be a JSON object, got {raw!r}") from error
        if not isinstance(value, dict):
            raise ConfigError("config", f"{name} must be a JSON object, got {type(value).__name__}")
        return value

    llm_base_url, llm_api_key, llm_model = _env("LLM_BASE_URL"), _env("LLM_API_KEY"), _env("LLM_MODEL")
    if (llm_base_url or llm_model) and not (llm_base_url and llm_api_key and llm_model):
        raise ConfigError("config", "LLM_BASE_URL/LLM_API_KEY/LLM_MODEL must be set together (all or none)")
    if llm_base_url and not llm_base_url.startswith(("http://", "https://")):
        raise ConfigError("config", f"LLM_BASE_URL must be http(s), got {llm_base_url!r}")

    return RuntimeConfig(
        builder_url=builder_url,
        arm=arm,
        top_k=_int("RETRIEVAL_TOP_K", 10, 1, 50),
        pack_limit=_int("PACK_LIMIT", 8, 1, 16),
        request_timeout=_bounded("REQUEST_TIMEOUT", 90, 5, 300),
        llm_base_url=llm_base_url,
        llm_api_key=llm_api_key,
        llm_model=llm_model,
        llm_timeout=_bounded("LLM_TIMEOUT_SECONDS", 120, 5, 300),
        ask_log_path=Path(_env("ASK_LOG", str(REPO_ROOT / "logs" / "ask.jsonl"))),
        llm_stream_idle_seconds=_bounded("LLM_STREAM_IDLE_SECONDS", 30, 2, 120),
        llm_stream_total_seconds=_bounded("LLM_STREAM_TOTAL_SECONDS", 120, 15, 300),
        llm_max_tokens=_int("LLM_MAX_TOKENS", 1500, 0, 8000),
        llm_extra_body=_extra_body("LLM_EXTRA_BODY"),
        query_optimization=_env("FEATURE_QUERY_OPTIMIZATION", "0") not in ("0", "false", "no"),
        table_context=_env("PRODUCT_TABLE_CONTEXT", "1") not in ("0", "false", "no"),
        follow_up_retrieval=_env("FEATURE_FOLLOW_UP_RETRIEVAL", "0") not in ("0", "false", "no"),
    )


def classify_runtime_error(detail: str) -> str:
    """Categorize a runtime /health failure for startup diagnostics."""
    lowered = str(detail).lower()
    if "manifest" in lowered or "not a build" in lowered or "no such file" in lowered or "unreachable" in lowered:
        return "build"
    if "index" in lowered or "annotation" in lowered or "llm_annotations" in lowered:
        return "index"
    if "model" in lowered or "rerank" in lowered or "torch" in lowered:
        return "model"
    return "config"


def llm_enabled(config: RuntimeConfig) -> bool:
    return bool(config.llm_base_url and config.llm_api_key and config.llm_model)
