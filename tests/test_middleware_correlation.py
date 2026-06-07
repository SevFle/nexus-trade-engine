"""Tests for engine.api.middleware.correlation — Phase 2 helpers.

The canonical correlation middleware tests live in
``tests/observability/test_middleware.py``. This file covers the *new*
helpers added in the Phase 2 cross-cutting package:

- :func:`propagate_headers` — outbound header construction that picks up
  the active contextvars correlation id and validates an explicitly
  supplied one.
- :func:`current_correlation_id` — convenience accessor.
- Re-exports of the canonical middleware work end-to-end via the
  ``engine.api.middleware`` import path.

Also includes an integration test that wires the correlation middleware
*and* the Valkey rate-limiter together to assert that:

1. The ``X-Correlation-Id`` header is preserved on 429 responses (so the
   caller can correlate the throttle event back to its original trace).
2. Concurrent requests with distinct correlation ids run in isolation
   (no leakage between asyncio tasks).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import fakeredis
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from engine.api.middleware.correlation import (
    CORRELATION_HEADER,
    CorrelationIdMiddleware,
    current_correlation_id,
    propagate_headers,
)
from engine.api.middleware.rate_limit import (
    RateLimitConfig as MiddlewareRateLimitConfig,
)
from engine.api.middleware.rate_limit import (
    ValkeyRateLimitMiddleware,
)
from engine.observability import context as ctx

# ---------------------------------------------------------------------------
# propagate_headers
# ---------------------------------------------------------------------------


class TestPropagateHeaders:
    def test_returns_fresh_dict_with_no_input(self):
        # No context, no headers → fresh UUID generated and bound.
        ctx.clear_context()
        out = propagate_headers()
        assert CORRELATION_HEADER in out
        # Valid UUID
        uuid.UUID(out[CORRELATION_HEADER])

    def test_stamps_active_context_id(self):
        cid = "ctx-active-id-123"
        token = ctx._VARS["correlation_id"].set(cid)
        try:
            out = propagate_headers()
            assert out[CORRELATION_HEADER] == cid
        finally:
            ctx._VARS["correlation_id"].reset(token)

    def test_preserves_caller_supplied_header(self):
        ctx.clear_context()
        out = propagate_headers({"X-Correlation-Id": "caller-supplied"})
        assert out[CORRELATION_HEADER] == "caller-supplied"

    def test_validates_caller_supplied_header(self):
        ctx.clear_context()
        # CR/LF in supplied header → replaced with a safe UUID
        out = propagate_headers({"X-Correlation-Id": "evil\r\nSet-Cookie: x=1"})
        assert "\r" not in out[CORRELATION_HEADER]
        assert "\n" not in out[CORRELATION_HEADER]

    def test_extra_headers_merged(self):
        ctx.clear_context()
        out = propagate_headers(
            {"X-Other": "preserved"},
            extra={"X-Request-Id": "abc", "X-Source": "engine"},
        )
        assert CORRELATION_HEADER in out
        assert out["X-Other"] == "preserved"
        assert out["X-Request-Id"] == "abc"
        assert out["X-Source"] == "engine"

    def test_extra_does_not_overwrite_explicit_header(self):
        ctx.clear_context()
        out = propagate_headers(
            {"X-Request-Id": "explicit"},
            extra={"X-Request-Id": "extra-value"},
        )
        # setdefault preserves the explicit value.
        assert out["X-Request-Id"] == "explicit"

    def test_does_not_mutate_input(self):
        ctx.clear_context()
        original: dict[str, str] = {}
        out = propagate_headers(original)
        assert original == {}
        assert CORRELATION_HEADER in out
        # Mutating the result does not leak back.
        out["X-Marker"] = "leaked?"
        assert "X-Marker" not in original

    def test_subsequent_calls_reuse_bound_id(self):
        ctx.clear_context()
        a = propagate_headers()
        b = propagate_headers()
        # First call binds; second reuses.
        assert a[CORRELATION_HEADER] == b[CORRELATION_HEADER]

    def test_oversized_input_replaced(self):
        ctx.clear_context()
        out = propagate_headers({"X-Correlation-Id": "x" * 10_000})
        assert len(out[CORRELATION_HEADER]) <= 128


# ---------------------------------------------------------------------------
# current_correlation_id
# ---------------------------------------------------------------------------


class TestCurrentCorrelationId:
    def test_returns_none_when_unset(self):
        ctx.clear_context()
        assert current_correlation_id() is None

    def test_returns_active_id(self):
        token = ctx._VARS["correlation_id"].set("abc-123")
        try:
            assert current_correlation_id() == "abc-123"
        finally:
            ctx._VARS["correlation_id"].reset(token)


# ---------------------------------------------------------------------------
# Re-exported middleware still works end-to-end
# ---------------------------------------------------------------------------


def _build_correlation_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/echo")
    async def echo() -> dict:
        return {
            "correlation_id": ctx.get_correlation_id(),
            "request_id": ctx.get_request_id(),
        }

    return app


class TestCorrelationMiddlewareReExport:
    @pytest.fixture
    async def client(self):
        app = _build_correlation_app()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            yield ac

    @pytest.mark.asyncio
    async def test_incoming_header_propagates(self, client: AsyncClient):
        cid = "from-test-abc"
        r = await client.get("/echo", headers={CORRELATION_HEADER: cid})
        assert r.status_code == 200
        assert r.json()["correlation_id"] == cid
        assert r.headers[CORRELATION_HEADER] == cid

    @pytest.mark.asyncio
    async def test_missing_header_generates_uuid(self, client: AsyncClient):
        r = await client.get("/echo")
        cid = r.json()["correlation_id"]
        uuid.UUID(cid)
        assert r.headers[CORRELATION_HEADER] == cid


# ---------------------------------------------------------------------------
# Integration: correlation + Valkey rate-limit
# ---------------------------------------------------------------------------


def _build_integrated_app(fake_client: Any) -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        ValkeyRateLimitMiddleware,
        config=MiddlewareRateLimitConfig(
            default_per_minute=60,
            default_burst=2,
            expose_headers=True,
        ),
        client=fake_client,
    )
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/ping")
    async def ping() -> dict:
        return {"correlation_id": ctx.get_correlation_id()}

    return app


@pytest.fixture
async def fake_client():
    client = fakeredis.FakeAsyncValkey()
    try:
        yield client
    finally:
        await client.aclose()


class TestCorrelationAndRateLimitIntegration:
    @pytest.mark.asyncio
    async def test_429_response_carries_correlation_id(self, fake_client):
        """A throttled client must be able to correlate the 429 back to
        its original trace. Without this, retry tooling cannot tell
        which request was rejected."""
        app = _build_integrated_app(fake_client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            cid = "trace-abc-123"
            headers = {CORRELATION_HEADER: cid}
            await ac.get("/ping", headers=headers)
            await ac.get("/ping", headers=headers)
            r = await ac.get("/ping", headers=headers)
            assert r.status_code == 429
            # Header must survive the rate-limit short-circuit.
            assert r.headers[CORRELATION_HEADER] == cid

    @pytest.mark.asyncio
    async def test_generated_correlation_id_propagated_on_429(self, fake_client):
        """When the client does not supply an id the server-generated
        one must still be echoed back on the 429."""
        app = _build_integrated_app(fake_client)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            await ac.get("/ping")
            await ac.get("/ping")
            r = await ac.get("/ping")
            assert r.status_code == 429
            echoed = r.headers.get(CORRELATION_HEADER)
            assert echoed is not None
            uuid.UUID(echoed)

    @pytest.mark.asyncio
    async def test_concurrent_requests_keep_isolated_correlation_ids(
        self, fake_client
    ):
        """Even under load each request keeps its own correlation id.
        This is the headline guarantee of contextvars-based middleware
        — a buggy thread-local-style implementation would mix them."""
        # High enough burst that nobody is rate-limited; we want to test
        # isolation, not throttling.
        app = FastAPI()
        app.add_middleware(
            ValkeyRateLimitMiddleware,
            config=MiddlewareRateLimitConfig(
                default_per_minute=10_000, default_burst=10_000
            ),
            client=fake_client,
        )
        app.add_middleware(CorrelationIdMiddleware)

        @app.get("/echo")
        async def echo() -> dict:
            return {"cid": ctx.get_correlation_id()}

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            ids = [f"trace-{i:03d}" for i in range(50)]

            async def fire(cid: str) -> str:
                r = await ac.get("/echo", headers={CORRELATION_HEADER: cid})
                assert r.status_code == 200
                assert r.headers[CORRELATION_HEADER] == cid
                return r.json()["cid"]

            results = await asyncio.gather(*(fire(cid) for cid in ids))
            assert set(results) == set(ids)

    @pytest.mark.asyncio
    async def test_correlation_id_in_structlog_record(self, fake_client):
        """The structlog processor chain picks up the bound correlation
        id from contextvars — verify it shows up in the rendered record
        when a log fires inside a request handler."""
        from io import StringIO

        import structlog

        from engine.observability.processors import add_correlation_context

        app = _build_integrated_app(fake_client)

        @app.get("/log")
        async def log_endpoint() -> dict:
            log = structlog.get_logger("test")
            log.info("hello", field="value")
            return {"cid": ctx.get_correlation_id()}

        # Capture structlog output via the engine's own processor.
        buf = StringIO()
        structlog.configure(
            processors=[
                add_correlation_context,
                structlog.processors.add_log_level,
                structlog.processors.KeyValueRenderer(
                    sort_keys=True, key_order=["event", "correlation_id"]
                ),
            ],
            wrapper_class=structlog.make_filtering_bound_logger(0),
            logger_factory=structlog.PrintLoggerFactory(file=buf),
            cache_logger_on_first_use=False,
        )

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            cid = "structlog-trace-789"
            r = await ac.get("/log", headers={CORRELATION_HEADER: cid})
            assert r.status_code == 200

        rendered = buf.getvalue()
        # The log line must carry the correlation id from contextvars.
        assert cid in rendered
        assert "correlation_id=" in rendered


# ---------------------------------------------------------------------------
# Backward compat: the legacy import paths still work
# ---------------------------------------------------------------------------


class TestBackwardCompatImports:
    def test_legacy_correlation_middleware_still_importable(self):
        from engine.observability.middleware import CORRELATION_HEADER as LEGACY_HEADER
        from engine.observability.middleware import (
            CorrelationIdMiddleware as LegacyMw,
        )

        assert LEGACY_HEADER == CORRELATION_HEADER
        assert LegacyMw is CorrelationIdMiddleware

    def test_middleware_package_re_exports(self):
        from engine.api.middleware import CORRELATION_HEADER as PKG_HEADER
        from engine.api.middleware import (
            CorrelationIdMiddleware as PkgMw,
        )

        assert PKG_HEADER == CORRELATION_HEADER
        assert PkgMw is CorrelationIdMiddleware


# ---------------------------------------------------------------------------
# Helper coverage — exercises the package __init__ surface
# ---------------------------------------------------------------------------


class TestPackageExports:
    def test_all_exports_resolvable(self):
        from engine.api import middleware as pkg

        for name in pkg.__all__:
            assert hasattr(pkg, name), f"missing export: {name}"
            assert getattr(pkg, name) is not None

    def test_propagate_headers_via_package(self):
        from engine.api.middleware import propagate_headers as pkg_propagate

        ctx.clear_context()
        out = pkg_propagate()
        assert CORRELATION_HEADER in out
