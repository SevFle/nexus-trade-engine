"""Tests for engine.api.rate_limit — token-bucket rate limiter middleware."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from engine.api.auth.jwt import create_access_token
from engine.api.rate_limit import (
    InMemoryBucketBackend,
    RateLimitConfig,
    RateLimitMiddleware,
    TokenBucket,
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
    async def test_429_response_carries_rate_limit_headers(
        self, client: AsyncClient
    ):
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
        cfg = RateLimitConfig(
            default_per_minute=60, default_burst=1, trusted_proxy_depth=1
        )
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

        resp = RateLimitMiddleware._build_429(
            burst=1, remaining=0, retry_after=float("inf")
        )
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
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            r = await ac.get("/ping")
            assert "X-RateLimit-Limit" not in r.headers
            assert "X-RateLimit-Remaining" not in r.headers


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_simultaneous_consumes_do_not_exceed_capacity(self):
        backend = InMemoryBucketBackend()
        bucket = TokenBucket(backend, capacity=3, refill_per_sec=0.0)
        results = await asyncio.gather(
            *(bucket.consume("c") for _ in range(10))
        )
        passed = sum(1 for ok, _, _ in results if ok)
        assert passed == 3


# ---------------------------------------------------------------------------
# Per-user keying (JWT + API key + IP fallback)
# ---------------------------------------------------------------------------
#
# These tests exercise the ``user_or_ip`` key strategy: authenticated
# requests share a per-principal bucket regardless of which IP they
# come from, while anonymous requests fall back to IP-based keying.
# Helper token issuers are inline so each test is self-contained.

_USER_A = uuid.UUID("00000000-0000-0000-0000-00000000aaaa")
_USER_B = uuid.UUID("00000000-0000-0000-0000-00000000bbbb")
_TEST_SECRET = "test-secret-key-for-rate-limit-tests"


@pytest.fixture(autouse=True)
def _configure_jwt_secret():
    """Pin the JWT secret so create_access_token produces tokens the
    middleware can actually decode — production code reads
    ``settings.secret_key`` at decode time, so we must set it for the
    duration of each test in this module."""
    from engine.config import settings

    previous = settings.secret_key
    settings.secret_key = _TEST_SECRET
    try:
        yield
    finally:
        settings.secret_key = previous


def _build_keyed_app(config: RateLimitConfig | None = None) -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        RateLimitMiddleware,
        config=config
        or RateLimitConfig(
            default_per_minute=60,
            default_burst=2,
            key_strategy="user_or_ip",
        ),
        backend=InMemoryBucketBackend(),
    )

    @app.get("/ping")
    async def ping() -> dict:
        return {"ok": True}

    @app.get("/login")
    async def login() -> dict:
        # Stand-in for an unauthenticated endpoint — even if a token
        # is presented we want to key on IP for the login route.
        return {"ok": True}

    return app


class TestPerUserKeying:
    """Cover the new authenticated-principal keying path."""

    @pytest.mark.asyncio
    async def test_jwt_authenticated_users_share_one_bucket(self):
        """Two clients with the same JWT sub must share a bucket,
        even though they appear to come from different IPs."""
        token = create_access_token(
            sub=str(_USER_A),
            email="a@example.com",
            role="user",
        )
        # Same user, two ASGI clients (two distinct client tuples)
        app = _build_keyed_app()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://client-a"
        ) as a, AsyncClient(
            transport=ASGITransport(app=app), base_url="http://client-b"
        ) as b:
            await a.get("/ping", headers={"Authorization": f"Bearer {token}"})
            await b.get("/ping", headers={"Authorization": f"Bearer {token}"})
            # Both requests hit the same per-user bucket, so the third
            # call (from either client) is rate-limited.
            r = await a.get("/ping", headers={"Authorization": f"Bearer {token}"})
            assert r.status_code == 429

    @pytest.mark.asyncio
    async def test_distinct_jwt_users_have_independent_buckets(self):
        """Two distinct users sharing the same client IP each get the
        full burst — they must not penalise each other."""
        token_a = create_access_token(sub=str(_USER_A), email="a@example.com", role="user")
        token_b = create_access_token(sub=str(_USER_B), email="b@example.com", role="user")
        app = _build_keyed_app()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            # User A consumes both A-tokens.
            await ac.get("/ping", headers={"Authorization": f"Bearer {token_a}"})
            await ac.get("/ping", headers={"Authorization": f"Bearer {token_a}"})
            # User B's first request still passes — different bucket.
            r = await ac.get("/ping", headers={"Authorization": f"Bearer {token_b}"})
            assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_anonymous_request_falls_back_to_ip(self):
        """No credential → IP-based keying, same as the legacy default."""
        app = _build_keyed_app()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            await ac.get("/ping")
            await ac.get("/ping")
            r = await ac.get("/ping")
            assert r.status_code == 429

    @pytest.mark.asyncio
    async def test_invalid_jwt_falls_back_to_ip(self):
        """A malformed token must NOT crash the middleware and must
        collapse back to IP-based keying."""
        app = _build_keyed_app()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            await ac.get(
                "/ping",
                headers={"Authorization": "Bearer not-a-real-jwt"},
            )
            await ac.get(
                "/ping",
                headers={"Authorization": "Bearer not-a-real-jwt"},
            )
            r = await ac.get(
                "/ping",
                headers={"Authorization": "Bearer not-a-real-jwt"},
            )
            # Falls through to IP keying → bucket exhausted.
            assert r.status_code == 429

    @pytest.mark.asyncio
    async def test_api_key_via_x_api_key_header(self):
        """Engine-issued API keys (nxs_*) presented via X-API-Key get
        their own per-key bucket."""
        # Real key shape: nxs_<env>_<32 hex>  (≥ 12 char prefix)
        key_a = "nxs_live_" + "a" * 32
        key_b = "nxs_live_" + "b" * 32
        app = _build_keyed_app()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            await ac.get("/ping", headers={"X-API-Key": key_a})
            await ac.get("/ping", headers={"X-API-Key": key_a})
            r = await ac.get("/ping", headers={"X-API-Key": key_a})
            assert r.status_code == 429
            # Different key → independent bucket.
            r = await ac.get("/ping", headers={"X-API-Key": key_b})
            assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_api_key_via_bearer_header(self):
        """API keys sent as Bearer tokens must also be recognised —
        some clients (e.g. SDKs) put the engine key in Authorization."""
        key = "nxs_test_" + "c" * 32
        app = _build_keyed_app()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            await ac.get("/ping", headers={"Authorization": f"Bearer {key}"})
            await ac.get("/ping", headers={"Authorization": f"Bearer {key}"})
            r = await ac.get("/ping", headers={"Authorization": f"Bearer {key}"})
            assert r.status_code == 429

    @pytest.mark.asyncio
    async def test_malformed_api_key_falls_back_to_ip(self):
        """A non-engine Bearer token whose JWT decode fails must fall
        back to IP keying rather than 500."""
        app = _build_keyed_app()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            # Too short to be an engine key AND not a JWT.
            await ac.get("/ping", headers={"X-API-Key": "nxs_short"})
            await ac.get("/ping", headers={"X-API-Key": "nxs_short"})
            r = await ac.get("/ping", headers={"X-API-Key": "nxs_short"})
            assert r.status_code == 429

    @pytest.mark.asyncio
    async def test_unauthenticated_path_overrides_user_keying(self):
        """Even with a valid JWT, the login endpoint must be keyed by
        IP — otherwise a stolen token lets an attacker bypass the
        anonymous rate limit on /login."""
        token = create_access_token(
            sub=str(_USER_A),
            email="a@example.com",
            role="user",
        )
        app = _build_keyed_app(
            RateLimitConfig(
                default_per_minute=60,
                default_burst=2,
                unauthenticated_paths=("/login",),
            )
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            # Hit /login twice with the same token — these should be
            # keyed by IP and therefore exhaust the IP bucket, not the
            # user bucket.
            await ac.get("/login", headers={"Authorization": f"Bearer {token}"})
            await ac.get("/login", headers={"Authorization": f"Bearer {token}"})
            r = await ac.get("/login", headers={"Authorization": f"Bearer {token}"})
            assert r.status_code == 429
            # A different caller (no token) hitting /login should also
            # see the IP bucket drained, because they share the IP.
            r2 = await ac.get("/login")
            assert r2.status_code == 429
            # But that user's per-user bucket on /ping is untouched.
            r3 = await ac.get("/ping", headers={"Authorization": f"Bearer {token}"})
            assert r3.status_code == 200


class TestKeyStrategyIpOnly:
    """Opt-in IP-only keying for callers that don't want per-user buckets."""

    @pytest.mark.asyncio
    async def test_ip_only_ignores_authentication(self):
        token = create_access_token(
            sub=str(_USER_A),
            email="a@example.com",
            role="user",
        )
        app = _build_keyed_app(
            RateLimitConfig(
                default_per_minute=60,
                default_burst=2,
                key_strategy="ip_only",
            )
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            # Same caller: bucket fills regardless of token presence.
            await ac.get("/ping", headers={"Authorization": f"Bearer {token}"})
            await ac.get("/ping")  # no token — same IP
            r = await ac.get("/ping", headers={"Authorization": f"Bearer {token}"})
            assert r.status_code == 429


class TestUserKeyExtractorUnit:
    """Direct unit tests on the static helpers — fast feedback without
    spinning up an ASGI client."""

    def test_extract_bearer_token_returns_none_when_missing(self):
        from engine.api.rate_limit import _extract_bearer_token

        scope = {"headers": []}
        assert _extract_bearer_token(scope) is None

    def test_extract_bearer_token_handles_basic_auth(self):
        """Authorization: Basic ... must not be mistaken for a bearer."""
        from engine.api.rate_limit import _extract_bearer_token

        scope = {"headers": [(b"authorization", b"Basic dXNlcjpwYXNz")]}
        assert _extract_bearer_token(scope) is None

    def test_extract_bearer_token_decodes_real_jwt(self):
        """End-to-end: bearer header → user:<sub> key."""
        token = create_access_token(
            sub=str(_USER_A),
            email="a@example.com",
            role="user",
        )
        scope = {"headers": [(b"authorization", f"Bearer {token}".encode())]}
        assert RateLimitMiddleware._user_key(scope) == f"user:{_USER_A}"

    def test_extract_api_key(self):
        from engine.api.rate_limit import _extract_api_key

        scope = {"headers": [(b"x-api-key", b"nxs_live_aaaa")]}
        assert _extract_api_key(scope) == "nxs_live_aaaa"

    def test_api_key_prefix_short_token(self):
        from engine.api.rate_limit import _api_key_prefix

        # Too short to have a 12-char prefix → None
        assert _api_key_prefix("nxs_ab") is None
        # Wrong prefix family → None
        assert _api_key_prefix("abc_live_aaaaaaaaaaaa") is None
        # Real shape → first 12 chars
        assert _api_key_prefix("nxs_live_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa") == "nxs_live_aaa"

    def test_jwt_subject_decodes_valid_token(self):
        from engine.api.rate_limit import _jwt_subject

        token = create_access_token(
            sub=str(_USER_A),
            email="a@example.com",
            role="user",
        )
        assert _jwt_subject(token) == str(_USER_A)

    def test_jwt_subject_returns_none_for_garbage(self):
        from engine.api.rate_limit import _jwt_subject

        assert _jwt_subject("not-a-jwt") is None
        assert _jwt_subject("") is None

    def test_user_key_priority_jwt_over_api_key(self):
        """When both Authorization and X-API-Key are present, the JWT
        wins (it carries a stronger identity assertion)."""
        token = create_access_token(
            sub=str(_USER_A),
            email="a@example.com",
            role="user",
        )
        scope = {
            "headers": [
                (b"authorization", f"Bearer {token}".encode()),
                (b"x-api-key", b"nxs_live_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
            ]
        }
        key = RateLimitMiddleware._user_key(scope)
        assert key == f"user:{_USER_A}"

    def test_user_key_falls_through_to_api_key_on_bad_jwt(self):
        """Bad JWT in Authorization + valid X-API-Key → use the API key."""
        scope = {
            "headers": [
                (b"authorization", b"Bearer garbage"),
                (b"x-api-key", b"nxs_live_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
            ]
        }
        key = RateLimitMiddleware._user_key(scope)
        assert key == "apikey:nxs_live_aaa"

    def test_user_key_returns_none_for_anonymous(self):
        scope = {"headers": []}
        assert RateLimitMiddleware._user_key(scope) is None


class TestRateLimitConfigHelpers:
    """Direct tests on the new config helpers."""

    def test_is_unauthenticated_path_matches_exact(self):
        cfg = RateLimitConfig(unauthenticated_paths=("/login",))
        assert cfg.is_unauthenticated_path("/login") is True
        assert cfg.is_unauthenticated_path("/loginx") is False

    def test_is_unauthenticated_path_matches_subpaths(self):
        cfg = RateLimitConfig(unauthenticated_paths=("/auth",))
        assert cfg.is_unauthenticated_path("/auth/callback") is True
        assert cfg.is_unauthenticated_path("/auth/") is True
        assert cfg.is_unauthenticated_path("/authentication") is False

    def test_for_path_returns_none_for_exempt(self):
        cfg = RateLimitConfig(exempt_paths=("/health",))
        assert cfg.for_path("/health") is None
        assert cfg.for_path("/health/live") is None

    def test_for_path_returns_overrides(self):
        cfg = RateLimitConfig(
            default_per_minute=100,
            default_burst=10,
            overrides={"/api/v1/expensive": (5, 1)},
        )
        assert cfg.for_path("/api/v1/expensive") == (5, 1)
        assert cfg.for_path("/api/v1/expensive/sub") == (5, 1)
        assert cfg.for_path("/api/v1/other") == (100, 10)
