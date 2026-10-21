# -*- coding: utf-8 -*-
"""OpenAI-compatible /v1/embeddings shim over the LOCAL bge-m3 (2026-10-17).

RAGFlow v1.0's slim image bundles no embedding model; this shim gives it the
SAME bge-m3 every other arm in the horizontal eval uses — same model, same
machine, corpus never leaves localhost.

Usage: ../llm-corpus-builder/.venv/Scripts/python.exe scripts/embeddings_shim.py [port]
RAGFlow (in Docker) reaches it via http://host.docker.internal:<port>/v1
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from evalsuite import builder_root  # noqa: E402

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")  # QUESTIONGEN_API_KEY / LLM_* for the chat proxy

from fastapi import FastAPI
from pydantic import BaseModel
import uvicorn

MODEL_PATH = str(builder_root() / "models" / "embed" / "bge-m3")
app = FastAPI()
_model = None


def _encoder():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(MODEL_PATH)
    return _model


class EmbedRequest(BaseModel):
    model: str = "bge-m3"
    input: list[str] | str


@app.post("/v1/embeddings")
def embeddings(req: EmbedRequest) -> dict:
    texts = [req.input] if isinstance(req.input, str) else req.input
    started = time.perf_counter()
    vecs = _encoder().encode(texts, batch_size=16, normalize_embeddings=True, show_progress_bar=False)
    return {
        "object": "list",
        "model": req.model,
        "data": [{"object": "embedding", "index": i, "embedding": v.tolist()} for i, v in enumerate(vecs)],
        "usage": {"prompt_tokens": 0, "total_tokens": 0},
        "_seconds": round(time.perf_counter() - started, 2),
    }


@app.get("/health")
def health() -> dict:
    return {"ok": True, "model": "bge-m3"}


@app.post("/v1/chat/completions")
async def chat_completions(request: dict):
    """Transparent proxy to the campus LLM gateway — lets RAGFlow's 'default'
    provider instance serve embeddings (local) AND chat (proxied) on one
    base_url (2026-10-17 RAGFlow baseline). Non-streaming."""
    import os

    import httpx
    from fastapi import Response

    base = os.environ.get("LLM_BASE_URL", "https://your-gateway.example.com/v1")
    key = os.environ.get("LLM_API_KEY") or os.environ.get("QUESTIONGEN_API_KEY", "")
    async with httpx.AsyncClient(timeout=600) as client:
        upstream = await client.post(f"{base}/chat/completions", json=request,
                                     headers={"Authorization": f"Bearer {key}"})
    return Response(content=upstream.content, status_code=upstream.status_code,
                    media_type=upstream.headers.get("content-type", "application/json"))


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18081
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
