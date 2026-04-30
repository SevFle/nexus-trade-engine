"""Tests for the outbound httpx hook that injects X-Correlation-Id."""

from __future__ import annotations

import httpx
import pytest

from engine.observability import context as ctx
from engine.observability.http_client import (
    correlated_async_client,
    correlation_id_request_hook,
)


@pytest.fixture(autouse=True)
def _clear():
    ctx.clear_context()
    yield
    ctx.clear_context()


class TestRequestHook:
    def test_hook_injects_header_from_context(self):
        ctx.bind_correlation_id("c-out")
        req = httpx.Request("GET", "http://example.com")
        correlation_id_request_hook(req)
        assert req.headers["X-Correlation-Id"] == "c-out"

    def test_hook_skips_when_unbound(self):
        req = httpx.Request("GET", "http://example.com")
        correlation_id_request_hook(req)
        assert "X-Correlation-Id" not in req.headers

    def test_hook_does_not_overwrite_existing(self):
        ctx.bind_correlation_id("from-context")
        req = httpx.Request(
            "GET",
            "http://example.com",
            headers={"X-Correlation-Id": "explicit"},
        )
        correlation_id_request_hook(req)
        assert req.headers["X-Correlation-Id"] == "explicit"


def _echo(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={"x-correlation-id": request.headers.get("X-Correlation-Id")},
    )


class TestCorrelatedClient:
    @pytest.mark.asyncio
    async def test_correlated_async_client_attaches_hook(self):
        ctx.bind_correlation_id("c-client")
        async with correlated_async_client(transport=httpx.MockTransport(_echo)) as c:
            resp = await c.get("http://example.com/")
        assert resp.json()["x-correlation-id"] == "c-client"
