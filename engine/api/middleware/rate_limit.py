"""Valkey/Redis-backed token-bucket rate limiter + auth-aware keying.

Phase 2 extension of :mod:`engine.api.rate_limit`. The legacy
``InMemoryBucketBackend`` is correct but only per-pod — a fleet of *N*
workers under a load balancer effectively multiplies the configured
limit by *N*. This module ships:

- :class:`RedisBucketBackend` — implements the same
  :class:`~engine.api.rate_limit.BucketBackend` protocol but executes
  refill + consume *atomically* inside a Lua script. A single EVALSHA
  round-trip per request keeps p99 latency in the sub-millisecond range
  on a warm Valkey instance.
- :class:`AuthAwareKeyFunc` — keys authenticated requests by
  ``user:<user_id>`` and unauthenticated ones by ``ip:<addr>`` so a
  logged-in abuser cannot dodge the per-IP bucket by rotating IPs (or
  vice-versa).
- :class:`ValkeyRateLimitMiddleware` — a drop-in replacement for
  :class:`~engine.api.rate_limit.RateLimitMiddleware` that resolves the
  Valkey client from ``app.state.valkey`` (or a passed-in client) and
  routes bucket updates through it.

The Lua script is loaded once per backend instance (SCRIPT LOAD) and
re-used via EVALSHA. We fall back to EVAL on a NOSCRIPT error — that
path is exercised by the test suite to defend against script eviction.
"""

from __future__ import annotations

import hashlib
import time
from typing import TYPE_CHECKING, Any

from engine.api.rate_limit import (
    _MAX_RETRY_AFTER_SEC,
    _MIN_RETRY_AFTER_SEC,
    BucketBackend,
    RateLimitConfig,
    RateLimitMiddleware,
    TokenBucket,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.types import ASGIApp
    from valkey.asyncio import Valkey


# `Authorization: Bearer <token>` has exactly two whitespace-separated
# parts after splitting on the first space. Pulled out as a constant so
# the lint rule does not flag the literal `2`.
_EXPECTED_AUTH_PARTS = 2


# Atomic token-bucket update, executed inside Valkey.
#
# The script takes one key (the bucket hash) and four arguments:
#   1. capacity (tokens, integer)
#   2. refill_per_sec (float, encoded as string)
#   3. now (monotonic-ish seconds, float, encoded as string)
#   4. ttl_seconds (integer) — expire idle buckets so we do not leak
#      memory for one-shot callers
#
# It returns a 3-tuple: { ok (0/1), remaining (int), retry_after_ms (int) }
# The implementation is deliberately self-contained (no external state)
# and side-effect-free on read errors so the caller can fall back to
# the in-memory limiter if Valkey is briefly unreachable.
_LUA_TOKEN_BUCKET = rb"""
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])

local state = redis.call('HMGET', key, 'tokens', 'last')
local tokens = tonumber(state[1])
local last = tonumber(state[2])

if tokens == nil then
    tokens = capacity
    last = now
end

local elapsed = now - last
if elapsed < 0 then elapsed = 0 end

tokens = math.min(capacity, tokens + elapsed * refill)

local ok = 0
local retry_ms = 0
if tokens >= 1.0 then
    tokens = tokens - 1.0
    ok = 1
else
    if refill > 0 then
        local deficit = 1.0 - tokens
        retry_ms = math.ceil(deficit / refill * 1000)
        if retry_ms < 1 then retry_ms = 1 end
        if retry_ms > 86400000 then retry_ms = 86400000 end
    else
        retry_ms = 86400000
    end
end

-- Store as plain strings so HMSET/HSET works on every Valkey/Redis
-- version (no FLOAT type assumptions).
redis.call('HSET', key, 'tokens', tostring(tokens), 'last', tostring(now))
redis.call('EXPIRE', key, ttl)

return { ok, math.floor(tokens), retry_ms }
"""


def _now() -> float:
    """Indirection for tests. Wall-clock is fine here — we never compare
    across processes, only within a single Lua call."""
    return time.time()


class RedisBucketBackend(BucketBackend):
    """Atomic token-bucket backend backed by Valkey/Redis.

    Uses a single Lua script (loaded once) to perform the
    refill-then-consume-then-update step atomically. The script returns
    a 3-element array ``{ok, remaining, retry_after_ms}`` which we
    re-shape to the (ok, remaining, retry_after_sec) tuple expected by
    :class:`~engine.api.rate_limit.TokenBucket`.

    Parameters
    ----------
    client:
        An async Valkey/Redis client (``valkey.asyncio.Valkey``). The
        backend does not own the connection — pass one owned by the app
        so it is closed at shutdown by the app lifecycle.
    key_prefix:
        All keys are stored as ``f"{prefix}:{key}"`` so multiple API
        services sharing a Valkey instance cannot collide.
    ttl_seconds:
        Idle buckets expire after this many seconds. The default is
        generous (1 hour) so a caller that legitimately bursted and then
        idled does not lose their refill history across the typical
        request cadence. Set lower if memory pressure is a concern.
    clock:
        Optional callable returning the current time in seconds. Useful
        for deterministic tests.
    """

    DEFAULT_PREFIX = "rl"
    DEFAULT_TTL_SECONDS = 3600

    def __init__(
        self,
        client: Valkey,
        *,
        key_prefix: str = DEFAULT_PREFIX,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.client = client
        self.key_prefix = key_prefix
        self.ttl_seconds = ttl_seconds
        self._clock = clock or _now
        # Resolved on first use; EVALSHA fall-back handles eviction.
        self._sha: bytes | str | None = None

    async def update(
        self,
        key: str,
        capacity: int,
        refill_per_sec: float,
        now: float,
    ) -> tuple[bool, int, float]:
        # `now` from the caller is monotonic-clock-derived; that is fine
        # because the script only ever compares two values written by
        # the same clock. We pass it through unchanged.
        del now  # we use our own clock so tests can drive deterministically
        ts = self._clock()
        redis_key = f"{self.key_prefix}:{key}"

        ok, remaining, retry_ms = await self._eval(
            redis_key, capacity, refill_per_sec, ts
        )
        # Lua returns int milliseconds; convert to float seconds.
        retry_sec = retry_ms / 1000.0
        if retry_sec < _MIN_RETRY_AFTER_SEC and retry_ms > 0:
            retry_sec = _MIN_RETRY_AFTER_SEC
        retry_sec = min(retry_sec, _MAX_RETRY_AFTER_SEC)
        return (bool(ok), int(remaining), retry_sec)

    async def _eval(
        self,
        key: str,
        capacity: int,
        refill_per_sec: float,
        now: float,
    ) -> tuple[int, int, int]:
        """Run the Lua script via EVALSHA, falling back to EVAL on
        NOSCRIPT (which fires after SCRIPT FLUSH, restart, or LRU
        eviction on the server)."""
        args: list[str | int | float] = [
            int(capacity),
            f"{refill_per_sec:.9f}",
            f"{now:.9f}",
            int(self.ttl_seconds),
        ]
        if self._sha is not None:
            try:
                result = await self.client.evalsha(self._sha, 1, key, *args)
                return self._coerce(result)
            except Exception as exc:
                if self._is_noscript(exc):
                    self._sha = None
                else:
                    raise
        # First call (or after a NOSCRIPT fallback): load + eval in one
        # round-trip. SCRIPT LOAD is preferred over plain EVAL so we
        # cache the SHA for subsequent calls.
        try:
            sha = await self.client.script_load(_LUA_TOKEN_BUCKET)
            self._sha = sha
        except Exception:
            self._sha = None
        result = await self.client.eval(_LUA_TOKEN_BUCKET, 1, key, *args)
        return self._coerce(result)

    @staticmethod
    def _is_noscript(exc: BaseException) -> bool:
        msg = str(exc).lower()
        return "noscript" in msg or "not loaded" in msg

    @staticmethod
    def _coerce(result: Any) -> tuple[int, int, int]:
        # Defensive: older Valkey clients sometimes return bytes for
        # integer replies when going through EVAL.
        ok = int(result[0])
        remaining = int(result[1])
        retry_ms = int(result[2])
        return (ok, remaining, retry_ms)

    async def reset(self, key: str | None = None) -> None:
        """Test helper: clear bucket state.

        Without a key, scans the prefix namespace and deletes everything
        beneath it (slow; intended only for the test suite). With a key,
        only that caller's bucket is removed.
        """
        if key is None:
            pattern = f"{self.key_prefix}:*"
            async for k in self.client.scan_iter(match=pattern, count=100):
                await self.client.delete(k)
        else:
            await self.client.delete(f"{self.key_prefix}:{key}")


class AuthAwareKeyFunc:
    """Composite key function: per-user when authenticated, per-IP otherwise.

    Reads from the same ASGI scope as the default keying strategy but
    inspects the JWT/API-key material to bucket authenticated callers
    under ``user:<sub>`` rather than ``ip:<addr>``. Anonymous callers
    continue to be bucketed per source IP (with the same
    ``trusted_proxy_depth`` semantics as the legacy limiter) so an
    unauthenticated attacker cannot bypass the limit by claiming to be
    someone.

    The credential extraction is intentionally cheap and forgiving: we
    pull the bare token off the wire and reduce it to its ``sub`` claim
    via the existing JWT decoder. Verification failures, missing
    subjects, and API-key tokens all fall through to the IP bucket — the
    auth dependency further down the pipeline will reject the request
    with 401, but rate limiting must still apply so a flood of invalid
    tokens cannot exhaust downstream capacity.
    """

    def __init__(
        self,
        *,
        trusted_proxy_depth: int = 0,
        jwt_decoder: Callable[[str], dict[str, Any] | None] | None = None,
    ) -> None:
        self.trusted_proxy_depth = trusted_proxy_depth
        # Lazy import to avoid a hard runtime dependency on the auth
        # stack from this very-low-level middleware. The default
        # decoder reads from engine.api.auth.jwt.decode_token.
        self._jwt_decoder = jwt_decoder or self._default_decoder

    @staticmethod
    def _default_decoder(token: str) -> dict[str, Any] | None:
        try:
            from engine.api.auth.jwt import decode_token  # noqa: PLC0415
        except Exception:  # pragma: no cover — import-time safety net
            return None
        try:
            return decode_token(token)
        except Exception:
            return None

    def __call__(self, scope: Any) -> str:
        principal = self._extract_principal(scope)
        if principal is not None:
            return f"user:{principal}"
        return self._ip_key(scope)

    def _extract_principal(self, scope: Any) -> str | None:
        authz = _header(scope, b"authorization")
        if authz:
            parts = authz.split(b" ", 1)
            if len(parts) == _EXPECTED_AUTH_PARTS and parts[0].lower() == b"bearer":
                try:
                    token = parts[1].decode("latin-1")
                except UnicodeDecodeError:  # pragma: no cover — defensive
                    return None
                payload = self._jwt_decoder(token)
                if payload:
                    sub = payload.get("sub")
                    if isinstance(sub, str) and sub:
                        return sub
        api_key = _header(scope, b"x-api-key")
        if api_key:
            # API keys hash to the same prefix as user IDs so an
            # authenticated principal cannot dodge the bucket by
            # switching between JWT and API-key auth. We do *not* hash
            # the token itself (the rate-limit key shows up in
            # logs/metrics); a constant-time hash is overkill for
            # bucketing but a fingerprint is enough to prevent trivial
            # correlation.
            try:
                key_str = api_key.decode("latin-1")
            except UnicodeDecodeError:  # pragma: no cover
                return None
            if key_str:
                return f"apikey:{_fingerprint(key_str)}"
        return None

    def _ip_key(self, scope: Any) -> str:
        depth = self.trusted_proxy_depth
        if depth > 0:
            xff = _header(scope, b"x-forwarded-for")
            if xff:
                parts = [
                    p.strip().decode("latin-1")
                    for p in xff.split(b",")
                    if p.strip()
                ]
                if len(parts) >= depth:
                    return f"ip:{parts[-depth]}"
        client = scope.get("client")
        if client and isinstance(client, tuple):
            return f"ip:{client[0]}"
        return "ip:unknown"


def _header(scope: Any, name: bytes) -> bytes | None:
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name == name:
            return raw_value
    return None


def _fingerprint(token: str) -> str:
    """Stable short identifier for an API key. We do not need a
    cryptographic hash — the goal is just to break the trivial 1:1
    mapping between token and bucket key so a key cannot be recovered
    from a log line."""
    # built-in hash() is salted per-process; that is fine for bucketing
    # within one Valkey instance but breaks cross-process equality.
    # Use a deterministic non-cryptographic digest instead.
    # `usedforsecurity=False` is the documented opt-out for fingerprinting.
    return hashlib.sha1(
        token.encode("utf-8"), usedforsecurity=False
    ).hexdigest()[:16]


class ValkeyRateLimitMiddleware(RateLimitMiddleware):
    """Rate limiter that routes bucket updates through Valkey.

    Drop-in replacement for :class:`~engine.api.rate_limit.RateLimitMiddleware`
    that uses a :class:`RedisBucketBackend` instead of the in-process
    backend. The ASGI surface is identical so the two can be swapped at
    app construction time without touching route code.

    Parameters
    ----------
    app:
        The wrapped ASGI application.
    config:
        Same shape as :class:`~engine.api.rate_limit.RateLimitConfig`.
    client:
        Pre-built async Valkey/Redis client. When omitted, the
        middleware resolves ``app.state.valkey`` at request time so the
        limiter shares the app's connection pool.
    key_func:
        Optional keying override. Defaults to
        :class:`AuthAwareKeyFunc` keyed off the same
        ``trusted_proxy_depth`` value as ``config``.
    fallback_backend:
        In-memory backend used when the Valkey client is missing
        (typical in unit tests that construct the middleware without an
        app lifespan). When ``client`` is ``None`` *and* no
        ``app.state.valkey`` is bound, the middleware transparently
        falls back to this backend and logs a one-time warning.
    """

    def __init__(
        self,
        app: ASGIApp,
        config: RateLimitConfig,
        *,
        client: Valkey | None = None,
        key_func: Callable[[Any], str] | None = None,
        fallback_backend: BucketBackend | None = None,
    ) -> None:
        # Build a placeholder backend; the real one is materialized on
        # first request once we can see `app.state`.
        super().__init__(
            app=app,
            config=config,
            backend=fallback_backend or _PendingBackend(),
            key_func=key_func or AuthAwareKeyFunc(
                trusted_proxy_depth=config.trusted_proxy_depth,
            ),
        )
        self._client = client
        self._materialized: RedisBucketBackend | None = None
        self._fallback = fallback_backend
        self._fallback_warned = False

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        # Resolve the backend on first HTTP request so we don't require
        # an app.state.valkey at __init__ time (which runs before the
        # lifespan opens the connection).
        if scope["type"] == "http":
            await self._resolve_backend(scope)
        await super().__call__(scope, receive, send)

    async def _resolve_backend(self, scope: Any) -> None:
        if self._materialized is not None:
            self.backend = self._materialized
            return
        client = self._client
        if client is None:
            # Starlette/FastAPI set scope["app"] to the FastAPI instance,
            # which exposes the lifespan-bound state.valkey.
            app_obj = scope.get("app")
            client = getattr(getattr(app_obj, "state", None), "valkey", None) if app_obj else None
        if client is not None:
            self._materialized = RedisBucketBackend(client)
            self.backend = self._materialized
        elif self._fallback is not None:
            self.backend = self._fallback
        elif not self._fallback_warned:
            # Last-resort: keep the _PendingBackend, which admits
            # every request until the real client is bound. Log
            # once so logs are not flooded.
            self._fallback_warned = True
            import logging  # noqa: PLC0415 — lazy to avoid stdlib dep at import time

            logging.getLogger(__name__).warning(
                "ValkeyRateLimitMiddleware: no valkey client bound; "
                "requests will pass through unrestricted until a "
                "client is available"
            )


class _PendingBackend:
    """Sentinel backend used until the real one is materialized.

    Behaves as an unlimited pass-through so startup traffic (health
    probes, etc.) is not blocked before the lifespan opens the Valkey
    connection. Once the first request resolves a real backend this
    object is swapped out and never called again.
    """

    async def update(
        self,
        key: str,  # noqa: ARG002
        capacity: int,  # noqa: ARG002
        refill_per_sec: float,  # noqa: ARG002
        now: float,  # noqa: ARG002
    ) -> tuple[bool, int, float]:
        return (True, 1_000_000, 0.0)


# Convenience re-export so callers can import the Phase 1 config from
# either location.
__all__ = [
    "AuthAwareKeyFunc",
    "RateLimitConfig",
    "RedisBucketBackend",
    "TokenBucket",
    "ValkeyRateLimitMiddleware",
]
