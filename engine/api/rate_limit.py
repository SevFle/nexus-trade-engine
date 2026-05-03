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
import math
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.responses import Response
    from starlette.types import ASGIApp


_MIN_RETRY_AFTER_SEC = 0.001
# Cap retry_after so we never emit `inf` or astronomically large values
# in 429 responses (RFC 7231 Retry-After is bounded by what's reasonable
# for clients; a one-day ceiling is more than enough).
_MAX_RETRY_AFTER_SEC = 86_400.0
# Per-pod cap on distinct keys we track. Without an upper bound an
# attacker spraying spoofed X-Forwarded-For values would leak memory.
_DEFAULT_MAX_KEYS = 100_000


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
    """Process-local backend with bounded LRU eviction.

    Not safe for multi-pod deployments — each pod has its own bucket
    and the effective global limit is `per_minute * pod_count`. Use a
    Valkey-backed backend (follow-up) when running multi-pod.
    """

    def __init__(self, max_keys: int = _DEFAULT_MAX_KEYS) -> None:
        # OrderedDict gives O(1) LRU semantics via move_to_end + popitem.
        self._state: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._max_keys = max_keys
        self._lock = asyncio.Lock()

    async def update(
        self, key: str, capacity: int, refill_per_sec: float, now: float
    ) -> tuple[bool, int, float]:
        async with self._lock:
            existing = self._state.get(key)
            tokens, last = existing if existing is not None else (float(capacity), now)
            elapsed = max(0.0, now - last)
            tokens = min(float(capacity), tokens + elapsed * refill_per_sec)
            if tokens >= 1.0:
                tokens -= 1.0
                self._state[key] = (tokens, now)
                self._state.move_to_end(key)
                self._evict_if_needed()
                return (True, int(tokens), 0.0)
            if refill_per_sec > 0:
                deficit = 1.0 - tokens
                retry = max(_MIN_RETRY_AFTER_SEC, deficit / refill_per_sec)
                retry = min(retry, _MAX_RETRY_AFTER_SEC)
            else:
                retry = _MAX_RETRY_AFTER_SEC
            self._state[key] = (tokens, now)
            self._state.move_to_end(key)
            self._evict_if_needed()
            return (False, 0, retry)

    def _evict_if_needed(self) -> None:
        while len(self._state) > self._max_keys:
            self._state.popitem(last=False)


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


@dataclass(frozen=True)
class RateLimitConfig:
    """Default + per-route rate limit knobs.

    ``trusted_proxy_depth`` selects the X-Forwarded-For element to trust.
    0 (default) = ignore XFF entirely and key on the ASGI client tuple.
    1 = trust one upstream proxy and key on the rightmost XFF entry.
    Set to your proxy chain depth so a client cannot spoof its own IP.
    """

    default_per_minute: int = 60
    default_burst: int = 30
    exempt_paths: tuple[str, ...] = field(default_factory=tuple)
    overrides: dict[str, tuple[int, int]] = field(default_factory=dict)
    trusted_proxy_depth: int = 0
    expose_headers: bool = False

    def for_path(self, path: str) -> tuple[int, int] | None:
        # Prefix-match exempts so /health/live and /metrics/scrape are
        # also bypassed when /health and /metrics are listed.
        for exempt in self.exempt_paths:
            if path == exempt or path.startswith(exempt.rstrip("/") + "/"):
                return None
        for prefix, limits in self.overrides.items():
            if path.startswith(prefix):
                return limits
        return (self.default_per_minute, self.default_burst)


class RateLimitMiddleware:
    """ASGI middleware that fronts every HTTP request with a TokenBucket."""

    # Methods that should never count against the bucket. CORS preflight
    # OPTIONS in particular: a browser fires one before each cross-origin
    # request, so counting them halves the effective limit for legit
    # JS clients.
    EXEMPT_METHODS = frozenset({"OPTIONS", "HEAD"})

    def __init__(
        self,
        app: ASGIApp,
        config: RateLimitConfig,
        backend: BucketBackend | None = None,
        key_func: Callable[[Any], str] | None = None,
    ) -> None:
        self.app = app
        self.config = config
        self.backend = backend or InMemoryBucketBackend()
        self._key_func = key_func or self._default_key

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "")
        if method in self.EXEMPT_METHODS:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        limits = self.config.for_path(path)
        if limits is None:
            await self.app(scope, receive, send)
            return
        per_minute, burst = limits

        key = self._key_func(scope)
        bucket = TokenBucket(
            backend=self.backend,
            capacity=burst,
            refill_per_sec=per_minute / 60.0,
        )
        ok, remaining, retry_after = await bucket.consume(key)

        async def send_wrapper(message: Any) -> None:
            if self.config.expose_headers and message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-ratelimit-limit", str(burst).encode("latin-1")))
                headers.append((b"x-ratelimit-remaining", str(remaining).encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        if not ok:
            # JSONResponse already carries Retry-After + X-RateLimit-* —
            # always emit those on the 429 path so clients can back off.
            response = self._build_429(burst, remaining, retry_after)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send_wrapper)

    def _default_key(self, scope: Any) -> str:
        depth = self.config.trusted_proxy_depth
        if depth > 0:
            for raw_name, raw_value in scope.get("headers", []):
                if raw_name == b"x-forwarded-for":
                    parts = [
                        p.strip().decode("latin-1") for p in raw_value.split(b",") if p.strip()
                    ]
                    # Trust the rightmost N hops — the leftmost element
                    # is client-controlled and trivially spoofable.
                    if len(parts) >= depth:
                        return f"ip:{parts[-depth]}"
        client = scope.get("client")
        if client and isinstance(client, tuple):
            return f"ip:{client[0]}"
        return "ip:unknown"

    @staticmethod
    def _build_429(burst: int, remaining: int, retry_after: float) -> Response:
        retry_after_clamped = (
            _MAX_RETRY_AFTER_SEC
            if math.isinf(retry_after) or retry_after > _MAX_RETRY_AFTER_SEC
            else retry_after
        )
        retry_after_int = max(1, int(retry_after_clamped + 0.999))
        return JSONResponse(
            status_code=429,
            content={
                "error": "rate_limit_exceeded",
                "retry_after": retry_after_int,
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
    "RateLimitMiddleware",
    "TokenBucket",
]
