"""Lifecycle tests for the raw-ASGI ``CorrelationIdMiddleware``.

These cover the requirements that the *default* (raw-ASGI) middleware:

* binds ``correlation_id`` / ``request_id`` into the structlog contextvars
  (not just the legacy observability context) so records rendered via
  ``merge_contextvars`` carry them;
* handles **WebSocket** connections — reading the id from the handshake
  headers and binding it for the full connection lifecycle; and
* keeps the binding live for **BackgroundTasks**, so log lines emitted by
  a background callback carry the originating ``correlation_id``.

The ``BaseHTTPMiddleware`` variant (:mod:`engine.middleware.correlation`)
fails the last two cases by construction, which is exactly why the raw-ASGI
middleware is the default registered by ``create_app``.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import structlog
from fastapi import BackgroundTasks, FastAPI, WebSocket
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from engine.observability import context as ctx
from engine.observability.middleware import CorrelationIdMiddleware


def _build_http_app(captured: dict[str, Any] | None = None) -> FastAPI:
    """App exposing endpoints that report the live correlation bindings."""
    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/inspect")
    async def inspect() -> dict:
        return {
            "ctx_cid": ctx.get_correlation_id(),
            "ctx_rid": ctx.get_request_id(),
            # structlog contextvars — must be bound by the raw-ASGI middleware.
            "structlog_cid": structlog.contextvars.get_contextvars().get(
                "correlation_id"
            ),
            "structlog_rid": structlog.contextvars.get_contextvars().get(
                "request_id"
            ),
        }

    @app.get("/background")
    async def with_background(background_tasks: BackgroundTasks) -> dict:
        async def _bg() -> None:
            # Runs *after* the response is sent. The raw-ASGI middleware
            # has not reset yet (its __call__ is still awaiting the app),
            # so both channels must still carry the correlation id.
            assert captured is not None
            captured["bg_ctx_cid"] = ctx.get_correlation_id()
            captured["bg_structlog_cid"] = structlog.contextvars.get_contextvars().get(
                "correlation_id"
            )

        background_tasks.add_task(_bg)
        return {"ok": True}

    return app


def _build_ws_app() -> FastAPI:
    """App exposing a WS endpoint that reports the bound correlation id."""
    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        await ws.send_json(
            {
                "ctx_cid": ctx.get_correlation_id(),
                "structlog_cid": structlog.contextvars.get_contextvars().get(
                    "correlation_id"
                ),
            }
        )
        await ws.close()

    return app


@pytest.fixture
async def http_client():
    app = _build_http_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestStructlogContextvarsBinding:
    """The raw-ASGI middleware must bind structlog contextvars (parity with
    the BaseHTTPMiddleware variant), in addition to the legacy ctx."""

    async def test_header_propagates_into_structlog_contextvars(
        self, http_client: AsyncClient
    ):
        cid = "structlog-binding-abc-123"
        resp = await http_client.get("/inspect", headers={"X-Correlation-Id": cid})
        assert resp.status_code == 200
        body = resp.json()
        assert body["structlog_cid"] == cid
        assert body["ctx_cid"] == cid

    async def test_generated_id_is_bound_to_structlog_contextvars(
        self, http_client: AsyncClient
    ):
        resp = await http_client.get("/inspect")
        body = resp.json()
        # Must be a valid uuid4 (generated, not inherited).
        uuid.UUID(body["structlog_cid"])
        assert body["structlog_cid"] == body["ctx_cid"]

    async def test_request_id_bound_to_both_channels(
        self, http_client: AsyncClient
    ):
        resp = await http_client.get("/inspect")
        body = resp.json()
        assert body["structlog_rid"] is not None
        assert body["structlog_rid"] == body["ctx_rid"]

    async def test_structlog_contextvars_reset_after_request(
        self, http_client: AsyncClient
    ):
        await http_client.get("/inspect", headers={"X-Correlation-Id": "leak-abc"})
        # No leakage into the outer (test) task.
        assert structlog.contextvars.get_contextvars().get("correlation_id") is None
        assert ctx.get_correlation_id() is None


class TestBackgroundTaskCorrelation:
    """Background tasks run after the response is sent; the raw-ASGI
    middleware must keep the binding live so their log lines carry the
    originating correlation id."""

    async def test_background_task_carries_correlation_id(self):
        captured: dict[str, Any] = {}
        app = _build_http_app(captured)
        transport = ASGITransport(app=app)
        cid = "bg-task-correlation-id"
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/background", headers={"X-Correlation-Id": cid})
            assert resp.status_code == 200

        # Background task ran within the awaited app call; both channels
        # observed the request-scoped correlation id.
        assert captured.get("bg_ctx_cid") == cid
        assert captured.get("bg_structlog_cid") == cid

    async def test_background_task_correlation_with_generated_id(self):
        captured: dict[str, Any] = {}
        app = _build_http_app(captured)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/background")
            assert resp.status_code == 200

        bg_cid = captured.get("bg_ctx_cid")
        assert bg_cid is not None
        uuid.UUID(bg_cid)  # well-formed generated id
        assert captured.get("bg_structlog_cid") == bg_cid


class TestWebSocketCorrelation:
    """WebSocket connections must receive correlation-id propagation from
    the handshake headers — the HTTP-only ``BaseHTTPMiddleware`` variant
    cannot do this, which is why the raw-ASGI middleware is the default."""

    def test_websocket_reads_correlation_header(self):
        app = _build_ws_app()
        cid = "ws-handshake-correlation-id"
        client = TestClient(app)
        with client.websocket_connect("/ws", headers={"X-Correlation-Id": cid}) as ws:
            data = ws.receive_json()
        assert data["ctx_cid"] == cid
        assert data["structlog_cid"] == cid

    def test_websocket_generates_id_when_header_missing(self):
        app = _build_ws_app()
        client = TestClient(app)
        with client.websocket_connect("/ws") as ws:
            data = ws.receive_json()
        # Generated id, present on both channels and identical.
        assert data["ctx_cid"] is not None
        uuid.UUID(data["ctx_cid"])
        assert data["structlog_cid"] == data["ctx_cid"]

    def test_websocket_unsafe_header_is_replaced(self):
        app = _build_ws_app()
        client = TestClient(app)
        # Control characters / oversized values must be discarded and
        # regenerated rather than echoed verbatim.
        with client.websocket_connect(
            "/ws", headers={"X-Correlation-Id": "x" * 10_000}
        ) as ws:
            data = ws.receive_json()
        assert data["ctx_cid"] is not None
        assert len(data["ctx_cid"]) <= 128

    def test_websocket_context_reset_after_disconnect(self):
        app = _build_ws_app()
        client = TestClient(app)
        with client.websocket_connect(
            "/ws", headers={"X-Correlation-Id": "ws-leak-check"}
        ):
            pass
        # The WS connection's bindings must not leak into the next client.
        assert ctx.get_correlation_id() is None
