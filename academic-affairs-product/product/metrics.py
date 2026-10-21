"""In-process metrics (P2): request counters, latency percentiles, build tag.

Deliberately dependency-free (no prometheus client): a JSON snapshot at
``GET /metrics`` now; a Prometheus text exporter can render from the same
counters later.  Latencies live in bounded ring buffers.
"""

from __future__ import annotations

import math
import threading
import time
from collections import Counter, deque
from typing import Any


class Metrics:
    def __init__(self, window: int = 2048):
        self._lock = threading.Lock()
        self.started_at = time.time()
        self.counters: Counter[str] = Counter()
        self.latencies: deque[float] = deque(maxlen=window)
        self.stage_latencies: dict[str, deque[float]] = {
            "retrieval_ms": deque(maxlen=window),
            "builder_ms": deque(maxlen=window),
            "llm_ms": deque(maxlen=window),
        }
        self.current_build: str = ""
        self.current_arm: str = ""

    def inc(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self.counters[name] += amount

    def observe(self, total_ms: float, stages: dict[str, float] | None = None) -> None:
        with self._lock:
            self.latencies.append(total_ms)
            for key, value in (stages or {}).items():
                if key in self.stage_latencies:
                    self.stage_latencies[key].append(value)

    def set_build(self, build_id: str, arm: str) -> None:
        with self._lock:
            self.current_build = build_id
            self.current_arm = arm

    @staticmethod
    def _percentile(values: list[float], q: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        return round(ordered[min(len(ordered) - 1, math.floor(q * len(ordered)))], 1)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            latencies = list(self.latencies)
            stages = {key: list(values) for key, values in self.stage_latencies.items()}
            counters = dict(self.counters)
        return {
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "counters": counters,
            "requests": counters.get("ask_total", 0),
            "errors": counters.get("ask_error", 0),
            "latency_ms": {
                "total": {"p50": self._percentile(latencies, 0.5), "p95": self._percentile(latencies, 0.95), "p99": self._percentile(latencies, 0.99)},
                **{key: {"p95": self._percentile(values, 0.95)} for key, values in stages.items()},
            },
            "current_build": self.current_build,
            "current_arm": self.current_arm,
        }


METRICS = Metrics()
