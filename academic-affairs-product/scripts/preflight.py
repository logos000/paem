"""Startup preflight (P0): validate configuration and the runtime dependency.

Exits non-zero with ONE categorized reason on failure:
    [config] invalid product environment
    [build]  runtime unreachable or build directory missing
    [index]  runtime index/sidecar problems
    [model]  runtime model loading problems

Usage: python scripts/preflight.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from product.config import ConfigError, classify_runtime_error, llm_enabled, load_config
from product.corpus_client import CorpusRuntimeClient, CorpusRuntimeError


async def main() -> int:
    try:
        config = load_config()
    except ConfigError as error:
        print(f"FAIL {error}")
        return 1
    print(f"[config] ok: arm={config.arm} top_k={config.top_k} pack_limit={config.pack_limit} "
          f"llm={'enabled:' + config.llm_model if llm_enabled(config) else 'disabled (template mode)'}")
    client = CorpusRuntimeClient(config.builder_url, timeout=config.request_timeout)
    try:
        health = await client.health()
    except CorpusRuntimeError as error:
        print(f"FAIL [{classify_runtime_error(str(error))}] runtime at {config.builder_url}: {error}")
        return 1
    finally:
        await client.aclose()
    if health.get("config") not in (config.arm,):
        print(f"WARN runtime default arm is {health.get('config')!r}; the product pins {config.arm!r} per request (usually fine, check BUILDER_URL if unexpected)")
    print(f"[build] ok: build_id={health.get('build_id')} units={health.get('n_units')} docs={health.get('n_documents')} runtime_version={health.get('version')}")
    print("PREFLIGHT OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
