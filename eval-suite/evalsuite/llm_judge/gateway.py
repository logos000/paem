"""Minimal OpenAI-compatible chat client for the judge (streaming, multi-turn).

Credentials come from the suite .env (QUESTIONGEN_* keys — the campus gateway
the user designated; LLMJUDGE_MODEL overrides the model for judging).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from evalsuite import SUITE_ROOT

from dotenv import load_dotenv

load_dotenv(SUITE_ROOT / ".env")


class Gateway:
    def __init__(self, *, base_url: str | None = None, api_key: str | None = None, model: str | None = None,
                 timeout: float = 120.0):
        self.base_url = (base_url or os.getenv("QUESTIONGEN_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.getenv("QUESTIONGEN_API_KEY", "")
        self.model = model or os.getenv("LLMJUDGE_MODEL") or os.getenv("QUESTIONGEN_MODEL", "")
        self.timeout = timeout
        if not (self.base_url and self.model):
            raise RuntimeError("judge gateway not configured (QUESTIONGEN_BASE_URL/MODEL in .env)")

    def chat(self, messages: list[dict[str, str]], *, max_tokens: int = 2000, temperature: float = 0.0,
             retries: int = 3) -> str:
        import httpx

        last: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                chunks: list[str] = []
                with httpx.Client(timeout=httpx.Timeout(30, read=self.timeout), trust_env=False) as client:
                    with client.stream(
                        "POST",
                        f"{self.base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        json={"model": self.model, "temperature": temperature, "max_tokens": max_tokens,
                              "stream": True, "messages": messages},
                    ) as response:
                        response.raise_for_status()
                        for line in response.iter_lines():
                            if not line.startswith("data: "):
                                continue
                            data = line[len("data: "):].strip()
                            if data == "[DONE]":
                                break
                            try:
                                delta = json.loads(data)["choices"][0].get("delta", {}).get("content")
                            except (ValueError, KeyError, IndexError):
                                continue
                            if delta:
                                chunks.append(delta)
                return "".join(chunks)
            except Exception as error:
                last = error
                time.sleep(1.5 * attempt)
        raise RuntimeError(f"judge gateway failed after {retries} retries: {last}")


def extract_json(text: str) -> Any:
    """First balanced JSON object/array in a completion (fence/prose tolerant).

    Braces inside quoted strings are ignored.  The old depth counter treated a
    rationale such as ``"value contains {x}"`` as structural JSON and could
    discard an otherwise valid response.
    """
    cleaned = (text or "").strip()
    fence = cleaned.find("```")
    if fence != -1:
        inner = cleaned[fence + 3:]
        if inner.startswith("json"):
            inner = inner[4:]
        end = inner.rfind("```")
        cleaned = inner[:end] if end != -1 else inner
    for start, char in enumerate(cleaned):
        if char not in "[{":
            continue
        stack = []
        in_string = False
        escaped = False
        pairs = {"{": "}", "[": "]"}
        for index in range(start, len(cleaned)):
            current = cleaned[index]
            if in_string:
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    in_string = False
                continue
            if current == '"':
                in_string = True
                continue
            if current in pairs:
                stack.append(pairs[current])
            elif current in ("}", "]"):
                if not stack or current != stack.pop():
                    break
                if not stack:
                    try:
                        return json.loads(cleaned[start:index + 1])
                    except ValueError:
                        break
    raise ValueError(f"no parseable JSON in: {text[:200]!r}")
