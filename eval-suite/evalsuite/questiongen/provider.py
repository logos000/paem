"""OpenAI-compatible streaming client for question generation, cache-first.

Endpoint comes from .env (QUESTIONGEN_BASE_URL / QUESTIONGEN_API_KEY /
QUESTIONGEN_MODEL — the ZJU campus gateway by user ruling 2026-10-15; switch
to a local Ollama URL anytime, e.g. http://127.0.0.1:11434/v1).  Responses
are cached by (payload, task, model, prompt-version) hash exactly like the
annotation pilot, so reruns are free and resumable.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from evalsuite import SUITE_ROOT, builder_root  # noqa: F401  (bridge: builder on sys.path)

from annotate.cache import cache_key, load, save  # noqa: E402
from annotate.runner import extract_json  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

# gateway credentials live in the SUITE .env (moved out of the builder repo)
load_dotenv(SUITE_ROOT / ".env")

PROMPT_VERSION = "qg-v1"


class GenerationError(RuntimeError):
    pass


class QuestionGenProvider:
    def __init__(self, *, base_url: str | None = None, api_key: str | None = None, model: str | None = None,
                 timeout: float | None = None, cache_dir: str | Path | None = None):
        self.base_url = (base_url or os.getenv("QUESTIONGEN_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.getenv("QUESTIONGEN_API_KEY", "")
        self.model = model or os.getenv("QUESTIONGEN_MODEL", "")
        self.timeout = float(timeout or os.getenv("QUESTIONGEN_TIMEOUT_SECONDS", "120"))
        self.cache_dir = cache_dir
        if not (self.base_url and self.model):
            raise GenerationError("QUESTIONGEN_BASE_URL/MODEL not configured (see .env)")

    def generate_json(self, prompt: str, *, task: str, max_tokens: int = 4000) -> Any:
        """One JSON-array/object completion, disk-cached by prompt hash."""
        key = cache_key(prompt, task, self.model, PROMPT_VERSION, prompt_hash=None)
        cached = load(key, cache_dir=self.cache_dir)
        if cached is not None:
            return cached.get("response", cached)
        text = self._complete(prompt, max_tokens=max_tokens)
        parsed = extract_json(text)
        save(key, parsed, model=self.model, prompt_version=PROMPT_VERSION, cache_dir=self.cache_dir)
        return parsed

    def _complete(self, prompt: str, *, max_tokens: int) -> str:
        import httpx

        chunks: list[str] = []
        body = {"model": self.model, "temperature": 0.2, "max_tokens": max_tokens, "stream": True,
                "messages": [{"role": "user", "content": prompt}]}
        last_error: Exception | None = None
        for attempt in (1, 2, 3):
            try:
                with httpx.Client(timeout=httpx.Timeout(30, read=self.timeout), trust_env=False) as client:
                    with client.stream(
                        "POST",
                        f"{self.base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        json=body,
                    ) as response:
                        response.raise_for_status()
                        for line in response.iter_lines():
                            if not line.startswith("data: "):
                                continue
                            data = line[len("data: "):].strip()
                            if data == "[DONE]":
                                break
                            try:
                                delta = __import__("json").loads(data)["choices"][0].get("delta", {}).get("content")
                            except (ValueError, KeyError, IndexError):
                                continue
                            if delta:
                                chunks.append(delta)
                return "".join(chunks)
            except Exception as error:  # network / 5xx / timeout — retry with backoff
                last_error = error
                chunks.clear()
                import time

                time.sleep(2.0 * attempt)
        raise GenerationError(f"questiongen provider failed after retries: {last_error}")
