"""Thin async client for the corpus retrieval runtime's HTTP contract.

This is the ONLY retrieval-related code in the product: no BM25, no Query IR,
no evidence logic.  Everything arrives as the versioned answer-context
payload (llm-corpus-builder runtime/answer_context.schema.json) and is
validated by ``product.contracts`` before use.
"""

from __future__ import annotations

from typing import Any

import httpx


class CorpusRuntimeError(RuntimeError):
    """The runtime answered with an error status or is unreachable."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class CorpusRuntimeClient:
    def __init__(self, base_url: str, timeout: float = 90.0, transport: httpx.AsyncBaseTransport | None = None):
        # trust_env=False: the runtime is always localhost on this deployment;
        # a shell-level HTTP_PROXY must never intercept it (seen as 502 from a
        # local proxy when the operator exports proxy vars).
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport, trust_env=False)

    async def health(self) -> dict[str, Any]:
        try:
            response = await self._client.get("/health")
        except httpx.HTTPError as error:
            raise CorpusRuntimeError(f"corpus runtime unreachable: {error}") from error
        if response.status_code != 200:
            raise CorpusRuntimeError(f"corpus runtime /health -> {response.status_code}: {response.text[:200]}", response.status_code)
        return response.json()

    async def answer_context(
        self,
        *,
        question: str,
        config: str,
        today: str | None = None,
        user_context: dict[str, Any] | None = None,
        k: int | None = None,
        pack_limit: int | None = None,
        request_id: str | None = None,
        retrieval_queries: list[str] | None = None,
    ) -> Any:
        body: dict[str, Any] = {"question": question, "config": config}
        if today:
            body["today"] = today
        if user_context:
            body["user_context"] = user_context
        if k:
            body["k"] = k
        if pack_limit:
            body["pack_limit"] = pack_limit
        if retrieval_queries:
            body["retrieval_queries"] = retrieval_queries
        headers = {"X-Request-ID": request_id} if request_id else {}
        try:
            response = await self._client.post("/v1/answer_context", json=body, headers=headers)
        except httpx.HTTPError as error:
            raise CorpusRuntimeError(f"corpus runtime unreachable: {error}") from error
        if response.status_code != 200:
            raise CorpusRuntimeError(f"corpus runtime -> {response.status_code}: {response.text[:200]}", response.status_code)
        return response.json()

    async def aclose(self) -> None:
        await self._client.aclose()
