"""Token-bucket rate limiter as ASGI middleware.

Three-layer design:

- :class:`BucketBackend` (Protocol) + :class:`InMemoryBucketBackend`
  / :class:`ValkeyBucketBackend` — atomic per-key state container.
- :class:`TokenBucket` — pure algorithm over the backend.
- :class:`RateLimitMiddleware` — extracts the per-request key
  (X-Forwarded-For → client.host fallback, or ``user:<sub>`` when an
  authenticated JWT can be decoded inline), routes to the bucket,
  selects the limits for the request's RBAC role tier, and emits
  429 with ``Retry-After`` + ``X-RateLimit-*`` headers when blocked.

Two backends are shipped:

- :class:`InMemoryBucketBackend` — process-local, single-pod only.
  Safe default; bounded LRU eviction.
- :class:`ValkeyBucketBackend` — distributed, atomic via a single
  ``EVAL`` of a Lua script. Used in multi-pod deployments where the
  effective rate must be enforced globally.

The middleware also honours RBAC role tiers. When the request carries
a Bearer JWT that the middleware can decode with the configured
secret, the bucket key becomes ``user:<sub>`` and the per-tier limits
take precedence over the default. API-key requests are keyed by the
12-char display prefix (``apikey:<prefix>``) and fall back to the
default tier; unauthenticated requests are keyed by IP.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from starlette.responses import JSONResponse

from engine.observability import context as ctx

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
# TTL on per-key state in the distributed backend. After this many
# seconds of inactivity the entry is reaped by Valkey, bounding memory
# growth across the cluster even when the key-space is adversarial.
_DEFAULT_VALKEY_TTL_SEC = 3600
# ``Authorization: <scheme> <token>`` has exactly two whitespace-separated
# parts when the scheme is followed by a non-empty token.
_AUTH_SCHEME_PARTS = 2
# Display-prefix length used by engine API keys (``nxs_<env>_<...>``).
_API_KEY_PREFIX_CHARS = 12


def _monotonic() -> float:
    """Indirection so tests can monkeypatch the clock."""
    return time.monotonic()


def _clamp_retry(retry_after: float) -> float:
    if math.isinf(retry_after) or math.isnan(retry_after):
        return _MAX_RETRY_AFTER_SEC
    return max(_MIN_RETRY_AFTER_SEC, min(retry_after, _MAX_RETRY_AFTER_SEC))


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
    :class:`ValkeyBucketBackend` when running multi-pod.
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


# Atomic refill+consume implemented in Lua so it is safe under
# concurrent multi-pod writers. State per key is two numbers:
#   tokens  = current tokens (float, clamped to [0, capacity])
#   ts      = monotonic time of the last update (float seconds)
# Returns: {ok (0/1), remaining (int), retry_after (float seconds)}.
#
# Using monotonic time (passed by the caller as ARGV[3]) means we are
# immune to NTP step changes on the Valkey host — every pod's clock is
# only used to compute its *own* elapsed time relative to the previous
# request from the same pod-equivalent window.
# (ruff S105 false-positive: this is a Lua script, not a secret.)
_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])

local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens, ts
if data[1] == false then
    tokens = capacity
    ts = now
else
    tokens = tonumber(data[1])
    ts = tonumber(data[2])
end

local elapsed = 0
if now > ts then
    elapsed = now - ts
end

tokens = math.min(capacity, tokens + elapsed * refill)

local ok = 0
local retry = 0
if tokens >= 1 then
    tokens = tokens - 1
    ok = 1
else
    if refill > 0 then
        retry = (1 - tokens) / refill
    else
        retry = 86400
    end
end

redis.call('HSET', key, 'tokens', tokens, 'ts', now)
if ttl > 0 then
    redis.call('EXPIRE', key, ttl)
end

return {ok, math.floor(tokens), retry}
"""


class ValkeyBucketBackend:
    """Distributed backend backed by Valkey (Redis-compatible).

    Uses a single Lua ``EVAL`` per ``update()`` to make the
    refill-and-consume step atomic across all pods sharing the same
    Valkey. ``key_ttl_sec`` bounds memory growth on the Valkey side:
    idle buckets are reaped after the TTL, which sets an upper bound
    on the working-set size even when the key-space is adversarial.

    The caller is responsible for managing the Valkey client lifecycle
    (connection pool, auth, TLS); this backend only requires that the
    client supports ``eval(script, numkeys, *keys_and_args)``. Both
    ``valkey.asyncio.Valkey`` and ``redis.asyncio.Redis`` work.
    """

    def __init__(
        self,
        client: Any,
        *,
        key_ttl_sec: int = _DEFAULT_VALKEY_TTL_SEC,
    ) -> None:
        self._client = client
        self._key_ttl_sec = max(0, int(key_ttl_sec))
        # Pre-load the script so subsequent calls are EVALSHA, not EVAL.
        # Fall back gracefully if the server doesn't support SCRIPT LOAD
        # (e.g. some managed Redis variants in test modes).
        self._script_sha: str | None = None

    async def _ensure_script(self) -> str | None:
        if self._script_sha is not None:
            return self._script_sha
        try:
            sha = await self._client.script_load(_TOKEN_BUCKET_LUA)
        except Exception:
            # script_load unavailable — fall back to plain EVAL.
            return None
        self._script_sha = sha
        return sha

    async def update(
        self, key: str, capacity: int, refill_per_sec: float, now: float
    ) -> tuple[bool, int, float]:
        sha = await self._ensure_script()
        try:
            if sha is not None:
                raw = await self._client.evalsha(
                    sha,
                    1,
                    key,
                    int(capacity),
                    float(refill_per_sec),
                    float(now),
                    int(self._key_ttl_sec),
                )
            else:
                raw = await self._client.eval(
                    _TOKEN_BUCKET_LUA,
                    1,
                    key,
                    int(capacity),
                    float(refill_per_sec),
                    float(now),
                    int(self._key_ttl_sec),
                )
        except Exception:
            # NOSCRIPT or connectivity blip — retry once with EVAL.
            self._script_sha = None
            raw = await self._client.eval(
                _TOKEN_BUCKET_LUA,
                1,
                key,
                int(capacity),
                float(refill_per_sec),
                float(now),
                int(self._key_ttl_sec),
            )
        ok = bool(int(raw[0]))
        remaining = int(raw[1])
        retry = _clamp_retry(float(raw[2]))
        return (ok, remaining, retry)


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
    """Default + per-route + per-role rate limit knobs.

    ``trusted_proxy_depth`` selects the X-Forwarded-For element to trust.
    0 (default) = ignore XFF entirely and key on the ASGI client tuple.
    1 = trust one upstream proxy and key on the rightmost XFF entry.
    Set to your proxy chain depth so a client cannot spoof its IP.

    ``role_tiers`` maps an RBAC role name to a ``(per_minute, burst)``
    override that takes effect when the request can be authenticated
    inline (i.e. a Bearer JWT is present and decodes successfully).
    Unknown roles fall back to the default. Requests authenticated
    via API keys use the default tier — those credentials are hashed
    in the database and cannot be resolved by the middleware without
    a DB lookup.
    """

    default_per_minute: int = 60
    default_burst: int = 30
    exempt_paths: tuple[str, ...] = field(default_factory=tuple)
    overrides: dict[str, tuple[int, int]] = field(default_factory=dict)
    role_tiers: dict[str, tuple[int, int]] = field(default_factory=dict)
    trusted_proxy_depth: int = 0
    expose_headers: bool = False

    def limits_for_path(self, path: str) -> tuple[int, int] | None:
        # Prefix-match exempts so /health/live and /metrics/scrape are
        # also bypassed when /health and /metrics are listed.
        for exempt in self.exempt_paths:
            if path == exempt or path.startswith(exempt.rstrip("/") + "/"):
                return None
        for prefix, limits in self.overrides.items():
            if path.startswith(prefix):
                return limits
        return (self.default_per_minute, self.default_burst)

    def limits_for_role(self, role: str | None) -> tuple[int, int]:
        if role and role in self.role_tiers:
            return self.role_tiers[role]
        return (self.default_per_minute, self.default_burst)

    # Back-compat alias — older callers referenced ``for_path``.
    def for_path(self, path: str) -> tuple[int, int] | None:
        return self.limits_for_path(path)


# ---------------------------------------------------------------------------
# Auth extraction
# ---------------------------------------------------------------------------
#
# The middleware runs *before* FastAPI dependency injection, so we cannot
# read `request.user` — it isn't populated yet. We do a cheap inline decode
# of the Bearer JWT (no DB lookup) just to learn the principal's ``sub`` and
# ``role`` claims. Invalid / expired / missing tokens fall through to the
# unauthenticated (per-IP) path; the actual 401 is still emitted by the
# downstream auth dependency.

def _extract_bearer_token(scope: Any) -> str | None:
    """Return the Bearer credential from the Authorization header, or None."""
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name != b"authorization":
            continue
        try:
            value = raw_value.decode("latin-1")
        except UnicodeDecodeError:
            return None
        parts = value.split(None, 1)
        if len(parts) == _AUTH_SCHEME_PARTS and parts[0].lower() == "bearer" and parts[1]:
            return parts[1].strip()
    return None


def _extract_api_key(scope: Any) -> str | None:
    """Return the X-API-Key header value, or None."""
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name == b"x-api-key":
            try:
                return raw_value.decode("latin-1").strip() or None
            except UnicodeDecodeError:
                return None
    return None


class AuthExtractor:
    """Resolves a (principal_key, role) tuple from the ASGI scope.

    Encapsulated as a class so tests can substitute their own
    extractor without monkeypatching module-level state. The default
    implementation decodes Bearer JWTs via :mod:`engine.api.auth.jwt`.
    If decoding fails for any reason (missing secret, malformed
    token, expired signature), the request is treated as anonymous
    and falls back to the per-IP bucket.
    """

    def __init__(
        self,
        *,
        jwt_decode: Callable[[str], dict[str, Any] | None] | None = None,
    ) -> None:
        # Lazy import so this module remains import-safe in contexts
        # where the JWT secret isn't configured (e.g. unit tests).
        if jwt_decode is not None:
            self._decode = jwt_decode
        else:
            from engine.api.auth.jwt import decode_token as _decode_token  # noqa: PLC0415

            self._decode = _decode_token

    def resolve(self, scope: Any) -> tuple[str | None, str | None]:
        """Return ``(principal_key, role)``. Both are ``None`` when anon.

        ``principal_key`` is the bucket key fragment identifying the
        authenticated principal — either ``user:<sub>`` or
        ``apikey:<prefix>``. The caller prefixes it with the route
        limits when constructing the full bucket key.
        """
        # JWT path: Bearer tokens carry sub + role claims.
        bearer = _extract_bearer_token(scope)
        if bearer and not bearer.startswith("nxs_"):
            payload = self._safe_decode(bearer)
            if payload is not None:
                sub = payload.get("sub")
                role = payload.get("role")
                if isinstance(sub, str) and sub:
                    return f"user:{sub}", role if isinstance(role, str) else None

        # API-key path: tokens are hashed server-side, so we cannot
        # resolve a user_id without a DB hit. Key on the 12-char
        # display prefix so all requests with the same key share a
        # bucket across pods.
        api_key = _extract_api_key(scope)
        if api_key and api_key.startswith("nxs_") and len(api_key) >= _API_KEY_PREFIX_CHARS:
            return f"apikey:{api_key[:_API_KEY_PREFIX_CHARS]}", None
        if bearer and bearer.startswith("nxs_") and len(bearer) >= _API_KEY_PREFIX_CHARS:
            return f"apikey:{bearer[:_API_KEY_PREFIX_CHARS]}", None

        return None, None

    def _safe_decode(self, token: str) -> dict[str, Any] | None:
        try:
            return self._decode(token)  # type: ignore[no-any-return]
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class RateLimitMiddleware:
    """ASGI middleware that fronts every HTTP request with a TokenBucket.

    The bucket key is selected as follows:

    1. If the request carries a decodable Bearer JWT, key on
       ``user:<sub>`` and apply the role-tier limits (if configured).
    2. Else if the request carries an engine API key, key on
       ``apikey:<prefix>`` and apply the default limits.
    3. Else key on the request IP and apply the default limits.

    Per-route ``overrides`` continue to take precedence over role tiers
    — a route that is known to be expensive (e.g. /api/v1/client/errors)
    can override the limit regardless of who calls it.
    """

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
        auth_extractor: AuthExtractor | None = None,
    ) -> None:
        self.app = app
        self.config = config
        self.backend = backend or InMemoryBucketBackend()
        self._key_func = key_func or self._default_ip_key
        self._auth = auth_extractor or AuthExtractor()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "")
        if method in self.EXEMPT_METHODS:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        limits = self.config.limits_for_path(path)
        if limits is None:
            await self.app(scope, receive, send)
            return
        per_minute, burst = limits

        principal, role = self._auth.resolve(scope)
        if principal is not None:
            # Authenticated: per-user / per-key bucket with the role's
            # tier — but only if the route override did not pin a
            # custom limit. Role tiers apply when the route is on the
            # default tier; route overrides always win.
            route_overrides = self._route_override_for(path)
            if route_overrides is None:
                tier_per_min, tier_burst = self.config.limits_for_role(role)
                per_minute = tier_per_min
                burst = tier_burst
            key = f"{principal}:{path}"
        else:
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
            cid = ctx.get_correlation_id()
            log_extra = {
                "rate_limit_key": key,
                "rate_limit_burst": burst,
                "rate_limit_per_minute": per_minute,
                "principal": principal,
                "role": role,
            }
            if cid is not None:
                log_extra["correlation_id"] = cid
            response = self._build_429(burst, remaining, retry_after)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send_wrapper)

    def _route_override_for(self, path: str) -> tuple[int, int] | None:
        for prefix, limits in self.config.overrides.items():
            if path.startswith(prefix):
                return limits
        return None

    def _default_ip_key(self, scope: Any) -> str:
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
        retry_after_clamped = _clamp_retry(retry_after)
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
    "AuthExtractor",
    "BucketBackend",
    "InMemoryBucketBackend",
    "RateLimitConfig",
    "RateLimitMiddleware",
    "TokenBucket",
    "ValkeyBucketBackend",
]
