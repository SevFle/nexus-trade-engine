"""Tests for the FastAPI correlation middleware."""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from engine.observability import context as ctx
from engine.observability.middleware import CorrelationIdMiddleware


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/echo")
    async def echo() -> dict:
        return {
            "correlation_id": ctx.get_correlation_id(),
            "request_id": ctx.get_request_id(),
        }

    return app


@pytest.fixture
async def client():
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestIncomingHeader:
    @pytest.mark.asyncio
    async def test_header_propagates_into_context(self, client: AsyncClient):
        cid = "abc-123-from-client"
        resp = await client.get("/echo", headers={"X-Correlation-Id": cid})
        assert resp.status_code == 200
        assert resp.json()["correlation_id"] == cid

    @pytest.mark.asyncio
    async def test_response_echoes_header(self, client: AsyncClient):
        cid = "abc-123"
        resp = await client.get("/echo", headers={"X-Correlation-Id": cid})
        assert resp.headers["X-Correlation-Id"] == cid


class TestGenerateWhenMissing:
    @pytest.mark.asyncio
    async def test_generates_uuid_when_header_missing(self, client: AsyncClient):
        resp = await client.get("/echo")
        body_cid = resp.json()["correlation_id"]
        assert body_cid is not None
        uuid.UUID(body_cid)
        assert resp.headers["X-Correlation-Id"] == body_cid


class TestRequestId:
    @pytest.mark.asyncio
    async def test_each_request_gets_distinct_request_id(self, client: AsyncClient):
        a = (await client.get("/echo")).json()["request_id"]
        b = (await client.get("/echo")).json()["request_id"]
        assert a is not None
        assert b is not None
        assert a != b


class TestContextLeak:
    @pytest.mark.asyncio
    async def test_context_cleared_after_response(self, client: AsyncClient):
        await client.get("/echo", headers={"X-Correlation-Id": "leak-check"})
        assert ctx.get_correlation_id() != "leak-check"
