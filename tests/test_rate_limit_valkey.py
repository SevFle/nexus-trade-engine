"""Tests for the distributed Valkey-backed token-bucket backend.

Uses ``fakeredis.aioredis.FakeRedis`` as a hermetic stand-in for a real
Valkey/Redis server so the Lua atomicity, refill math, key isolation,
TTL expiry, and integration with :class:`RateLimitMiddleware` are all
exercised without external dependencies.
"""

from __future__ import annotations

import asyncio

import pytest
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from engine.api.rate_limit import (
    InMemoryBucketBackend,
    RateLimitConfig,
    RateLimitMiddleware,
    TokenBucket,
)
from engine.api.rate_limit_valkey import ValkeyBucketBackend, _coerce_result


@pytest.fixture
async def fake_redis():
    client = FakeAsyncRedis()
    yield client
    await client.aclose()


@pytest.fixture
async def backend(fake_redis):
    return ValkeyBucketBackend(fake_redis, state_ttl_sec=60)


# ---------------------------------------------------------------------------
# Lua result coercion
# ---------------------------------------------------------------------------


class TestCoerceResult:
    def test_handles_int_payload(self):
        ok, remaining, retry = _coerce_result([1, 5, b"0.5"])
        assert ok is True
        assert remaining == 5
        assert retry == 0.5

    def test_handles_bytes_payload(self):
        ok, remaining, retry = _coerce_result(
            [b"0", b"2", b"1.234"]
        )
        assert ok is False
        assert remaining == 2
        assert pytest.approx(retry) == 1.234

    def test_clamps_retry_to_safe_bounds(self):
        # Lua may return extreme values; the middleware must never emit
        # inf or negative retry_after to clients.
        ok, _, retry = _coerce_result([0, b"0", b"999999"])
        assert ok is False
        assert retry <= 86_400
        _, _, retry_low = _coerce_result([1, b"0", b"0"])
        assert retry_low >= 0.001  # min clamp

    def test_invalid_shape_raises(self):
        with pytest.raises(RuntimeError):
            _coerce_result([1, 2])
        with pytest.raises(RuntimeError):
            _coerce_result("not-a-list")


# ---------------------------------------------------------------------------
# Algorithm correctness
# ---------------------------------------------------------------------------


class TestAlgorithm:
    @pytest.mark.asyncio
    async def test_first_call_consumes_token(self, backend):
        ok, remaining, _ = await backend.update(
            "k", capacity=5, refill_per_sec=1.0, now=0.0
        )
        assert ok is True
        assert remaining == 4

    @pytest.mark.asyncio
    async def test_exhaustion_blocks_until_refill(self, backend):
        # capacity 3, refill 1/sec
        for _ in range(3):
            ok, _, _ = await backend.update(
                "k", capacity=3, refill_per_sec=1.0, now=0.0
            )
            assert ok
        ok, remaining, retry = await backend.update(
            "k", capacity=3, refill_per_sec=1.0, now=0.0
        )
        assert ok is False
        assert remaining == 0
        assert 0 < retry <= 1.0  # ~1 second to next token

    @pytest.mark.asyncio
    async def test_refill_after_wait(self, backend):
        for _ in range(2):
            await backend.update("k", capacity=2, refill_per_sec=10.0, now=0.0)
        ok, _, _ = await backend.update("k", capacity=2, refill_per_sec=10.0, now=0.0)
        assert ok is False
        await asyncio.sleep(0.15)  # ~1.5 tokens at 10/s
        ok, remaining, _ = await backend.update(
            "k", capacity=2, refill_per_sec=10.0, now=0.0
        )
        assert ok is True
        assert remaining == 0  # only 1 token was needed

    @pytest.mark.asyncio
    async def test_distinct_keys_isolated(self, backend):
        for _ in range(2):
            await backend.update("a", capacity=2, refill_per_sec=0.0, now=0.0)
        ok, _, _ = await backend.update("a", capacity=2, refill_per_sec=0.0, now=0.0)
        assert ok is False
        # Different key → independent bucket, still full.
        ok, remaining, _ = await backend.update(
            "b", capacity=2, refill_per_sec=0.0, now=0.0
        )
        assert ok is True
        assert remaining == 1

    @pytest.mark.asyncio
    async def test_capacity_is_a_ceiling(self, backend, fake_redis):
        """A long idle period must not over-fill the bucket."""
        await backend.update("k", capacity=3, refill_per_sec=100.0, now=0.0)
        # Burn the rest.
        for _ in range(2):
            await backend.update("k", capacity=3, refill_per_sec=100.0, now=0.0)
        await asyncio.sleep(0.1)  # would generate 10 tokens at 100/s
        ok, remaining, _ = await backend.update(
            "k", capacity=3, refill_per_sec=100.0, now=0.0
        )
        assert ok is True
        # Must be clamped at capacity=3, not 10.
        assert remaining <= 2  # we just consumed one

    @pytest.mark.asyncio
    async def test_concurrent_consumes_are_atomic(self, backend):
        """Hammer the backend with concurrent consumes for the same key;
        the Lua script must serialise them so capacity is never exceeded."""
        results = await asyncio.gather(*[
            backend.update("shared", capacity=5, refill_per_sec=0.0, now=0.0)
            for _ in range(50)
        ])
        passed = sum(1 for ok, _, _ in results if ok)
        assert passed == 5

    @pytest.mark.asyncio
    async def test_key_prefix_isolation(self, fake_redis):
        """Two backends with different prefixes must not share state."""
        b1 = ValkeyBucketBackend(fake_redis, key_prefix="v1:")
        b2 = ValkeyBucketBackend(fake_redis, key_prefix="v2:")
        await b1.update("k", capacity=1, refill_per_sec=0.0, now=0.0)
        ok, _, _ = await b1.update("k", capacity=1, refill_per_sec=0.0, now=0.0)
        assert ok is False  # exhausted in v1
        ok, _, _ = await b2.update("k", capacity=1, refill_per_sec=0.0, now=0.0)
        assert ok is True  # v2 still has its token


# ---------------------------------------------------------------------------
# TTL / expiry
# ---------------------------------------------------------------------------


class TestStateExpiry:
    @pytest.mark.asyncio
    async def test_state_keys_get_ttl(self, backend, fake_redis):
        await backend.update("k", capacity=5, refill_per_sec=1.0, now=0.0)
        ttl = await fake_redis.ttl("ratelimit:k")
        assert 0 < ttl <= 60

    @pytest.mark.asyncio
    async def test_custom_ttl(self, fake_redis):
        b = ValkeyBucketBackend(fake_redis, state_ttl_sec=120)
        await b.update("k", capacity=5, refill_per_sec=1.0, now=0.0)
        ttl = await fake_redis.ttl("ratelimit:k")
        assert 60 < ttl <= 120

    @pytest.mark.asyncio
    async def test_reset_drops_state(self, backend):
        await backend.update("k", capacity=2, refill_per_sec=0.0, now=0.0)
        await backend.update("k", capacity=2, refill_per_sec=0.0, now=0.0)
        ok, _, _ = await backend.update("k", capacity=2, refill_per_sec=0.0, now=0.0)
        assert ok is False
        await backend.reset("k")
        ok, _, _ = await backend.update("k", capacity=2, refill_per_sec=0.0, now=0.0)
        assert ok is True


# ---------------------------------------------------------------------------
# Equivalence with InMemoryBucketBackend
# ---------------------------------------------------------------------------


class TestBackendEquivalence:
    """The Valkey backend must produce the same consumption sequence as
    the in-memory backend for the same key+config — this is the
    invariant the middleware relies on."""

    @pytest.mark.asyncio
    async def test_consumption_sequence_matches_in_memory(self, fake_redis):
        v_backend = ValkeyBucketBackend(fake_redis)
        m_backend = InMemoryBucketBackend()
        capacity = 4
        refill = 2.0

        for i in range(8):
            v_ok, v_rem, _ = await v_backend.update(
                "k", capacity, refill, now=0.0
            )
            m_ok, m_rem, _ = await m_backend.update(
                "k", capacity, refill, now=0.0
            )
            assert v_ok == m_ok, f"iteration {i}: ok mismatch"
            assert v_rem == m_rem, f"iteration {i}: remaining mismatch"


# ---------------------------------------------------------------------------
# Integration with RateLimitMiddleware
# ---------------------------------------------------------------------------


def _build_app_with_backend(backend) -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        RateLimitMiddleware,
        config=RateLimitConfig(default_per_minute=60, default_burst=2),
        backend=backend,
    )

    @app.get("/ping")
    async def ping() -> dict:
        return {"ok": True}

    return app


class TestMiddlewareIntegration:
    @pytest.mark.asyncio
    async def test_middleware_with_valkey_backend(self, backend):
        """Wire the Valkey backend into the full middleware and verify
        the 429 path still works end-to-end."""
        app = _build_app_with_backend(backend)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            await ac.get("/ping")
            await ac.get("/ping")
            r = await ac.get("/ping")
            assert r.status_code == 429
            assert "Retry-After" in r.headers

    @pytest.mark.asyncio
    async def test_shared_backend_two_app_instances(self, fake_redis):
        """Two API processes sharing one Valkey backend must share the
        same per-key bucket — the whole point of moving state out of
        process."""
        backend_a = ValkeyBucketBackend(fake_redis)
        backend_b = ValkeyBucketBackend(fake_redis)
        app_a = _build_app_with_backend(backend_a)
        app_b = _build_app_with_backend(backend_b)
        async with AsyncClient(
            transport=ASGITransport(app=app_a), base_url="http://a"
        ) as a, AsyncClient(
            transport=ASGITransport(app=app_b), base_url="http://b"
        ) as b:
            await a.get("/ping")
            await b.get("/ping")  # second consume from shared bucket
            r = await a.get("/ping")  # bucket exhausted
            assert r.status_code == 429

    @pytest.mark.asyncio
    async def test_tokenbucket_indirectly_uses_backend(self, backend):
        """The :class:`TokenBucket` facade must route through the
        Valkey backend when wired in."""
        bucket = TokenBucket(backend, capacity=2, refill_per_sec=0.0)
        ok, _, _ = await bucket.consume("k")
        assert ok
        ok, _, _ = await bucket.consume("k")
        assert ok
        ok, _, _ = await bucket.consume("k")
        assert ok is False
