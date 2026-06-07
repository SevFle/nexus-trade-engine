"""Token-bucket rate limiter as ASGI middleware.

Two-layer design:

- :class:`TokenBucket` — pure algorithm; pluggable backend.
- :class:`RateLimitMiddleware` — extracts the per-request key
  (authenticated user > client IP), routes to the bucket, emits
  429 with ``Retry-After`` + ``X-RateLimit-*`` headers when blocked.

Keying strategy
---------------
By default the middleware keys buckets on the authenticated principal
when one is recoverable from the request headers — ``user:<sub>`` for
JWT Bearer tokens and ``apikey:<prefix>`` for engine API keys — so
that a single user is throttled coherently across IPs (and, conversely,
two distinct users behind the same NAT are not unfairly squeezed into
one bucket). Unauthenticated requests fall back to ``ip:<addr>`` based
on the ASGI client tuple, optionally honouring ``X-Forwarded-For`` when
``trusted_proxy_depth`` is configured.

JWT decoding is best-effort: any signature/expiry/format failure is
swallowed and the request is keyed by IP, so a malformed token never
prevents the limiter from running.

PR1 ships an in-memory backend suitable for single-pod deployments and
tests. Multi-pod deployments can use :class:`ValkeyBucketBackend`
(see ``engine/api/rate_limit_valkey.py``).
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

try:
    # Guarded so a misconfigured JWT environment (no usable secret, or
    # ``engine.api.auth.jwt`` itself unimportable) can never prevent this
    # module — and therefore the whole ASGI limiter — from loading.
    # ``_jwt_subject`` treats a ``None`` decode_token as "no principal".
    from engine.api.auth.jwt import decode_token
except Exception:
    decode_token = None  # type: ignore[assignment]


_MIN_RETRY_AFTER_SEC = 0.001
# Cap retry_after so we never emit `inf` or astronomically large values
# in 429 responses (RFC 7231 Retry-After is bounded by what's reasonable
# for clients; a one-day ceiling is more than enough).
_MAX_RETRY_AFTER_SEC = 86_400.0
# Per-pod cap on distinct keys we track. Without an upper bound an
# attacker spraying spoofed X-Forwarded-For values would leak memory.
_DEFAULT_MAX_KEYS = 100_000
# A well-formed ``Authorization: Bearer <token>`` header splits into
# exactly two whitespace-separated parts.
_BEARER_TOKEN_PARTS = 2


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

    ``key_strategy`` controls how the bucket key is derived:

    - ``"user_or_ip"`` (default) — use ``user:<sub>`` for authenticated
      requests (JWT or engine API key) and ``ip:<addr>`` otherwise.
      This is the right default for multi-tenant APIs because two users
      behind the same NAT are billed separately and a single user
      moving between networks stays under one quota.
    - ``"ip_only"`` — always key by IP. Useful for public, unauthenticated
      endpoints or when a downstream proxy already enforces per-user
      limits.

    ``unauthenticated_paths`` is an iterable of path prefixes whose
    requests must always be keyed by IP regardless of ``key_strategy``
    (e.g. the login endpoint accepts a JWT in the body but the request
    itself is pre-auth).
    """

    default_per_minute: int = 60
    default_burst: int = 30
    exempt_paths: tuple[str, ...] = field(default_factory=tuple)
    overrides: dict[str, tuple[int, int]] = field(default_factory=dict)
    trusted_proxy_depth: int = 0
    expose_headers: bool = False
    key_strategy: str = "user_or_ip"
    unauthenticated_paths: tuple[str, ...] = field(default_factory=tuple)

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

    def is_unauthenticated_path(self, path: str) -> bool:
        for prefix in self.unauthenticated_paths:
            if path == prefix or path.startswith(prefix.rstrip("/") + "/"):
                return True
        return False


def _extract_bearer_token(scope: Any) -> str | None:
    """Return the Bearer token from the Authorization header, else None."""
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name == b"authorization":
            try:
                value = raw_value.decode("latin-1")
            except UnicodeDecodeError:
                return None
            parts = value.split(None, 1)
            if len(parts) == _BEARER_TOKEN_PARTS and parts[0].lower() == "bearer":
                token = parts[1].strip()
                return token or None
            return None
    return None


def _extract_api_key(scope: Any) -> str | None:
    """Return the X-API-Key header value, else None."""
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name == b"x-api-key":
            try:
                value = raw_value.decode("latin-1").strip()
            except UnicodeDecodeError:
                return None
            return value or None
    return None


def _jwt_subject(token: str) -> str | None:
    """Decode a JWT and return its ``sub`` claim, or None on any error.

    The JWT decoder is imported guardedly at module load (see above) so
    this module stays importable in environments that do not configure a
    JWT secret (e.g. unit tests for the algorithm). Failures (signature
    mismatch, expired token, malformed payload, missing claim, or an
    unimportable/None decoder) all collapse to ``None`` — the caller
    falls back to IP-based keying and the request is still rate-limited,
    never accidentally let through.
    """
    if decode_token is None:
        return None
    try:
        payload = decode_token(token)
    except Exception:
        # decode_token catches PyJWT errors and returns None, but we
        # also defend against e.g. configuration failures (no usable
        # secret_key in the current environment).
        return None
    if not payload:
        return None
    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub:
        return None
    return sub


# Length of the prefix used as the bucket key for engine API keys.
# Matches ``_PREFIX_DISPLAY_CHARS`` in ``engine/api/auth/api_keys.py``;
# duplicated here to avoid an import cycle.
_APIKEY_PREFIX_LEN = 12


def _api_key_prefix(token: str) -> str | None:
    """Return the deterministic prefix of an engine API key.

    Returns ``None`` if the token doesn't look like an engine-issued key
    (``nxs_<env>_<hex>``) — in that case we don't know how to attribute
    the request so it falls back to IP-based keying.
    """
    if not token.startswith("nxs_") or len(token) <= _APIKEY_PREFIX_LEN:
        return None
    return token[:_APIKEY_PREFIX_LEN]


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
            if (
                self.config.expose_headers
                and message["type"] == "http.response.start"
            ):
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
            # always emit those on the 429 path so clients can back off.
            response = self._build_429(burst, remaining, retry_after)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send_wrapper)

    def _default_key(self, scope: Any) -> str:
        # Per-user keying takes precedence — two users behind the same
        # NAT should not share a bucket, and one user moving between
        # networks should stay under a single quota.
        if self.config.key_strategy == "user_or_ip":
            path = scope.get("path", "")
            if not self.config.is_unauthenticated_path(path):
                user_key = self._user_key(scope)
                if user_key is not None:
                    return user_key
        return self._ip_key(scope)

    @staticmethod
    def _user_key(scope: Any) -> str | None:
        """Best-effort extraction of an authenticated-principal key.

        Order: JWT ``sub`` > engine API-key prefix. Returns None when
        no recognisable credential is present, so the caller falls back
        to IP-based keying.
        """
        bearer = _extract_bearer_token(scope)
        if bearer is not None:
            # Engine API keys sometimes arrive as Bearer tokens too;
            # recognise that shape first to avoid a pointless JWT
            # decode attempt.
            if bearer.startswith("nxs_"):
                prefix = _api_key_prefix(bearer)
                if prefix is not None:
                    return f"apikey:{prefix}"
            else:
                sub = _jwt_subject(bearer)
                if sub is not None:
                    return f"user:{sub}"
                # Fall through to X-API-Key check; some clients send a
                # malformed Authorization header alongside a valid
                # X-API-Key and we want the latter to still win.
        api_key = _extract_api_key(scope)
        if api_key is not None:
            prefix = _api_key_prefix(api_key)
            if prefix is not None:
                return f"apikey:{prefix}"
        return None

    def _ip_key(self, scope: Any) -> str:
        depth = self.config.trusted_proxy_depth
        if depth > 0:
            for raw_name, raw_value in scope.get("headers", []):
                if raw_name == b"x-forwarded-for":
                    parts = [
                        p.strip().decode("latin-1")
                        for p in raw_value.split(b",")
                        if p.strip()
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
    "_api_key_prefix",
    "_extract_api_key",
    "_extract_bearer_token",
    "_jwt_subject",
]
