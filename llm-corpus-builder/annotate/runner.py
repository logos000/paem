"""LLM runners for the annotation pilot: ``ApiRunner`` (OpenAI-compatible chat
completions, e.g. deepseek-flash) and ``FakeRunner`` (tests, no network).

真实调用约定：temperature=0；不依赖 ``response_format``（prompt 强制纯 JSON +
容错提取首个平衡的 JSON 块）；失败重试 3 次指数退避；超时用 ``.env`` 的
``LLM_TIMEOUT_SECONDS``。测试一律用 :class:`FakeRunner`，绝不真实调用 API。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable

import httpx

__all__ = ["ApiRunner", "FakeRunner", "extract_json", "load_env", "parse_env_file"]

_ENV_KEYS = ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "LLM_TIMEOUT_SECONDS")


# --------------------------------------------------------------------- env parsing


def parse_env_file(path: str | Path) -> dict[str, str]:
    """Minimal .env parser: utf-8-sig, KEY=VALUE, '#' comments, strip quotes."""
    values: dict[str, str] = {}
    with open(path, encoding="utf-8-sig") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key:
                values[key] = value
    return values


def load_env(env_path: str | Path | None = None) -> dict[str, str]:
    """Merge .env file with real environment variables (environment wins)."""
    config: dict[str, str] = {}
    path = Path(env_path) if env_path else Path(".env")
    if path.exists():
        config.update(parse_env_file(path))
    for key in _ENV_KEYS:
        if os.environ.get(key):
            config[key] = os.environ[key]
    return config


# --------------------------------------------------------------------- JSON extraction


def _first_balanced(text: str, opener: str, closer: str) -> str | None:
    start = text.find(opener)
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            ch = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    return text[start:index + 1]
        start = text.find(opener, start + 1)
    return None


def extract_json(text: str) -> Any:
    """Extract the first balanced JSON object/array from a completion string.

    Tolerates surrounding prose and markdown fences; when an object and an
    array both appear, the one that *starts earliest* in the text wins (so
    ``结果如下：[{"row_ordinal": 1}]`` yields the array, not the inner object).
    Raises ``ValueError`` when nothing parseable is found.
    """
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    candidates: list[tuple[int, Any]] = []
    for opener, closer in (("{", "}"), ("[", "]")):
        block = _first_balanced(cleaned, opener, closer)
        if block:
            try:
                candidates.append((cleaned.find(opener), json.loads(block)))
            except json.JSONDecodeError:
                continue
    if candidates:
        return min(candidates, key=lambda item: item[0])[1]
    raise ValueError("no JSON object/array found in completion")


# --------------------------------------------------------------------- runners


class ApiRunner:
    """OpenAI-compatible /chat/completions runner (real API; used by the pilot)."""

    def __init__(
        self,
        env_path: str | Path | None = None,
        *,
        client: httpx.Client | None = None,
        model: str | None = None,
        max_retries: int = 3,
        retry_delay: float = 0.5,
    ) -> None:
        config = load_env(env_path)
        # Campus gateway only (user ruling 2026-10-16); never fall back to the
        # official DeepSeek API — that billed the user's account silently.
        self.base_url = (config.get("LLM_BASE_URL") or "https://your-gateway.example.com/v1").rstrip("/")
        self.model = model or config.get("LLM_MODEL") or "deepseek-flash"
        self.api_key = config.get("LLM_API_KEY") or ""
        try:
            self.timeout = float(config.get("LLM_TIMEOUT_SECONDS") or 60)
        except (TypeError, ValueError):
            self.timeout = 60.0
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.call_count = 0
        self._client = client or httpx.Client(timeout=self.timeout)

    def close(self) -> None:
        self._client.close()

    def provenance(self) -> dict[str, Any]:
        """Reproducibility snapshot recorded into every annotation (P1).

        The model alias alone does not pin a run: the endpoint and sampling
        configuration can change behavior just as much as the prompt.
        """
        return {
            "model": self.model,
            "base_url": self.base_url,
            "sampling": {"temperature": 0, "max_tokens": 2000, "thinking": "disabled", "stream": False},
        }

    # -- low level ---------------------------------------------------------

    def _request(self, messages: list[dict[str, str]], max_tokens: int) -> Any:
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
            "stream": False,
        }
        # api.deepseek.com's flash is a reasoning model: without this it spends
        # the whole budget on reasoning_content and returns empty content.
        # The campus gateway (your-gateway.example.com) rejects the field with
        # a 500, so attach it only where it belongs (2026-10-15).
        if "deepseek.com" in self.base_url:
            body["thinking"] = {"type": "disabled"}
        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        last_error: Exception | None = None
        delay = self.retry_delay
        for attempt in range(1, self.max_retries + 1):
            retryable = True
            try:
                self.call_count += 1
                response = self._client.post(url, json=body, headers=headers)
                if response.status_code >= 400:
                    retryable = response.status_code == 429 or response.status_code >= 500
                    response.raise_for_status()
                payload = response.json()
                content = payload["choices"][0]["message"]["content"]
                return extract_json(content)
            except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
                if not retryable:
                    raise
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(delay)
                    delay *= 2
        raise RuntimeError(
            f"LLM request failed after {self.max_retries} attempts ({self.model}): {last_error}"
        ) from last_error

    # -- public API --------------------------------------------------------

    def complete_json(self, messages: list[dict[str, str]], max_tokens: int = 2000) -> dict:
        """Complete and return the first JSON object as a dict."""
        data = self._request(messages, max_tokens)
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            return data[0] if data and isinstance(data[0], dict) else {}
        return {}

    def complete_json_array(self, messages: list[dict[str, str]], max_tokens: int = 2000) -> list:
        """Complete and return a JSON array (falls back to the first list value)."""
        data = self._request(messages, max_tokens)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for value in data.values():
                if isinstance(value, list):
                    return value
        return []


class FakeRunner:
    """Offline test double. Preset responses via a queue or a ``responder(payload)``."""

    def __init__(
        self,
        responses: list[Any] | None = None,
        responder: Callable[[str], Any] | None = None,
        *,
        model: str = "fake-runner",
    ) -> None:
        self._responses = list(responses or [])
        self._responder = responder
        self.model = model
        self.calls: list[list[dict[str, str]]] = []
        self.call_count = 0

    def _next(self, messages: list[dict[str, str]]) -> Any:
        self.calls.append(messages)
        self.call_count += 1
        payload = messages[-1]["content"] if messages else ""
        if self._responder is not None:
            return self._responder(payload)
        if self._responses:
            return self._responses.pop(0)
        raise AssertionError("FakeRunner has no preset response left")

    def provenance(self) -> dict[str, Any]:
        return {"model": self.model, "base_url": "fake://offline", "sampling": {}}

    def complete_json(self, messages: list[dict[str, str]], max_tokens: int = 2000) -> dict:
        value = self._next(messages)
        if isinstance(value, dict):
            return value
        if isinstance(value, list):
            return value[0] if value and isinstance(value[0], dict) else {}
        raise AssertionError("FakeRunner preset must be a dict or list of dicts")

    def complete_json_array(self, messages: list[dict[str, str]], max_tokens: int = 2000) -> list:
        value = self._next(messages)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            for item in value.values():
                if isinstance(item, list):
                    return item
        return []
