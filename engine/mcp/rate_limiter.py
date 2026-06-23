"""Per-principal token-bucket rate limiter.

Prevents an assistant (or a misbehaving client) from driving the engine too
hard. Each authenticated principal gets an independent bucket refilled at
``rate_limit_per_minute`` tokens with a burst ceiling of ``rate_limit_burst``.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from engine.mcp.config import mcp_settings
from engine.mcp.errors import RateLimitError


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


class RateLimiter:
    """In-memory sliding token-bucket rate limiter keyed by principal."""

    def __init__(
        self,
        per_minute: int | None = None,
        burst: int | None = None,
    ) -> None:
        self._per_minute = per_minute if per_minute is not None else mcp_settings.rate_limit_per_minute
        self._burst = burst if burst is not None else mcp_settings.rate_limit_burst
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def _refill(self, bucket: _Bucket, now: float) -> None:
        elapsed = now - bucket.last_refill
        refill = elapsed * (self._per_minute / 60.0)
        bucket.tokens = min(float(self._burst), bucket.tokens + refill)
        bucket.last_refill = now

    def check(self, key: str) -> None:
        """Raise :class:`RateLimitError` if ``key`` has exceeded its budget."""
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=float(self._burst), last_refill=now)
                self._buckets[key] = bucket
            self._refill(bucket, now)
            if bucket.tokens < 1.0:
                retry_after = (1.0 - bucket.tokens) / (self._per_minute / 60.0)
                raise RateLimitError(
                    "Rate limit exceeded. Please retry shortly.",
                    data={
                        "retry_after_seconds": round(retry_after, 2),
                        "limit_per_minute": self._per_minute,
                    },
                )
            bucket.tokens -= 1.0


__all__ = ["RateLimiter"]
