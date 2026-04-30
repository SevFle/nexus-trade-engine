"""Token-bucket rate limiter as ASGI middleware.

Two-layer design:

- :class:`TokenBucket` — pure algorithm; pluggable backend.
- :class:`RateLimitMiddleware` — extracts the per-request key
  (X-Forwarded-For → client.host fallback), routes to the bucket, emits
  429 with ``Retry-After`` + ``X-RateLimit-*`` headers when blocked.

PR1 ships an in-memory backend suitable for single-pod deployments and
tests. Multi-pod deployments will plug a Valkey-backed atomic refill
backend in a follow-up.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from starlette.responses import Response
    from starlette.types import ASGIApp


_MIN_RETRY_AFTER_SEC = 0.001


def _monotonic() -> float:
    """Indirection so tests can monkeypatch the clock."""
    return time.monotonic()


class BucketBackend(Protocol):
    """Atomic per-key state container for the token bucket."""

    async def update(
        self, key: str, capacity: int, refill_per_sec: float, now: float
    ) -> tuple[bool, int, float]:
        """Refill, attempt-to-consume, return (ok, remaining, retry_after)."""
        ...


class InMemoryBucketBackend:
    """Process-local backend. Not safe for multi-pod deployments."""

    def __init__(self) -> None:
        self._state: dict[str, tuple[float, float]] = {}
        self._lock = asyncio.Lock()

    async def update(
        self, key: str, capacity: int, refill_per_sec: float, now: float
    ) -> tuple[bool, int, float]:
        async with self._lock:
            tokens, last = self._state.get(key, (float(capacity), now))
            elapsed = max(0.0, now - last)
            tokens = min(float(capacity), tokens + elapsed * refill_per_sec)
            if tokens >= 1.0:
                tokens -= 1.0
                self._state[key] = (tokens, now)
                return (True, int(tokens), 0.0)
            if refill_per_sec > 0:
                deficit = 1.0 - tokens
                retry = max(_MIN_RETRY_AFTER_SEC, deficit / refill_per_sec)
            else:
                retry = float("inf")
            self._state[key] = (tokens, now)
            return (False, 0, retry)


@dataclass
class TokenBucket:
    """A token-bucket consumer over an arbitrary backend."""

    backend: BucketBackend
    capacity: int
    refill_per_sec: float

    async def consume(self, key: str) -> tuple[bool, int, float]:
        """Try to consume one token. Returns (ok, remaining, retry_after_sec)."""
        return await self.backend.update(
            key=key,
            capacity=self.capacity,
            refill_per_sec=self.refill_per_sec,
            now=_monotonic(),
        )


class RateLimitExceededError(Exception):
    def __init__(self, *, retry_after: float, limit: int, remaining: int) -> None:
        self.retry_after = retry_after
        self.limit = limit
        self.remaining = remaining
        super().__init__(
            f"rate limit exceeded: retry after {retry_after:.2f}s"
        )


@dataclass(frozen=True)
class RateLimitConfig:
    """Default + per-route rate limit knobs."""

    default_per_minute: int = 60
    default_burst: int = 30
    exempt_paths: tuple[str, ...] = field(default_factory=tuple)
    overrides: dict[str, tuple[int, int]] = field(default_factory=dict)

    def for_path(self, path: str) -> tuple[int, int] | None:
        if path in self.exempt_paths:
            return None
        for prefix, limits in self.overrides.items():
            if path.startswith(prefix):
                return limits
        return (self.default_per_minute, self.default_burst)


class RateLimitMiddleware:
    """ASGI middleware that fronts every HTTP request with a TokenBucket."""

    def __init__(
        self,
        app: ASGIApp,
        config: RateLimitConfig,
        backend: BucketBackend | None = None,
    ) -> None:
        self.app = app
        self.config = config
        self.backend = backend or InMemoryBucketBackend()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        limits = self.config.for_path(path)
        if limits is None:
            await self.app(scope, receive, send)
            return
        per_minute, burst = limits

        key = self._client_key(scope)
        bucket = TokenBucket(
            backend=self.backend,
            capacity=burst,
            refill_per_sec=per_minute / 60.0,
        )
        ok, remaining, retry_after = await bucket.consume(key)

        async def send_wrapper(message: Any) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append(
                    (b"x-ratelimit-limit", str(burst).encode("latin-1"))
                )
                headers.append(
                    (b"x-ratelimit-remaining", str(remaining).encode("latin-1"))
                )
                message = {**message, "headers": headers}
            await send(message)

        if not ok:
            # JSONResponse already carries Retry-After + X-RateLimit-* —
            # send it directly so headers don't get duplicated.
            response = self._build_429(burst, remaining, retry_after)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send_wrapper)

    @staticmethod
    def _client_key(scope: Any) -> str:
        for raw_name, raw_value in scope.get("headers", []):
            if raw_name == b"x-forwarded-for":
                first = raw_value.split(b",")[0].strip().decode("latin-1")
                if first:
                    return f"ip:{first}"
        client = scope.get("client")
        if client and isinstance(client, tuple):
            return f"ip:{client[0]}"
        return "ip:unknown"

    @staticmethod
    def _build_429(burst: int, remaining: int, retry_after: float) -> Response:
        retry_after_int = max(1, int(retry_after + 0.999))
        return JSONResponse(
            status_code=429,
            content={
                "error": "rate_limit_exceeded",
                "retry_after": round(retry_after, 3),
            },
            headers={
                "Retry-After": str(retry_after_int),
                "X-RateLimit-Limit": str(burst),
                "X-RateLimit-Remaining": str(remaining),
            },
        )


__all__ = [
    "BucketBackend",
    "InMemoryBucketBackend",
    "RateLimitConfig",
    "RateLimitExceededError",
    "RateLimitMiddleware",
    "TokenBucket",
]
