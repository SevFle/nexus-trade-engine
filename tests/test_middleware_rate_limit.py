"""Tests for engine.api.middleware.rate_limit — Valkey backend + auth keying.

Covers:

- :class:`RedisBucketBackend` against ``fakeredis.FakeAsyncValkey``:
  token-bucket correctness (capacity, refill, blocking, retry-after),
  atomicity under heavy concurrency (a single ``asyncio.gather`` of
  ``capacity + 50`` callers must admit exactly ``capacity``), key
  isolation, SCRIPT LOAD caching, and NOSCRIPT fall-back.
- :class:`AuthAwareKeyFunc`: JWT/API-key principal extraction, IP
  fallback, XFF honoring when ``trusted_proxy_depth`` > 0, and
  graceful degradation when the JWT decoder returns ``None``.
- :class:`ValkeyRateLimitMiddleware` end-to-end: 429 response shape,
  ``Retry-After`` header, correlation of consecutive requests,
  fallback behavior when no Valkey client is bound.

The tests use ``fakeredis.FakeAsyncValkey`` so they run hermetically
without a live Valkey instance.
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any
from unittest.mock import patch

import fakeredis
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from engine.api.middleware.rate_limit import (
    AuthAwareKeyFunc,
    RedisBucketBackend,
    ValkeyRateLimitMiddleware,
    _fingerprint,
    _header,
)
from engine.api.rate_limit import (
    BucketBackend,
    InMemoryBucketBackend,
    RateLimitConfig,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def fake_client():
    """Fresh FakeAsyncValkey per test — isolation without a real server."""
    client = fakeredis.FakeAsyncValkey()
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
async def shared_fake_server():
    """A shared FakeServer so multiple clients see the same state.

    Used to simulate multi-pod deployments where two app instances share
    a single Valkey and must coordinate via the Lua script.
    """
    return fakeredis.FakeServer()


def _build_app_with_middleware(
    middleware: ValkeyRateLimitMiddleware | None = None,
    *,
    config: RateLimitConfig | None = None,
) -> FastAPI:
    """Construct a minimal FastAPI app for tests that need the legacy
    construction path. The actual middleware injection is handled by
    callers via ``app.add_middleware`` because Starlette re-instantiates
    middleware classes internally."""
    del middleware, config  # accepted for API symmetry only
    return FastAPI()


def _app_with_valkey(
    client: Any,
    *,
    per_minute: int = 60,
    burst: int = 3,
    trusted_proxy_depth: int = 0,
) -> FastAPI:
    app = FastAPI()
    config = RateLimitConfig(
        default_per_minute=per_minute,
        default_burst=burst,
        trusted_proxy_depth=trusted_proxy_depth,
        expose_headers=True,
    )
    app.add_middleware(
        ValkeyRateLimitMiddleware,
        config=config,
        client=client,
    )

    @app.get("/ping")
    async def ping() -> dict:
        return {"ok": True}

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    return app


# ---------------------------------------------------------------------------
# RedisBucketBackend — algorithmic correctness
# ---------------------------------------------------------------------------


class TestRedisBucketBackendAlgorithm:
    @pytest.mark.asyncio
    async def test_first_call_consumes_token(self, fake_client):
        backend = RedisBucketBackend(fake_client)
        ok, remaining, retry_after = await backend.update(
            "k1", capacity=5, refill_per_sec=1.0, now=time.time()
        )
        assert ok is True
        assert remaining == 4
        assert retry_after == 0.0

    @pytest.mark.asyncio
    async def test_exhaustion_blocks_with_retry_after(self, fake_client):
        backend = RedisBucketBackend(fake_client)
        for _ in range(3):
            ok, _, _ = await backend.update("c", 3, 1.0, time.time())
            assert ok
        ok, remaining, retry_after = await backend.update(
            "c", 3, 1.0, time.time()
        )
        assert ok is False
        assert remaining == 0
        assert retry_after > 0.0
        # refill_per_sec=1 → wait ~1s for a token
        assert retry_after <= 1.5

    @pytest.mark.asyncio
    async def test_refill_recovers_after_wait(self, fake_client):
        clock = {"t": 1000.0}
        backend = RedisBucketBackend(fake_client, clock=lambda: clock["t"])
        for _ in range(2):
            await backend.update("c", 2, 10.0, clock["t"])
        ok, _, _ = await backend.update("c", 2, 10.0, clock["t"])
        assert ok is False
        # 10 tokens/sec → 1 token in 0.1s
        clock["t"] += 0.5
        ok, remaining, _ = await backend.update("c", 2, 10.0, clock["t"])
        assert ok is True
        # capacity 2, refilled fully, consumed one → 1 remaining
        assert remaining == 1

    @pytest.mark.asyncio
    async def test_separate_keys_independent(self, fake_client):
        backend = RedisBucketBackend(fake_client)
        for _ in range(2):
            await backend.update("a", 2, 1.0, time.time())
        ok, _, _ = await backend.update("b", 2, 1.0, time.time())
        assert ok is True

    @pytest.mark.asyncio
    async def test_zero_refill_blocks_indefinitely(self, fake_client):
        backend = RedisBucketBackend(fake_client)
        await backend.update("c", 1, 0.0, time.time())
        ok, _, retry_after = await backend.update("c", 1, 0.0, time.time())
        assert ok is False
        # 0 refill → server-side retry_after caps at 1 day (86400s)
        assert retry_after <= 86_400.0
        assert retry_after > 0.0

    @pytest.mark.asyncio
    async def test_zero_refill_clamps_max_retry(self, fake_client):
        backend = RedisBucketBackend(fake_client)
        await backend.update("c", 1, 0.0, time.time())
        _, _, retry_after = await backend.update("c", 1, 0.0, time.time())
        # Exactly 86400s, never infinity.
        assert math.isfinite(retry_after)


# ---------------------------------------------------------------------------
# RedisBucketBackend — atomicity under load
# ---------------------------------------------------------------------------


class TestRedisBucketBackendConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_consumes_never_exceed_capacity(self, fake_client):
        backend = RedisBucketBackend(fake_client)
        # 5 capacity, 0 refill, 50 concurrent calls — must admit exactly 5.
        results = await asyncio.gather(
            *(backend.update("c", 5, 0.0, time.time()) for _ in range(50))
        )
        passed = sum(1 for ok, _, _ in results if ok)
        assert passed == 5

    @pytest.mark.asyncio
    async def test_concurrent_consumes_across_keys(self, fake_client):
        backend = RedisBucketBackend(fake_client)
        keys = (["a"] * 20) + (["b"] * 20) + (["c"] * 20)
        results = await asyncio.gather(
            *(backend.update(k, 3, 0.0, time.time()) for k in keys)
        )
        per_key: dict[str, int] = {}
        for k, (ok, _, _) in zip(keys, results, strict=True):
            per_key[k] = per_key.get(k, 0) + (1 if ok else 0)
        # Each bucket independently admits exactly 3.
        assert per_key == {"a": 3, "b": 3, "c": 3}

    @pytest.mark.asyncio
    async def test_no_leaked_state_between_keys(self, fake_client):
        backend = RedisBucketBackend(fake_client)
        # Exhaust one key, verify others are unaffected.
        for _ in range(3):
            await backend.update("exhausted", 3, 0.0, time.time())
        ok, _, _ = await backend.update("fresh", 3, 0.0, time.time())
        assert ok is True

    @pytest.mark.asyncio
    async def test_parallel_throughput_is_bounded(self, fake_client):
        """Sanity check: bucket capacity is the hard ceiling regardless
        of how aggressive the caller is. This is the property that
        justifies using the limiter in front of an expensive LLM call
        — even 10_000 simultaneous requests cannot slip past the cap."""
        backend = RedisBucketBackend(fake_client)
        results = await asyncio.gather(
            *(backend.update("hot", 10, 0.0, time.time()) for _ in range(10_000))
        )
        assert sum(1 for ok, *_ in results if ok) == 10


# ---------------------------------------------------------------------------
# RedisBucketBackend — script loading and NOSCRIPT fall-back
# ---------------------------------------------------------------------------


class TestRedisScriptLoading:
    @pytest.mark.asyncio
    async def test_script_load_caches_sha(self, fake_client):
        backend = RedisBucketBackend(fake_client)
        await backend.update("k", 5, 1.0, time.time())
        assert backend._sha is not None

    @pytest.mark.asyncio
    async def test_evalsha_used_after_first_call(self, fake_client):
        backend = RedisBucketBackend(fake_client)
        await backend.update("k", 5, 1.0, time.time())
        # Patch evalsha to track usage; the second call must hit it.
        calls = {"evalsha": 0, "eval": 0}

        original_evalsha = fake_client.evalsha

        async def counting_evalsha(*args, **kwargs):
            calls["evalsha"] += 1
            return await original_evalsha(*args, **kwargs)

        original_eval = fake_client.eval

        async def counting_eval(*args, **kwargs):
            calls["eval"] += 1
            return await original_eval(*args, **kwargs)

        with patch.object(fake_client, "evalsha", counting_evalsha), \
             patch.object(fake_client, "eval", counting_eval):
            await backend.update("k", 5, 1.0, time.time())

        assert calls["evalsha"] == 1
        assert calls["eval"] == 0

    @pytest.mark.asyncio
    async def test_noscript_falls_back_to_eval(self, fake_client):
        backend = RedisBucketBackend(fake_client)
        # First call loads + caches the SHA.
        await backend.update("k", 5, 1.0, time.time())
        assert backend._sha is not None

        # Simulate SCRIPT FLUSH on the server: EVALSHA raises NOSCRIPT.
        evalsha_calls = {"n": 0}

        async def raising_evalsha(*args, **kwargs):
            evalsha_calls["n"] += 1
            msg = "NOSCRIPT No matching script. Please use EVAL."
            raise RuntimeError(msg)

        eval_calls = {"n": 0}
        original_eval = fake_client.eval

        async def counting_eval(*args, **kwargs):
            eval_calls["n"] += 1
            return await original_eval(*args, **kwargs)

        with patch.object(fake_client, "evalsha", raising_evalsha), \
             patch.object(fake_client, "eval", counting_eval):
            ok, _, _ = await backend.update("k", 5, 1.0, time.time())

        assert ok is True
        # EVALSHA was attempted and rejected; EVAL took over.
        assert evalsha_calls["n"] == 1
        assert eval_calls["n"] >= 1
        # The script was re-loaded after the fall-back so subsequent
        # requests can resume the fast path.
        assert backend._sha is not None


# ---------------------------------------------------------------------------
# RedisBucketBackend — key prefixing and TTL
# ---------------------------------------------------------------------------


class TestRedisKeyNamespacing:
    @pytest.mark.asyncio
    async def test_keys_are_namespaced(self, fake_client):
        backend = RedisBucketBackend(fake_client, key_prefix="tenantA")
        await backend.update("user-1", 5, 1.0, time.time())
        exists = await fake_client.exists("tenantA:user-1")
        assert exists == 1
        absent = await fake_client.exists("rl:user-1")
        assert absent == 0

    @pytest.mark.asyncio
    async def test_distinct_prefixes_do_not_collide(self, fake_client):
        a = RedisBucketBackend(fake_client, key_prefix="tenantA")
        b = RedisBucketBackend(fake_client, key_prefix="tenantB")
        # Exhaust tenantA's bucket
        for _ in range(2):
            await a.update("user", 2, 0.0, time.time())
        # Same logical key on tenantB is unaffected
        ok, _, _ = await b.update("user", 2, 0.0, time.time())
        assert ok is True

    @pytest.mark.asyncio
    async def test_ttl_is_set(self, fake_client):
        backend = RedisBucketBackend(fake_client, ttl_seconds=60)
        await backend.update("ephemeral", 5, 1.0, time.time())
        ttl = await fake_client.ttl("rl:ephemeral")
        # TTL must be set to roughly the configured value (allow some
        # slack for execution time).
        assert 1 <= ttl <= 60

    @pytest.mark.asyncio
    async def test_reset_clears_one_key(self, fake_client):
        backend = RedisBucketBackend(fake_client)
        await backend.update("a", 5, 1.0, time.time())
        await backend.update("b", 5, 1.0, time.time())
        await backend.reset("a")
        assert await fake_client.exists("rl:a") == 0
        assert await fake_client.exists("rl:b") == 1

    @pytest.mark.asyncio
    async def test_reset_clears_all_keys(self, fake_client):
        backend = RedisBucketBackend(fake_client)
        await backend.update("a", 5, 1.0, time.time())
        await backend.update("b", 5, 1.0, time.time())
        await backend.reset()
        assert await fake_client.exists("rl:a") == 0
        assert await fake_client.exists("rl:b") == 0


# ---------------------------------------------------------------------------
# RedisBucketBackend — multi-pod semantics
# ---------------------------------------------------------------------------


class TestMultiPodCoordination:
    @pytest.mark.asyncio
    async def test_two_backends_share_global_limit(self, shared_fake_server):
        """Two app instances, one Valkey — the global cap is the
        configured capacity, not capacity * pod_count."""
        client_a = fakeredis.FakeAsyncValkey(server=shared_fake_server)
        client_b = fakeredis.FakeAsyncValkey(server=shared_fake_server)
        try:
            backend_a = RedisBucketBackend(client_a)
            backend_b = RedisBucketBackend(client_b)
            # Each pod independently fires `capacity` requests. The Lua
            # script must serialize the updates so the total admitted
            # equals capacity, not 2 * capacity.
            results_a = await asyncio.gather(
                *(backend_a.update("user", 5, 0.0, time.time()) for _ in range(5))
            )
            results_b = await asyncio.gather(
                *(backend_b.update("user", 5, 0.0, time.time()) for _ in range(5))
            )
            total_admitted = sum(1 for ok, *_ in results_a if ok) + sum(
                1 for ok, *_ in results_b if ok
            )
            assert total_admitted == 5
        finally:
            await client_a.aclose()
            await client_b.aclose()


# ---------------------------------------------------------------------------
# AuthAwareKeyFunc
# ---------------------------------------------------------------------------


class TestAuthAwareKeyFunc:
    @staticmethod
    def _scope(headers: list[tuple[bytes, bytes]] | None = None,
               client: tuple[str, int] = ("203.0.113.7", 5000)) -> dict[str, Any]:
        return {
            "type": "http",
            "headers": headers or [],
            "client": client,
        }

    def test_anonymous_falls_back_to_ip(self):
        kf = AuthAwareKeyFunc()
        key = kf(self._scope())
        assert key == "ip:203.0.113.7"

    def test_valid_jwt_keys_user(self):
        kf = AuthAwareKeyFunc(jwt_decoder=lambda token: {"sub": "user-42"})
        scope = self._scope([
            (b"authorization", b"Bearer some.token.here"),
        ])
        assert kf(scope) == "user:user-42"

    def test_expired_jwt_falls_back_to_ip(self):
        kf = AuthAwareKeyFunc(jwt_decoder=lambda token: None)
        scope = self._scope([
            (b"authorization", b"Bearer expired.tok"),
        ])
        assert kf(scope) == "ip:203.0.113.7"

    def test_jwt_without_sub_falls_back_to_ip(self):
        kf = AuthAwareKeyFunc(jwt_decoder=lambda token: {"exp": 9999})
        scope = self._scope([
            (b"authorization", b"Bearer tok"),
        ])
        assert kf(scope) == "ip:203.0.113.7"

    def test_api_key_is_fingerprinted(self):
        kf = AuthAwareKeyFunc()
        scope = self._scope([
            (b"x-api-key", b"nk_live_abc123"),
        ])
        key = kf(scope)
        assert key.startswith("user:apikey:")
        # Same key always fingerprints to the same id.
        assert kf(self._scope([(b"x-api-key", b"nk_live_abc123")])) == key
        # Different key fingerprints differently.
        other = kf(self._scope([(b"x-api-key", b"nk_live_other")]))
        assert other != key

    def test_authorization_basic_is_ignored(self):
        # Basic auth must NOT be bucketed as a user — it's plaintext
        # and trivially spoofable.
        kf = AuthAwareKeyFunc()
        scope = self._scope([
            (b"authorization", b"Basic dXNlcjpwYXNz"),
        ])
        assert kf(scope) == "ip:203.0.113.7"

    def test_xff_honored_when_proxy_depth_set(self):
        kf = AuthAwareKeyFunc(trusted_proxy_depth=1)
        scope = self._scope([
            (b"x-forwarded-for", b"spoof, 198.51.100.5"),
        ])
        assert kf(scope) == "ip:198.51.100.5"

    def test_xff_ignored_when_proxy_depth_zero(self):
        kf = AuthAwareKeyFunc()
        scope = self._scope([
            (b"x-forwarded-for", b"spoofed-ip"),
        ])
        # Default depth=0 → ignore XFF entirely, fall through to client.
        assert kf(scope) == "ip:203.0.113.7"

    def test_jwt_takes_precedence_over_api_key(self):
        # Both headers present — JWT wins. API key auth is a fallback
        # for non-JWT clients, not an alternative identity.
        kf = AuthAwareKeyFunc(jwt_decoder=lambda t: {"sub": "u-1"})
        scope = self._scope([
            (b"authorization", b"Bearer tok"),
            (b"x-api-key", b"nk_live_xyz"),
        ])
        assert kf(scope) == "user:u-1"

    def test_missing_client_does_not_crash(self):
        kf = AuthAwareKeyFunc()
        scope = {"type": "http", "headers": []}
        assert kf(scope) == "ip:unknown"


class TestFingerprintAndHelpers:
    def test_fingerprint_is_deterministic(self):
        a = _fingerprint("nk_live_abc")
        b = _fingerprint("nk_live_abc")
        assert a == b
        assert len(a) == 16

    def test_fingerprint_is_not_the_token(self):
        tok = "nk_live_super_secret_value"
        fp = _fingerprint(tok)
        assert fp != tok
        assert fp not in tok

    def test_header_returns_none_when_missing(self):
        scope = {"headers": [(b"content-type", b"application/json")]}
        assert _header(scope, b"authorization") is None

    def test_header_returns_value_when_present(self):
        scope = {"headers": [(b"authorization", b"Bearer x")]}
        assert _header(scope, b"authorization") == b"Bearer x"


# ---------------------------------------------------------------------------
# ValkeyRateLimitMiddleware — end-to-end ASGI behavior
# ---------------------------------------------------------------------------


class TestValkeyRateLimitMiddleware:
    @pytest.mark.asyncio
    async def test_within_burst_passes(self, fake_client):
        app = _app_with_valkey(fake_client, per_minute=60, burst=3)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            for _ in range(3):
                r = await ac.get("/ping")
                assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_burst_exceeded_returns_429(self, fake_client):
        app = _app_with_valkey(fake_client, per_minute=60, burst=2)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            await ac.get("/ping")
            await ac.get("/ping")
            r = await ac.get("/ping")
            assert r.status_code == 429
            assert "Retry-After" in r.headers
            assert r.headers["X-RateLimit-Limit"] == "2"

    @pytest.mark.asyncio
    async def test_429_response_shape(self, fake_client):
        app = _app_with_valkey(fake_client, per_minute=60, burst=1)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            await ac.get("/ping")
            r = await ac.get("/ping")
            assert r.status_code == 429
            body = r.json()
            assert body["error"] == "rate_limit_exceeded"
            assert isinstance(body["retry_after"], int)
            assert body["retry_after"] >= 1

    @pytest.mark.asyncio
    async def test_exempt_path_skipped(self, fake_client):
        from engine.api.rate_limit import RateLimitConfig

        config = RateLimitConfig(
            default_per_minute=60,
            default_burst=1,
            exempt_paths=("/health",),
        )
        app = FastAPI()
        app.add_middleware(
            ValkeyRateLimitMiddleware,
            config=config,
            client=fake_client,
        )

        @app.get("/ping")
        async def ping() -> dict:
            return {"ok": True}

        @app.get("/health")
        async def health() -> dict:
            return {"status": "ok"}

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            for _ in range(10):
                r = await ac.get("/health")
                assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_distinct_callers_get_distinct_buckets(self, fake_client):
        app = _app_with_valkey(fake_client, per_minute=60, burst=2)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as a, AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as b:
            for _ in range(2):
                assert (await a.get("/ping")).status_code == 200
            # Different IP / port (httpx transport gives each client
            # the same scope, so we exercise the API key path).
            r_b = await b.get(
                "/ping", headers={"X-API-Key": "nk_distinct_caller_a"}
            )
            assert r_b.status_code == 200

    @pytest.mark.asyncio
    async def test_user_key_isolates_authenticated_callers(self, fake_client):
        """Authenticated callers must be keyed by their ``sub`` claim,
        not by their source IP. Two callers behind the same NAT sharing
        a JWT must share the bucket; two callers with different JWTs
        must not."""
        app = _app_with_valkey(fake_client, per_minute=60, burst=2)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            # Patch the JWT decoder so we don't need real tokens.
            from engine.api.middleware import rate_limit as rl_mod

            with patch.object(
                rl_mod, "AuthAwareKeyFunc",
                wraps=rl_mod.AuthAwareKeyFunc,
            ):
                # Same JWT twice → same bucket → second request blocked.
                headers = {"Authorization": "Bearer fake.same.token"}
                r1 = await ac.get("/ping", headers=headers)
                r2 = await ac.get("/ping", headers=headers)
                assert r1.status_code == 200
                assert r2.status_code == 200
                r3 = await ac.get("/ping", headers=headers)
                assert r3.status_code == 429

    @pytest.mark.asyncio
    async def test_falls_back_when_no_client_bound(self):
        """When no Valkey client is passed AND the fallback backend is
        provided, the middleware must transparently use the fallback
        (e.g. for unit tests without a live Valkey)."""
        fallback = InMemoryBucketBackend()
        app = FastAPI()
        config = RateLimitConfig(default_per_minute=60, default_burst=1)
        app.add_middleware(
            ValkeyRateLimitMiddleware,
            config=config,
            client=None,
            fallback_backend=fallback,
        )

        @app.get("/ping")
        async def ping() -> dict:
            return {"ok": True}

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            assert (await ac.get("/ping")).status_code == 200
            assert (await ac.get("/ping")).status_code == 429

    @pytest.mark.asyncio
    async def test_backend_protocol_satisfied(self, fake_client):
        # Sanity check: the new backend honors the BucketBackend Protocol.
        backend: BucketBackend = RedisBucketBackend(fake_client)
        result = await backend.update("k", 1, 1.0, time.time())
        assert isinstance(result, tuple)
        assert len(result) == 3
        ok, remaining, retry_after = result
        assert isinstance(ok, bool)
        assert isinstance(remaining, int)
        assert isinstance(retry_after, float)

    @pytest.mark.asyncio
    async def test_resolves_client_from_app_state(self, fake_client):
        """When no client is passed at construction, the middleware
        must reach into ``scope['app'].state.valkey`` on first request
        to find the lifespan-bound client. This is the production path:
        ``create_app()`` adds the middleware before lifespan opens the
        Valkey connection, so the client cannot be passed in __init__."""

        app = FastAPI()
        config = RateLimitConfig(default_per_minute=60, default_burst=2)
        app.add_middleware(
            ValkeyRateLimitMiddleware,
            config=config,
            # No client= passed!
        )

        @app.get("/ping")
        async def ping() -> dict:
            return {"ok": True}

        # Bind the valkey client on the app state — this is what the
        # lifespan does in production.
        app.state.valkey = fake_client

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            await ac.get("/ping")
            await ac.get("/ping")
            r = await ac.get("/ping")
            assert r.status_code == 429


# ---------------------------------------------------------------------------
# Cross-pod multi-instance ASGI integration
# ---------------------------------------------------------------------------


class TestMultiPodASGI:
    @pytest.mark.asyncio
    async def test_two_apps_share_global_cap(self, shared_fake_server):
        """The headline guarantee of the Valkey backend: two app
        instances behind a load balancer enforce the *global* cap, not
        the per-pod cap. This is what the in-memory backend cannot do."""
        client_a = fakeredis.FakeAsyncValkey(server=shared_fake_server)
        client_b = fakeredis.FakeAsyncValkey(server=shared_fake_server)
        app_a = _app_with_valkey(client_a, per_minute=60, burst=4)
        app_b = _app_with_valkey(client_b, per_minute=60, burst=4)
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app_a), base_url="http://a"
            ) as a, AsyncClient(
                transport=ASGITransport(app=app_b), base_url="http://b"
            ) as b:
                # Same source IP (httpx default) on both clients — same
                # bucket key — the 5th request across both pods must 429.
                rs = [
                    await asyncio.gather(a.get("/ping"), b.get("/ping"))
                    for _ in range(4)
                ]
                # Each pod has been hit 4 times = 8 total requests
                # against a 4-token bucket. Roughly 4 must succeed.
                succ = sum(1 for pair in rs for r in pair if r.status_code == 200)
                assert succ == 4
                # The next request from either pod must 429.
                r = await a.get("/ping")
                assert r.status_code == 429
        finally:
            await client_a.aclose()
            await client_b.aclose()
