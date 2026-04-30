"""Tests for engine.api.rate_limit — token-bucket rate limiter middleware."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from engine.api.rate_limit import (
    InMemoryBucketBackend,
    RateLimitConfig,
    RateLimitExceededError,
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

    return app


@pytest.fixture
async def client():
    cfg = RateLimitConfig(
        default_per_minute=60,
        default_burst=2,
        exempt_paths=("/health",),
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


class TestKeying:
    @pytest.mark.asyncio
    async def test_different_clients_have_independent_buckets(self):
        cfg = RateLimitConfig(default_per_minute=60, default_burst=1)
        app = _build_app(cfg)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"X-Forwarded-For": "1.1.1.1"},
        ) as a:
            await a.get("/ping")
            r = await a.get("/ping")
            assert r.status_code == 429
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"X-Forwarded-For": "2.2.2.2"},
        ) as b:
            r = await b.get("/ping")
            assert r.status_code == 200


class TestErrorType:
    def test_exception_carries_retry_after(self):
        err = RateLimitExceededError(retry_after=2.5, limit=60, remaining=0)
        assert err.retry_after == 2.5
        assert err.limit == 60


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
