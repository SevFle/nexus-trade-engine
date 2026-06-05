"""Per-connection outbound rate limiter (SEV-275).

Sliding-window counter keyed by ``user_id`` (preferred) or peer IP.
The :class:`OutboundRateLimiter` is async-safe via a single
``asyncio.Lock`` per bucket. Each call to :meth:`acquire` either
returns ``True`` (allow) or raises :class:`RateLimitedError` (deny).

The limits are intentionally *outbound* (frames-per-second the
server sends to one client). Inbound rate limiting is handled by
the HTTP middleware on the upgrade request — once a connection is
open, an attacker cannot push more than ``receive_json`` will
buffer.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass

from engine.api.websocket.constants import (
    DEFAULT_OUTBOUND_BURST,
)
from engine.api.websocket.exceptions import RateLimitedError


@dataclass
class _Bucket:
    """Sliding-window counter."""

    capacity: int
    window_seconds: float
    events: deque[float]

    def acquire(self, now: float) -> bool:
        # Drop events outside the window.
        cutoff = now - self.window_seconds
        while self.events and self.events[0] <= cutoff:
            self.events.popleft()
        if len(self.events) >= self.capacity:
            return False
        self.events.append(now)
        return True


class OutboundRateLimiter:
    """Per-key sliding-window rate limiter.

    ``capacity`` is the maximum number of frames per ``window_seconds``.
    Defaults are tunable per-connection at the route handler's
    discretion; the global defaults are sized for bursts of market
    data fan-out (a thousand ticks/sec is well above what any
    individual client should absorb).
    """

    def __init__(
        self,
        *,
        capacity: int = DEFAULT_OUTBOUND_BURST,
        window_seconds: float = 1.0,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.capacity = capacity
        self.window_seconds = window_seconds
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, key: str) -> bool:
        """Try to record one outbound frame. Returns ``True`` if allowed.

        Raises :class:`RateLimitedError` if the bucket is full so the
        caller's send loop can short-circuit and disconnect the noisy
        client. The boolean form is exposed for tests that want to
        observe the limit without triggering the disconnect path.
        """
        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(
                    capacity=self.capacity,
                    window_seconds=self.window_seconds,
                    events=deque(),
                )
                self._buckets[key] = bucket
            allowed = bucket.acquire(self._now())
        return allowed

    async def require(self, key: str) -> None:
        """Acquire-or-raise. Convenience for the send loop."""
        if not await self.acquire(key):
            raise RateLimitedError(reason="rate_limited")

    def _now(self) -> float:
        """Indirection so tests can patch the clock without monkey-patching ``time``."""
        return time.monotonic()

    async def reset(self, key: str) -> None:
        async with self._lock:
            self._buckets.pop(key, None)


__all__ = ["OutboundRateLimiter"]
