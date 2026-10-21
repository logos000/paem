"""Smoke test: one end-to-end ask against the running corpus runtime.

Usage (needs the runtime service up — see scripts/launch.ps1):
    python scripts/smoke.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from product.config import load_config
from product.corpus_client import CorpusRuntimeClient


async def main() -> int:
    client = CorpusRuntimeClient(load_config().builder_url)
    try:
        health = await client.health()
        print("runtime:", {key: health.get(key) for key in ("build_id", "config", "n_units", "n_documents")})
        payload = await client.answer_context(
            question="ZJUI 2026 fall undergraduate course withdrawal deadline?",
            user_context={"audience": "undergraduate"},
            request_id="smoke",
        )
        print("decision:", payload["decision"], "| latency_ms:", payload["latency_ms"])
        for item in payload["evidence_pack"]["evidence"][:3]:
            print("-", item["doc_id"], "|", item["title"][:50], "|", item["text"][:60].replace("\n", " "))
        return 0
    finally:
        await client.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
