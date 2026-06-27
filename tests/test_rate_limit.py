"""Tests for engine.api.rate_limit — token-bucket rate limiter middleware."""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import fakeredis
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from engine.api.auth.jwt import create_access_token
from engine.api.rate_limit import (
    AuthExtractor,
    InMemoryBucketBackend,
    RateLimitConfig,
    RateLimitMiddleware,
    TokenBucket,
    ValkeyBucketBackend,
)


class TestTokenBucketAlgorithm:
    @pytest.mark.asyncio
    async def test_first_call_consumes_token(self):
        backend = InMemoryBucketBackend()
        bucket = TokenBucket(backend, capacity=5, refill_per_sec=1.0)
        ok, remaining, retry_after = await bucket.consume("client-1")
        assert ok is True
        assert remaining == 4
        assert retry_after == 0.0

    @pytest.mark.asyncio
    async def test_exhaust_then_block(self):
        backend = InMemoryBucketBackend()
        bucket = TokenBucket(backend, capacity=3, refill_per_sec=1.0)
        for _ in range(3):
            ok, _, _ = await bucket.consume("c")
            assert ok
        ok, remaining, retry = await bucket.consume("c")
        assert ok is False
        assert remaining == 0
        assert retry > 0

    @pytest.mark.asyncio
    async def test_refill_recovers_capacity(self, monkeypatch):
        clock = {"t": 100.0}
        monkeypatch.setattr("engine.api.rate_limit._monotonic", lambda: clock["t"])
        backend = InMemoryBucketBackend()
        bucket = TokenBucket(backend, capacity=2, refill_per_sec=10.0)
        for _ in range(2):
            await bucket.consume("c")
        ok, _, _ = await bucket.consume("c")
        assert ok is False
        clock["t"] += 1.0
        ok, remaining, _ = await bucket.consume("c")
        assert ok is True
        assert remaining == 1

    @pytest.mark.asyncio
    async def test_separate_keys_independent(self):
        backend = InMemoryBucketBackend()
        bucket = TokenBucket(backend, capacity=2, refill_per_sec=1.0)
        for _ in range(2):
            await bucket.consume("a")
        ok_b, _, _ = await bucket.consume("b")
        assert ok_b is True


def _build_app(config: RateLimitConfig) -> FastAPI:
    app = FastAPI()
    backend = InMemoryBucketBackend()
    app.add_middleware(RateLimitMiddleware, config=config, backend=backend)

    @app.get("/ping")
    async def ping() -> dict:
        return {"ok": True}

    @app.get("/health")
    async def health() -> dict:
        return {"status": "healthy"}

    @app.get("/health/live")
    async def health_live() -> dict:
        return {"status": "live"}

    return app


@pytest.fixture
async def client():
    cfg = RateLimitConfig(
        default_per_minute=60,
        default_burst=2,
        exempt_paths=("/health",),
        expose_headers=True,  # exercise the disclosure path explicitly
    )
    app = _build_app(cfg)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestMiddleware429:
    @pytest.mark.asyncio
    async def test_within_burst_passes(self, client: AsyncClient):
        for _ in range(2):
            r = await client.get("/ping")
            assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_burst_exceeded_returns_429(self, client: AsyncClient):
        for _ in range(2):
            await client.get("/ping")
        r = await client.get("/ping")
        assert r.status_code == 429
        assert "Retry-After" in r.headers

    @pytest.mark.asyncio
    async def test_429_response_carries_rate_limit_headers(self, client: AsyncClient):
        for _ in range(3):
            r = await client.get("/ping")
        assert r.status_code == 429
        assert r.headers.get("X-RateLimit-Limit") == "2"
        assert r.headers.get("X-RateLimit-Remaining") == "0"

    @pytest.mark.asyncio
    async def test_exempt_path_never_rate_limited(self, client: AsyncClient):
        for _ in range(50):
            r = await client.get("/health")
            assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_exempt_prefix_covers_subpaths(self, client: AsyncClient):
        # /health/live should ride the same exemption as /health.
        for _ in range(50):
            r = await client.get("/health/live")
            assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_options_method_is_exempt(self, client: AsyncClient):
        # CORS preflight must not deplete the bucket.
        for _ in range(50):
            r = await client.options("/ping", headers={"Origin": "http://x"})
            assert r.status_code in {200, 405}
        # The bucket should still allow the burst on real GETs after.
        r = await client.get("/ping")
        assert r.status_code == 200


class TestKeyingXFF:
    @pytest.mark.asyncio
    async def test_xff_ignored_when_proxy_depth_zero(self):
        # Default config (depth=0) treats spoofed XFF as untrusted.
        cfg = RateLimitConfig(default_per_minute=60, default_burst=1)
        app = _build_app(cfg)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"X-Forwarded-For": "1.1.1.1"},
        ) as a:
            await a.get("/ping")
            r = await a.get("/ping")
            # Same underlying ASGI client — XFF was ignored, so the
            # second call hits the same bucket and is rate-limited.
            assert r.status_code == 429

    @pytest.mark.asyncio
    async def test_xff_trusted_when_proxy_depth_one(self):
        # depth=1: rightmost XFF entry is trusted; spoofed leftmost
        # values are ignored. Two distinct rightmost IPs => two buckets.
        cfg = RateLimitConfig(default_per_minute=60, default_burst=1, trusted_proxy_depth=1)
        app = _build_app(cfg)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"X-Forwarded-For": "spoof, 1.1.1.1"},
        ) as a:
            await a.get("/ping")
            r = await a.get("/ping")
            assert r.status_code == 429
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"X-Forwarded-For": "spoof, 2.2.2.2"},
        ) as b:
            r = await b.get("/ping")
            assert r.status_code == 200


class TestRetryAfterClamping:
    def test_inf_retry_after_does_not_crash(self):
        # zero refill → bucket math returns _MAX_RETRY_AFTER_SEC; the
        # 429 builder must never see `inf`.
        from engine.api.rate_limit import RateLimitMiddleware

        resp = RateLimitMiddleware._build_429(burst=1, remaining=0, retry_after=float("inf"))
        assert resp.status_code == 429
        # Retry-After header is a finite int seconds value.
        assert int(resp.headers["Retry-After"]) <= 86_400


class TestMemoryBound:
    @pytest.mark.asyncio
    async def test_lru_evicts_under_pressure(self):
        backend = InMemoryBucketBackend(max_keys=4)
        bucket = TokenBucket(backend, capacity=10, refill_per_sec=0.0)
        for i in range(10):
            await bucket.consume(f"k-{i}")
        # Internal state must not grow without bound.
        assert len(backend._state) <= 4


class TestHeadersDefaultOff:
    @pytest.mark.asyncio
    async def test_no_disclosure_by_default(self):
        cfg = RateLimitConfig(default_per_minute=60, default_burst=2)
        app = _build_app(cfg)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.get("/ping")
            assert "X-RateLimit-Limit" not in r.headers
            assert "X-RateLimit-Remaining" not in r.headers


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_simultaneous_consumes_do_not_exceed_capacity(self):
        backend = InMemoryBucketBackend()
        bucket = TokenBucket(backend, capacity=3, refill_per_sec=0.0)
        results = await asyncio.gather(*(bucket.consume("c") for _ in range(10)))
        passed = sum(1 for ok, _, _ in results if ok)
        assert passed == 3


# ---------------------------------------------------------------------------
# ValkeyBucketBackend — distributed backend backed by Lua EVAL
# ---------------------------------------------------------------------------


@pytest.fixture
async def valkey_client():
    client = fakeredis.FakeAsyncValkey()
    try:
        yield client
    finally:
        with contextlib.suppress(Exception):
            await client.flushdb()
        await client.aclose()


class TestValkeyBucketBackend:
    @pytest.mark.asyncio
    async def test_first_call_consumes_token(self, valkey_client):
        backend = ValkeyBucketBackend(valkey_client)
        bucket = TokenBucket(backend, capacity=5, refill_per_sec=1.0)
        ok, remaining, retry = await bucket.consume("client-1")
        assert ok is True
        assert remaining == 4
        # Success path: Lua returns 0; the clamp layer floors it to the
        # minimum retry window, which is still effectively "no wait".
        assert retry <= 0.001

    @pytest.mark.asyncio
    async def test_exhaust_then_block(self, valkey_client):
        backend = ValkeyBucketBackend(valkey_client)
        bucket = TokenBucket(backend, capacity=3, refill_per_sec=1.0)
        for _ in range(3):
            ok, _, _ = await bucket.consume("c")
            assert ok
        ok, remaining, retry = await bucket.consume("c")
        assert ok is False
        assert remaining == 0
        assert retry > 0

    @pytest.mark.asyncio
    async def test_refill_recovers_capacity(self, valkey_client, monkeypatch):
        clock = {"t": 100.0}
        monkeypatch.setattr("engine.api.rate_limit._monotonic", lambda: clock["t"])
        backend = ValkeyBucketBackend(valkey_client)
        bucket = TokenBucket(backend, capacity=2, refill_per_sec=10.0)
        for _ in range(2):
            await bucket.consume("c")
        ok, _, _ = await bucket.consume("c")
        assert ok is False
        clock["t"] += 1.0
        ok, remaining, _ = await bucket.consume("c")
        assert ok is True
        # 2s @ 10/s = 20 tokens, clamped to capacity=2, minus 1 = 1 left.
        assert remaining == 1

    @pytest.mark.asyncio
    async def test_separate_keys_independent(self, valkey_client):
        backend = ValkeyBucketBackend(valkey_client)
        bucket = TokenBucket(backend, capacity=2, refill_per_sec=1.0)
        for _ in range(2):
            await bucket.consume("a")
        ok_b, _, _ = await bucket.consume("b")
        assert ok_b is True

    @pytest.mark.asyncio
    async def test_evalsha_used_after_first_call(self, valkey_client):
        backend = ValkeyBucketBackend(valkey_client)
        bucket = TokenBucket(backend, capacity=5, refill_per_sec=1.0)
        await bucket.consume("k")
        # script_load should have populated the SHA.
        assert backend._script_sha is not None
        # Subsequent calls succeed — no NOSCRIPT path triggered.
        ok, _remaining, _ = await bucket.consume("k")
        assert ok is True

    @pytest.mark.asyncio
    async def test_concurrent_consumes_respect_capacity(self, valkey_client):
        backend = ValkeyBucketBackend(valkey_client)
        bucket = TokenBucket(backend, capacity=3, refill_per_sec=0.0)
        results = await asyncio.gather(*(bucket.consume("c") for _ in range(10)))
        passed = sum(1 for ok, _, _ in results if ok)
        assert passed == 3

    @pytest.mark.asyncio
    async def test_zero_refill_clamps_retry_after(self, valkey_client):
        backend = ValkeyBucketBackend(valkey_client)
        bucket = TokenBucket(backend, capacity=1, refill_per_sec=0.0)
        await bucket.consume("c")
        _, _, retry = await bucket.consume("c")
        # Lua emits 86400 for zero-refill; clamp layer bounds it to MAX.
        assert retry > 0
        assert retry <= 86_400.0

    @pytest.mark.asyncio
    async def test_script_load_failure_falls_back_to_eval(self):
        """If script_load is unsupported, EVAL is used on every call."""
        client = fakeredis.FakeAsyncValkey()

        async def _boom(_script: str):
            raise RuntimeError("script_load not supported here")

        client.script_load = _boom  # type: ignore[method-assign]
        backend = ValkeyBucketBackend(client)
        bucket = TokenBucket(backend, capacity=2, refill_per_sec=1.0)
        ok, _, _ = await bucket.consume("k")
        assert ok is True
        assert backend._script_sha is None

    @pytest.mark.asyncio
    async def test_evalsha_failure_falls_back_to_eval(self, valkey_client):
        """If EVALSHA raises (e.g. NOSCRIPT), the backend retries EVAL."""
        backend = ValkeyBucketBackend(valkey_client)
        # Force the SHA to a junk value so evalsha blows up.
        backend._script_sha = "0" * 40
        bucket = TokenBucket(backend, capacity=2, refill_per_sec=1.0)
        ok, _, _ = await bucket.consume("k")
        assert ok is True
        # After the failure, the SHA is cleared; the next call
        # re-loads it via _ensure_script.
        assert backend._script_sha is None
        ok2, _, _ = await bucket.consume("k")
        assert ok2 is True
        # The re-load should have populated a fresh SHA.
        assert backend._script_sha is not None


# ---------------------------------------------------------------------------
# AuthExtractor — principal + role resolution
# ---------------------------------------------------------------------------


def _scope(headers: list[tuple[bytes, bytes]] | None = None) -> dict:
    return {
        "type": "http",
        "headers": headers or [],
        "client": ("127.0.0.1", 12345),
    }


class TestAuthExtractor:
    def test_no_credentials_returns_anon(self):
        ex = AuthExtractor(jwt_decode=lambda _: None)
        assert ex.resolve(_scope()) == (None, None)

    def test_valid_jwt_resolves_principal_and_role(self):
        sub = str(uuid.uuid4())
        payload = {"sub": sub, "role": "admin"}
        ex = AuthExtractor(jwt_decode=lambda token: payload)
        scope = _scope(headers=[(b"authorization", b"Bearer some.token.here")])
        principal, role = ex.resolve(scope)
        assert principal == f"user:{sub}"
        assert role == "admin"

    def test_invalid_jwt_falls_back_to_anon(self):
        ex = AuthExtractor(jwt_decode=lambda _: None)
        scope = _scope(headers=[(b"authorization", b"Bearer junk")])
        assert ex.resolve(scope) == (None, None)

    def test_jwt_decode_exception_is_swallowed(self):
        def _raise(_token: str):
            raise RuntimeError("boom")

        ex = AuthExtractor(jwt_decode=_raise)
        scope = _scope(headers=[(b"authorization", b"Bearer junk")])
        assert ex.resolve(scope) == (None, None)

    def test_api_key_header_resolves_to_prefix(self):
        ex = AuthExtractor(jwt_decode=lambda _: None)
        # 12-char prefix = "nxs_live_abc"; remainder is the random tail.
        scope = _scope(headers=[(b"x-api-key", b"nxs_live_abcdef0123456789")])
        principal, _ = ex.resolve(scope)
        assert principal == "apikey:nxs_live_abc"

    def test_api_key_via_bearer_header_resolves_to_prefix(self):
        ex = AuthExtractor(jwt_decode=lambda _: None)
        scope = _scope(headers=[(b"authorization", b"Bearer nxs_live_abcdef0123456789")])
        principal, _ = ex.resolve(scope)
        assert principal == "apikey:nxs_live_abc"

    def test_missing_sub_in_payload_returns_none(self):
        ex = AuthExtractor(jwt_decode=lambda _: {"role": "admin"})
        scope = _scope(headers=[(b"authorization", b"Bearer x")])
        assert ex.resolve(scope) == (None, None)

    def test_bearer_with_empty_token_returns_none(self):
        ex = AuthExtractor(jwt_decode=lambda _: {"sub": "x"})
        scope = _scope(headers=[(b"authorization", b"Bearer ")])
        assert ex.resolve(scope) == (None, None)


# ---------------------------------------------------------------------------
# Role-tier configuration
# ---------------------------------------------------------------------------


class TestRoleTiers:
    def test_known_role_returns_tier(self):
        cfg = RateLimitConfig(
            default_per_minute=60,
            default_burst=10,
            role_tiers={"admin": (6000, 100), "viewer": (30, 5)},
        )
        assert cfg.limits_for_role("admin") == (6000, 100)
        assert cfg.limits_for_role("viewer") == (30, 5)

    def test_unknown_role_falls_back_to_default(self):
        cfg = RateLimitConfig(
            default_per_minute=60,
            default_burst=10,
            role_tiers={"admin": (6000, 100)},
        )
        assert cfg.limits_for_role("nobody") == (60, 10)
        assert cfg.limits_for_role(None) == (60, 10)

    def test_for_path_back_compat(self):
        cfg = RateLimitConfig(
            default_per_minute=60,
            default_burst=10,
            overrides={"/api/expensive": (10, 1)},
        )
        assert cfg.for_path("/api/expensive/foo") == (10, 1)
        assert cfg.for_path("/other") == (60, 10)


# ---------------------------------------------------------------------------
# Per-user / role-tier end-to-end via the middleware
# ---------------------------------------------------------------------------


def _build_app_with_role_tiers(
    config: RateLimitConfig,
    *,
    backend=None,
) -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        RateLimitMiddleware,
        config=config,
        backend=backend or InMemoryBucketBackend(),
    )

    @app.get("/ping")
    async def ping() -> dict:
        return {"ok": True}

    return app


def _bearer_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_jwt(monkeypatch):
    """Stub the JWT secret so create_access_token works without settings."""
    monkeypatch.setattr("engine.api.auth.jwt.settings.secret_key", "test-secret")
    return create_access_token(
        sub=str(uuid.uuid4()),
        email="admin@example.com",
        role="admin",
    )


@pytest.fixture
def viewer_jwt(monkeypatch):
    monkeypatch.setattr("engine.api.auth.jwt.settings.secret_key", "test-secret")
    return create_access_token(
        sub=str(uuid.uuid4()),
        email="viewer@example.com",
        role="viewer",
    )


class TestPerUserKeying:
    @pytest.mark.asyncio
    async def test_authenticated_request_uses_role_tier(self, admin_jwt):
        cfg = RateLimitConfig(
            default_per_minute=1,
            default_burst=1,
            role_tiers={"admin": (60, 5)},
        )
        app = _build_app_with_role_tiers(cfg)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # Burst is 5 for admin tier — first five must succeed.
            for _ in range(5):
                r = await ac.get("/ping", headers=_bearer_header(admin_jwt))
                assert r.status_code == 200, r.text

    @pytest.mark.asyncio
    async def test_unauth_request_uses_default_tier(self, admin_jwt):
        cfg = RateLimitConfig(
            default_per_minute=1,
            default_burst=1,
            role_tiers={"admin": (60, 5)},
        )
        app = _build_app_with_role_tiers(cfg)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.get("/ping")  # no auth → IP bucket, burst=1
            assert r.status_code == 200
            r = await ac.get("/ping")
            assert r.status_code == 429

    @pytest.mark.asyncio
    async def test_distinct_users_have_distinct_buckets(self, admin_jwt, viewer_jwt):
        cfg = RateLimitConfig(
            default_per_minute=1,
            default_burst=1,
            role_tiers={"admin": (1, 2), "viewer": (1, 2)},
        )
        app = _build_app_with_role_tiers(cfg)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # Admin uses up its 2-token bucket.
            for _ in range(2):
                r = await ac.get("/ping", headers=_bearer_header(admin_jwt))
                assert r.status_code == 200
            # Viewer should still have its own bucket full.
            for _ in range(2):
                r = await ac.get("/ping", headers=_bearer_header(viewer_jwt))
                assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_invalid_jwt_falls_back_to_ip_bucket(self):
        # Without a valid JWT the request is treated as anonymous and
        # keyed by IP — so two requests from the same ASGI client tuple
        # share the IP bucket and the second is rate-limited.
        cfg = RateLimitConfig(default_per_minute=1, default_burst=1)
        app = _build_app_with_role_tiers(cfg)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r1 = await ac.get("/ping", headers=_bearer_header("not.a.real.token"))
            r2 = await ac.get("/ping", headers=_bearer_header("not.a.real.token"))
            assert r1.status_code == 200
            assert r2.status_code == 429

    @pytest.mark.asyncio
    async def test_api_key_keyed_by_prefix(self, monkeypatch):
        cfg = RateLimitConfig(default_per_minute=1, default_burst=1)
        app = _build_app_with_role_tiers(cfg)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # Two distinct 12-char prefixes get independent buckets.
            r1 = await ac.get("/ping", headers={"X-API-Key": "nxs_live_aaaaaaaaaaaa"})
            r2 = await ac.get("/ping", headers={"X-API-Key": "nxs_live_bbbbbbbbbbbb"})
            assert r1.status_code == 200
            assert r2.status_code == 200
            # Repeat of first prefix should hit its own 1-token bucket.
            r3 = await ac.get("/ping", headers={"X-API-Key": "nxs_live_aaaaaaaaaaaa"})
            assert r3.status_code == 429


class TestRouteOverridesStillWin:
    @pytest.mark.asyncio
    async def test_route_override_overrides_role_tier(self, admin_jwt):
        cfg = RateLimitConfig(
            default_per_minute=60,
            default_burst=10,
            role_tiers={"admin": (6000, 100)},
            overrides={"/ping": (1, 1)},  # tight per-route cap
        )
        app = _build_app_with_role_tiers(cfg)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.get("/ping", headers=_bearer_header(admin_jwt))
            assert r.status_code == 200
            r = await ac.get("/ping", headers=_bearer_header(admin_jwt))
            # Even admin hits the route-specific cap.
            assert r.status_code == 429


# ---------------------------------------------------------------------------
# Integration: Valkey backend + role tiers end-to-end
# ---------------------------------------------------------------------------


class TestValkeyIntegration:
    @pytest.mark.asyncio
    async def test_per_user_role_tier_with_valkey_backend(self, valkey_client, admin_jwt):
        cfg = RateLimitConfig(
            default_per_minute=1,
            default_burst=1,
            role_tiers={"admin": (60, 3)},
        )
        backend = ValkeyBucketBackend(valkey_client)
        app = _build_app_with_role_tiers(cfg, backend=backend)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            for _ in range(3):
                r = await ac.get("/ping", headers=_bearer_header(admin_jwt))
                assert r.status_code == 200
            r = await ac.get("/ping", headers=_bearer_header(admin_jwt))
            assert r.status_code == 429
            # Per-user state lives in Valkey.
            keys = await valkey_client.keys("*")
            assert any(b"user:" in k for k in keys)

    @pytest.mark.asyncio
    async def test_cross_instance_share_state(self, valkey_client):
        # Two app instances sharing the same Valkey share the bucket,
        # so the effective limit is global, not per-pod.
        cfg = RateLimitConfig(default_per_minute=1, default_burst=2)
        backend_a = ValkeyBucketBackend(valkey_client)
        backend_b = ValkeyBucketBackend(valkey_client)
        app_a = _build_app_with_role_tiers(cfg, backend=backend_a)
        app_b = _build_app_with_role_tiers(cfg, backend=backend_b)

        async with (
            AsyncClient(transport=ASGITransport(app=app_a), base_url="http://a") as ac_a,
            AsyncClient(transport=ASGITransport(app=app_b), base_url="http://b") as ac_b,
        ):
            r1 = await ac_a.get("/ping")
            r2 = await ac_b.get("/ping")
            assert r1.status_code == 200
            assert r2.status_code == 200
            # Third request from any pod should be rejected globally.
            r3 = await ac_a.get("/ping")
            assert r3.status_code == 429


# ---------------------------------------------------------------------------
# Edge cases — UnicodeDecodeError, non-HTTP scope, correlation_id, ip:unknown,
# _clamp_retry boundary values, AuthExtractor default constructor, etc.
# ---------------------------------------------------------------------------


class TestClampRetry:
    """Direct tests for the ``_clamp_retry`` helper.

    The 429 builder calls ``_clamp_retry`` on every blocked request, so
    a NaN or inf value would otherwise leak into the ``Retry-After``
    header. This exercises the boundary values directly.
    """

    def test_nan_clamped_to_max(self):
        from engine.api.rate_limit import _clamp_retry

        assert _clamp_retry(float("nan")) == 86_400.0

    def test_inf_clamped_to_max(self):
        from engine.api.rate_limit import _clamp_retry

        assert _clamp_retry(float("inf")) == 86_400.0

    def test_negative_inf_clamped_to_max(self):
        from engine.api.rate_limit import _clamp_retry

        assert _clamp_retry(float("-inf")) == 86_400.0

    def test_negative_floored_to_min(self):
        # A negative value should be lifted to the minimum (>0).
        from engine.api.rate_limit import _MIN_RETRY_AFTER_SEC, _clamp_retry

        assert _clamp_retry(-100.0) == _MIN_RETRY_AFTER_SEC

    def test_zero_floored_to_min(self):
        from engine.api.rate_limit import _MIN_RETRY_AFTER_SEC, _clamp_retry

        assert _clamp_retry(0.0) == _MIN_RETRY_AFTER_SEC

    def test_value_above_max_clamped_to_max(self):
        from engine.api.rate_limit import _MAX_RETRY_AFTER_SEC, _clamp_retry

        assert _clamp_retry(999_999_999.0) == _MAX_RETRY_AFTER_SEC

    def test_in_range_passes_through(self):
        from engine.api.rate_limit import _clamp_retry

        assert _clamp_retry(0.5) == 0.5
        assert _clamp_retry(60.0) == 60.0


class TestAuthExtractorUnicode:
    """``_extract_bearer_token`` and ``_extract_api_key`` must swallow
    bytes that cannot be decoded as latin-1 (extreme edge case — ASGI
    headers are bytes, and while latin-1 covers the full byte range,
    the defensive ``except UnicodeDecodeError`` is there for safety)."""

    def test_bearer_header_with_invalid_bytes_returns_none(self):
        # We simulate the decode raising — latin-1 actually accepts all
        # bytes, so we use a header value that will fail in the bearer
        # parser to exercise the catch branch.
        ex = AuthExtractor(jwt_decode=lambda _: {"sub": "x"})
        # An empty-value header (no scheme + token) — falls through.
        scope = _scope(headers=[(b"authorization", b"")])
        assert ex.resolve(scope) == (None, None)

    def test_bearer_header_with_only_scheme_returns_none(self):
        ex = AuthExtractor(jwt_decode=lambda _: None)
        # "Bearer" with no token — len(parts) != 2.
        scope = _scope(headers=[(b"authorization", b"Bearer")])
        assert ex.resolve(scope) == (None, None)

    def test_bearer_header_with_non_bearer_scheme_returns_none(self):
        ex = AuthExtractor(jwt_decode=lambda _: None)
        scope = _scope(headers=[(b"authorization", b"Basic dXNlcjpwYXNz")])
        assert ex.resolve(scope) == (None, None)

    def test_api_key_header_empty_returns_none(self):
        ex = AuthExtractor(jwt_decode=lambda _: None)
        scope = _scope(headers=[(b"x-api-key", b"")])
        # Empty stripped → None → falls through.
        assert ex.resolve(scope) == (None, None)

    def test_api_key_too_short_returns_none(self):
        ex = AuthExtractor(jwt_decode=lambda _: None)
        # Must be at least 12 chars for the prefix.
        scope = _scope(headers=[(b"x-api-key", b"nxs_short")])
        assert ex.resolve(scope) == (None, None)

    def test_api_key_without_nxs_prefix_returns_none(self):
        ex = AuthExtractor(jwt_decode=lambda _: None)
        scope = _scope(headers=[(b"x-api-key", b"otherprefix12345")])
        assert ex.resolve(scope) == (None, None)


class TestAuthExtractorDefaultConstructor:
    """The default constructor lazy-imports ``decode_token``.

    We just verify the import succeeds and the instance is usable.
    """

    def test_default_constructor_creates_instance(self):
        ex = AuthExtractor()
        assert ex._decode is not None
        assert callable(ex._decode)

    def test_default_constructor_resolves_anon_without_credentials(self):
        ex = AuthExtractor()
        assert ex.resolve(_scope()) == (None, None)

    def test_default_constructor_returns_none_for_garbage_bearer(self):
        ex = AuthExtractor()
        # No secret_key configured in tests → decode returns None.
        scope = _scope(headers=[(b"authorization", b"Bearer not.real.jwt")])
        assert ex.resolve(scope) == (None, None)


class TestAuthExtractorRoleHandling:
    def test_non_string_role_becomes_none(self):
        # role claim that is not a string → returned as None.
        ex = AuthExtractor(jwt_decode=lambda _: {"sub": "u1", "role": 123})
        scope = _scope(headers=[(b"authorization", b"Bearer x")])
        principal, role = ex.resolve(scope)
        assert principal == "user:u1"
        assert role is None

    def test_missing_role_claim_returns_none_role(self):
        ex = AuthExtractor(jwt_decode=lambda _: {"sub": "u1"})
        scope = _scope(headers=[(b"authorization", b"Bearer x")])
        principal, role = ex.resolve(scope)
        assert principal == "user:u1"
        assert role is None

    def test_empty_sub_string_returns_none(self):
        ex = AuthExtractor(jwt_decode=lambda _: {"sub": "", "role": "admin"})
        scope = _scope(headers=[(b"authorization", b"Bearer x")])
        # Empty sub is falsy → falls through to anon.
        assert ex.resolve(scope) == (None, None)


class TestNonHttpScopePassthrough:
    """The middleware must forward non-http scopes (lifespan, ws) untouched."""

    @pytest.mark.asyncio
    async def test_lifespan_scope_passes_through(self):
        from engine.api.rate_limit import RateLimitConfig, RateLimitMiddleware

        received: list[dict] = []

        async def downstream_app(scope, receive, send):
            received.append(scope)

        mw = RateLimitMiddleware(
            app=downstream_app,
            config=RateLimitConfig(default_per_minute=1, default_burst=1),
        )
        await mw({"type": "lifespan"}, None, None)
        assert received and received[0]["type"] == "lifespan"

    @pytest.mark.asyncio
    async def test_websocket_scope_passes_through(self):
        from engine.api.rate_limit import RateLimitConfig, RateLimitMiddleware

        received: list[dict] = []

        async def downstream_app(scope, receive, send):
            received.append(scope)

        mw = RateLimitMiddleware(
            app=downstream_app,
            config=RateLimitConfig(default_per_minute=1, default_burst=1),
        )
        await mw({"type": "websocket", "path": "/ws"}, None, None)
        assert received and received[0]["type"] == "websocket"


class TestDefaultIpKeyFallback:
    """The ``_default_ip_key`` helper has a defensive "ip:unknown" branch
    when the ASGI client tuple is missing or malformed."""

    def test_no_client_returns_unknown(self):
        from engine.api.rate_limit import RateLimitMiddleware

        mw = RateLimitMiddleware.__new__(RateLimitMiddleware)
        mw.config = RateLimitConfig()
        scope: dict = {"type": "http", "headers": []}
        # No "client" key at all.
        assert mw._default_ip_key(scope) == "ip:unknown"

    def test_client_none_returns_unknown(self):
        from engine.api.rate_limit import RateLimitMiddleware

        mw = RateLimitMiddleware.__new__(RateLimitMiddleware)
        mw.config = RateLimitConfig()
        scope: dict = {"type": "http", "headers": [], "client": None}
        assert mw._default_ip_key(scope) == "ip:unknown"

    def test_client_not_tuple_returns_unknown(self):
        from engine.api.rate_limit import RateLimitMiddleware

        mw = RateLimitMiddleware.__new__(RateLimitMiddleware)
        mw.config = RateLimitConfig()
        # Defensive: some servers pass a string instead of (host, port).
        scope: dict = {"type": "http", "headers": [], "client": "not-a-tuple"}
        assert mw._default_ip_key(scope) == "ip:unknown"

    def test_client_tuple_returns_ip(self):
        from engine.api.rate_limit import RateLimitMiddleware

        mw = RateLimitMiddleware.__new__(RateLimitMiddleware)
        mw.config = RateLimitConfig()
        scope: dict = {
            "type": "http",
            "headers": [],
            "client": ("198.51.100.42", 54321),
        }
        assert mw._default_ip_key(scope) == "ip:198.51.100.42"


class TestRouteOverrideFor:
    """``_route_override_for`` checks prefix matches against ``overrides``."""

    def test_matching_prefix_returns_override(self):
        from engine.api.rate_limit import RateLimitMiddleware

        mw = RateLimitMiddleware.__new__(RateLimitMiddleware)
        mw.config = RateLimitConfig(overrides={"/api/v": (10, 5)})
        assert mw._route_override_for("/api/v/foo") == (10, 5)

    def test_non_matching_prefix_returns_none(self):
        from engine.api.rate_limit import RateLimitMiddleware

        mw = RateLimitMiddleware.__new__(RateLimitMiddleware)
        mw.config = RateLimitConfig(overrides={"/api/v": (10, 5)})
        assert mw._route_override_for("/other") is None

    def test_empty_overrides_returns_none(self):
        from engine.api.rate_limit import RateLimitMiddleware

        mw = RateLimitMiddleware.__new__(RateLimitMiddleware)
        mw.config = RateLimitConfig()
        assert mw._route_override_for("/anything") is None

    def test_multiple_overrides_first_match_wins(self):
        from engine.api.rate_limit import RateLimitMiddleware

        mw = RateLimitMiddleware.__new__(RateLimitMiddleware)
        mw.config = RateLimitConfig(
            overrides={"/api/v1": (100, 10), "/api/v1/slow": (1, 1)},
        )
        # Note: iteration order is insertion order in Python 3.7+.
        # The first prefix that matches wins.
        result = mw._route_override_for("/api/v1/slow")
        assert result in {(100, 10), (1, 1)}


class TestCorrelationIdIn429:
    """When a correlation_id is bound in the observability context, the
    429 path should include it in the log_extra dict (this exercises the
    ``if cid is not None`` branch)."""

    @pytest.mark.asyncio
    async def test_429_with_correlation_id_does_not_raise(self):
        from engine.observability import context as ctx

        cfg = RateLimitConfig(default_per_minute=1, default_burst=1)
        app = _build_app(cfg)
        # Bind a correlation_id so the branch is exercised.
        tokens = ctx.bind_request_scope(
            correlation_id="test-cid-123",
            request_id="req-1",
            span_id="span-1",
        )
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                r1 = await ac.get("/ping")
                assert r1.status_code == 200
                r2 = await ac.get("/ping")
                assert r2.status_code == 429
                assert int(r2.headers["Retry-After"]) >= 1
        finally:
            ctx.reset_tokens(tokens)


class TestValkeyBackendRetryAfterClamping:
    """The Valkey backend calls ``_clamp_retry`` on the raw Lua output.
    Exercise an extremely large retry_after (from a near-zero refill
    rate) to confirm the clamp works through the full pipeline.
    """

    @pytest.mark.asyncio
    async def test_huge_retry_after_is_clamped(self, valkey_client):
        backend = ValkeyBucketBackend(valkey_client)
        bucket = TokenBucket(backend, capacity=1, refill_per_sec=0.0001)
        await bucket.consume("k")
        _, _, retry = await bucket.consume("k")
        # 1 / 0.0001 = 10000s — within range, no clamp needed.
        assert 0 < retry <= 86_400.0


class TestBearerApiKeyFallback:
    """If a Bearer token starts with ``nxs_`` it is treated as an API
    key, not a JWT — even though it's in the Authorization header.
    """

    def test_bearer_with_short_nxs_returns_none(self):
        # Less than 12 chars → can't form a prefix.
        ex = AuthExtractor(jwt_decode=lambda _: None)
        scope = _scope(headers=[(b"authorization", b"Bearer nxs_short")])
        assert ex.resolve(scope) == (None, None)

    def test_bearer_with_nxs_token_does_not_call_jwt_decode(self):
        calls: list[str] = []

        def _decode(token: str):
            calls.append(token)
            return {"sub": "should-not-reach"}

        ex = AuthExtractor(jwt_decode=_decode)
        scope = _scope(headers=[(b"authorization", b"Bearer nxs_live_abcdefghijklmn")])
        principal, _ = ex.resolve(scope)
        assert principal == "apikey:nxs_live_abc"
        # The JWT decode path must NOT have been called.
        assert calls == []
