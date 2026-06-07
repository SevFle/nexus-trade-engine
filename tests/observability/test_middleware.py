"""Tests for the FastAPI correlation middleware."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import BackgroundTasks, FastAPI
from fastapi.responses import StreamingResponse
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
        await client.get(
            "/echo", headers={"X-Correlation-Id": "leak-check-abc-123"}
        )
        # Each ASGI request runs in its own task; outer test scope must
        # never inherit the request-scoped correlation id.
        assert ctx.get_correlation_id() is None


class TestConcurrentRequests:
    @pytest.mark.asyncio
    async def test_concurrent_requests_keep_isolated_ids(
        self, client: AsyncClient
    ):
        import asyncio

        async def fire(cid: str) -> str:
            resp = await client.get("/echo", headers={"X-Correlation-Id": cid})
            return resp.json()["correlation_id"]

        ids = ("alpha-correlation-id", "beta-correlation-id")
        results = await asyncio.gather(*(fire(c) for c in ids))
        assert tuple(results) == ids


class TestHeaderInjectionDefense:
    @pytest.mark.asyncio
    async def test_crlf_in_header_is_rejected(self, client: AsyncClient):
        # httpx blocks CRLF in raw headers, so we drive a manually
        # constructed value through the validator instead.
        from engine.observability.middleware import _safe_correlation_id

        out = _safe_correlation_id("legit\r\nSet-Cookie: pwn=1")
        assert "\r" not in out
        assert "\n" not in out
        assert "Set-Cookie" not in out

    @pytest.mark.asyncio
    async def test_oversized_header_is_replaced(self, client: AsyncClient):
        from engine.observability.middleware import _safe_correlation_id

        out = _safe_correlation_id("x" * 10_000)
        assert len(out) <= 128

    @pytest.mark.asyncio
    async def test_control_chars_rejected(self, client: AsyncClient):
        from engine.observability.middleware import _safe_correlation_id

        out = _safe_correlation_id("\x1b[31mred")
        assert "\x1b" not in out


# ---------------------------------------------------------------------------
# Extended coverage: streaming, background tasks, exceptions, header
# variations, structlog propagation, and downstream-middleware visibility.
# ---------------------------------------------------------------------------


class TestStreamingResponses:
    """Streaming (chunked) responses must carry the correlation id on
    the response.start message and keep the contextvar alive for the
    duration of the stream."""

    @pytest.mark.asyncio
    async def test_streaming_response_carries_header(self):
        async def gen():
            assert ctx.get_correlation_id() is not None
            yield b"chunk-1\n"
            yield b"chunk-2"

        app = FastAPI()
        app.add_middleware(CorrelationIdMiddleware)

        @app.get("/stream")
        async def stream() -> StreamingResponse:
            return StreamingResponse(gen(), media_type="text/plain")

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.get(
                "/stream", headers={"X-Correlation-Id": "stream-cid"}
            )
            assert resp.status_code == 200
            assert resp.headers["X-Correlation-Id"] == "stream-cid"
            assert b"chunk-1" in resp.content
            assert b"chunk-2" in resp.content


class TestBackgroundTasks:
    """BackgroundTasks run after the response is sent; they must still
    see the bound correlation id so post-response work (webhook fan-out,
    audit logging) can be correlated back to the originating request."""

    @pytest.mark.asyncio
    async def test_background_task_sees_correlation_id(self):
        captured: dict[str, str | None] = {}

        async def slow_task() -> None:
            # The middleware keeps the contextvar bound until the
            # BackgroundTasks finish — see ``send_wrapper`` in
            # ``CorrelationIdMiddleware``.
            captured["cid"] = ctx.get_correlation_id()

        app = FastAPI()
        app.add_middleware(CorrelationIdMiddleware)

        @app.get("/fire")
        async def fire(background: BackgroundTasks) -> dict:
            background.add_task(slow_task)
            return {"queued": True}

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.get(
                "/fire", headers={"X-Correlation-Id": "bg-task-cid"}
            )
            assert resp.status_code == 200, resp.text
            assert resp.headers["X-Correlation-Id"] == "bg-task-cid"

        # After the response cycle completes (and any background tasks
        # are awaited) we should have captured the same correlation id.
        assert captured.get("cid") == "bg-task-cid"


class TestExceptionPath:
    """The response header must be set even when the handler raises
    a 500 — otherwise downstream tracing systems see the failure with
    no link back to the originating request."""

    @pytest.mark.asyncio
    async def test_correlation_header_set_on_500(self):
        app = FastAPI()
        app.add_middleware(CorrelationIdMiddleware)

        @app.get("/boom")
        async def boom() -> dict:
            raise RuntimeError("intentional")

        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as ac:
            resp = await ac.get(
                "/boom", headers={"X-Correlation-Id": "explode-cid"}
            )
            assert resp.status_code == 500
            assert resp.headers["X-Correlation-Id"] == "explode-cid"


class TestCustomHeaderName:
    @pytest.mark.asyncio
    async def test_custom_header_name_propagates(self):
        app = FastAPI()
        app.add_middleware(CorrelationIdMiddleware, header_name="X-Trace-Id")

        @app.get("/echo")
        async def echo() -> dict:
            return {"correlation_id": ctx.get_correlation_id()}

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.get("/echo", headers={"X-Trace-Id": "trace-abc"})
            assert resp.status_code == 200
            assert resp.headers["X-Trace-Id"] == "trace-abc"
            assert resp.json()["correlation_id"] == "trace-abc"
            # Default header must NOT be present — the middleware swaps
            # the header name cleanly.
            assert "X-Correlation-Id" not in resp.headers


class TestStructlogPropagation:
    """The middleware binds ``correlation_id`` into the contextvar that
    structlog's processor chain reads — a downstream handler logging
    via structlog must automatically pick it up."""

    @pytest.mark.asyncio
    async def test_structlog_sees_bound_context(self):
        import structlog

        captured: dict[str, object] = {}
        log = structlog.get_logger()

        app = FastAPI()
        app.add_middleware(CorrelationIdMiddleware)

        @app.get("/log")
        async def log_handler() -> dict:
            # structlog binds the contextvar into the rendered event;
            # we capture it for assertion by reading the contextvar
            # directly (the actual structlog integration is exercised
            # in test_processors.py).
            captured["ctx"] = ctx.snapshot()
            log.info("handler.called", path="/log")
            return {"ok": True}

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.get(
                "/log", headers={"X-Correlation-Id": "structlog-cid"}
            )
            assert resp.status_code == 200
            snap = captured["ctx"]
            assert isinstance(snap, dict)
            assert snap.get("correlation_id") == "structlog-cid"
            assert "request_id" in snap
            assert "span_id" in snap


class TestMiddlewareOrdering:
    """The correlation middleware must set its contextvar BEFORE the
    downstream app runs, so a downstream middleware (added later in the
    stack but executing earlier in request handling) can observe it.

    FastAPI/Starlette applies middleware in LIFO order: the last
    ``add_middleware`` call wraps everything else. So we put
    CorrelationId first, then a sentinel middleware after it — the
    sentinel must see the bound correlation id."""

    @pytest.mark.asyncio
    async def test_downstream_middleware_sees_bound_context(self):
        captured: dict[str, str | None] = {}

        class Sentinel:
            def __init__(self, app):
                self.app = app

            async def __call__(self, scope, receive, send):
                captured["cid"] = ctx.get_correlation_id()
                await self.app(scope, receive, send)

        app = FastAPI()
        # Sentinel added AFTER CorrelationId → wraps inside it, so its
        # __call__ runs after CorrelationId's bind_request_scope.
        app.add_middleware(CorrelationIdMiddleware)
        app.add_middleware(Sentinel)

        @app.get("/probe")
        async def probe() -> dict:
            return {"ok": True}

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.get(
                "/probe", headers={"X-Correlation-Id": "ordering-cid"}
            )
            assert resp.status_code == 200
            assert captured["cid"] == "ordering-cid"


class TestValidatorCoverage:
    """Edge cases for ``_safe_correlation_id`` not covered above."""

    def test_returns_fresh_uuid_for_empty_string(self):
        from engine.observability.middleware import _safe_correlation_id

        out = _safe_correlation_id("")
        uuid.UUID(out)  # raises if not a valid UUID

    def test_returns_fresh_uuid_for_none(self):
        from engine.observability.middleware import _safe_correlation_id

        out = _safe_correlation_id(None)
        uuid.UUID(out)

    def test_preserves_valid_visible_ascii(self):
        from engine.observability.middleware import _safe_correlation_id

        # All visible ASCII at the boundary length.
        s = "a" * 128
        assert _safe_correlation_id(s) == s

    def test_rejects_one_too_long(self):
        from engine.observability.middleware import _safe_correlation_id

        out = _safe_correlation_id("a" * 129)
        assert len(out) <= 128
        assert out != "a" * 129

    def test_rejects_space_only(self):
        from engine.observability.middleware import _safe_correlation_id

        # 0x20 is space; the regex requires \x21+ so space is invalid.
        out = _safe_correlation_id(" ")
        uuid.UUID(out)  # regenerated as UUID

    def test_accepts_visible_special_chars(self):
        from engine.observability.middleware import _safe_correlation_id

        # ! through ~ minus space — the full visible ASCII range.
        s = "!#$%&'()*+,-./:;<=>?@[]^_`{|}~"
        assert _safe_correlation_id(s) == s


class TestNonHttpScopes:
    """WebSocket and lifespan scopes must pass through unchanged."""

    @pytest.mark.asyncio
    async def test_lifespan_scope_passes_through(self):
        from engine.observability.middleware import CorrelationIdMiddleware

        received: dict[str, object] = {}

        async def downstream(scope, receive, send):
            received["type"] = scope.get("type")
            # Echo back the lifespan startup message so Starlette is
            # satisfied.
            if scope["type"] == "lifespan":
                msg = await receive()
                received["msg"] = msg["type"]
                if msg["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})

        wrapped = CorrelationIdMiddleware(downstream)
        # Drive a fake lifespan startup through the middleware.
        startup_sent = asyncio.Event()

        async def receive():
            return {"type": "lifespan.startup"}

        async def send(message):
            if message["type"] == "lifespan.startup.complete":
                startup_sent.set()

        await wrapped({"type": "lifespan"}, receive, send)
        await asyncio.wait_for(startup_sent.wait(), timeout=1.0)
        assert received["type"] == "lifespan"
