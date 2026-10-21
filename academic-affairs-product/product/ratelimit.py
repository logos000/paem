"""Rate limiting and capacity guards (P2, pre-auth naive tier).

Token buckets per client IP plus a global concurrency cap; oversized request
bodies are rejected before parsing.  This is the no-auth tier — P3 replaces
per-IP identity with real principals.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque

from contextlib import asynccontextmanager

from fastapi import HTTPException, Request


class TokenBucket:
    def __init__(self, rate_per_minute: float, burst: int):
        self.rate = rate_per_minute / 60.0
        self.burst = burst
        self.tokens = float(burst)
        self.updated = time.monotonic()

    def allow(self) -> bool:
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


class RateLimiter:
    """Per-IP token buckets with lazy eviction; ``max_addresses`` bounds memory."""

    def __init__(self, rate_per_minute: float, burst: int, max_addresses: int = 4096):
        self.rate_per_minute = rate_per_minute
        self.burst = burst
        self.max_addresses = max_addresses
        self._buckets: dict[str, TokenBucket] = {}
        self._order: deque[str] = deque()

    def check(self, key: str) -> bool:
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= self.max_addresses:
                evicted = self._order.popleft()
                self._buckets.pop(evicted, None)
            bucket = TokenBucket(self.rate_per_minute, self.burst)
            self._buckets[key] = bucket
            self._order.append(key)
        return bucket.allow()


def client_address(request: Request) -> str:
    client = request.client
    return client.host if client else "unknown"


def capacity_guard(request: Request, *, max_body_bytes: int, limiter: RateLimiter, semaphore: asyncio.Semaphore):
    """Shared dependency: body-size cap, rate limit, bounded concurrency.

    Returns an async context manager — the concurrency slot is held for the
    request's lifetime and ALWAYS released (yield-dependency semantics).
    """
    if (content_length := request.headers.get("content-length")) and int(content_length) > max_body_bytes:
        raise HTTPException(status_code=413, detail=f"request body exceeds {max_body_bytes} bytes")
    if not limiter.check(client_address(request)):
        raise HTTPException(status_code=429, detail="rate limit exceeded, retry later")

    @asynccontextmanager
    async def _slot():
        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=10.0)
        except TimeoutError:
            raise HTTPException(status_code=503, detail="server at capacity, retry later") from None
        try:
            yield
        finally:
            semaphore.release()

    return _slot()
