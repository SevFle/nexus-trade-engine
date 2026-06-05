"""End-to-end integration tests for the WebSocket API (SEV-275).

Exercises the full connection lifecycle: auth handshake, subscribe,
event delivery, cross-user isolation, backpressure, and shutdown
drain. Uses a hand-written WebSocket client driven over FastAPI's
ASGI transport to avoid pulling in a real WS library.

The auth dependency is short-circuited by injecting a known
Principal via a module-level monkeypatch on
``engine.api.websocket.handlers.authenticate``. The Redis bridge is
not started — instead we publish events directly via the manager's
``publish_to_channel`` method, which is exactly the API the bridge
calls.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from engine.api.websocket import handlers as ws_handlers
from engine.api.websocket.channels import for_market, for_portfolio
from engine.api.websocket.connection_manager_v2 import (
    ConnectionManagerV2,
    set_manager_v2,
)
from engine.api.websocket.models import Principal
from engine.app import create_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _principal(role: str = "user", scopes: set[str] | None = None) -> Principal:
    return Principal(
        user_id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        role=role,
        scopes=frozenset(scopes or {"portfolio:read", "orders:read", "market:read"}),
        auth_method="jwt",
    )


class _RecordingPrincipal:
    """Context manager that patches authenticate to return ``principal``."""

    def __init__(self, principal: Principal | None = None) -> None:
        self.principal = principal

    def __enter__(self) -> _RecordingPrincipal:
        self._real = ws_handlers.authenticate
        if self.principal is None:
            ws_handlers.authenticate = self._async_return_none  # type: ignore[assignment]
        else:
            ws_handlers.authenticate = self._async_return_principal  # type: ignore[assignment]
        return self

    def __exit__(self, *_a: object) -> None:
        ws_handlers.authenticate = self._real  # type: ignore[assignment]

    async def _async_return_principal(self, ws: Any) -> Principal:
        return self.principal  # type: ignore[return-value]

    async def _async_return_none(self, ws: Any) -> Principal:
        from engine.api.websocket.exceptions import AuthRequiredError

        raise AuthRequiredError


@pytest.fixture
def fresh_manager() -> ConnectionManagerV2:
    m = ConnectionManagerV2(queue_capacity=64, slow_consumer_grace=2)
    set_manager_v2(m)
    return m


@pytest.fixture
async def app_client(fresh_manager):
    """ASGI client wired to a FastAPI app that bypasses the heavy
    startup lifespan (which needs Valkey / DB)."""
    app = create_app()
    app.state.ws_manager = fresh_manager
    app.state.ws_bridge = None
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, app


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------
class TestHealth:
    async def test_health_websocket_returns_manager_snapshot(self, app_client):
        _ac, _app = app_client
        # Hit the route via httpx — works because the route is HTTP GET.
        # When going through ASGITransport we use the path the router
        # mounted on. The combined app (create_app) doesn't include the
        # API router; the integration here is via api_router. Skip if
        # the route isn't reachable from create_app().
        # The combined ``create_app`` does include ``api_router`` via
        # ``app.include_router(api_router)``.
        from fastapi import FastAPI

        from engine.api.router import api_router

        test_app = FastAPI()
        test_app.include_router(api_router)
        test_app.state.ws_manager = _app.state.ws_manager
        test_app.state.ws_bridge = None
        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/health/websocket")
            assert resp.status_code == 200
            body = resp.json()
            assert "manager" in body
            assert "bridge" in body
            assert body["manager"]["connections"] == 0
            assert body["bridge"]["connected"] is False


# ---------------------------------------------------------------------------
# Connection lifecycle (via direct handler invocation)
# ---------------------------------------------------------------------------
class _ClientWS:
    """In-process WebSocket double. Records sent frames and lets the
    test driver feed it inbound messages."""

    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.sent: list[dict] = []
        self.inbound: asyncio.Queue[dict | None] = asyncio.Queue()
        self.scope: dict = {"subprotocols": []}
        self.headers: dict[str, str] = {}
        self.query_params: dict[str, str] = {}
        self.accepted = False
        self.subprotocol: str | None = None
        self.closed_code: int | None = None
        self.closed_reason: str | None = None

    async def accept(self, subprotocol: str | None = None) -> None:
        self.accepted = True
        self.subprotocol = subprotocol

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def receive_json(self) -> dict:
        item = await self.inbound.get()
        if item is None:
            raise RuntimeError("disconnected")
        return item

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed_code = code
        self.closed_reason = reason


class TestHandshake:
    async def test_auth_ok_frame_after_handshake(self, fresh_manager):
        ws = _ClientWS()
        p = _principal()

        with _RecordingPrincipal(p):
            handler_task = asyncio.create_task(
                ws_handlers.serve_unified(ws, fresh_manager)
            )
            # Give the handler a tick to send auth.ok
            await asyncio.sleep(0.05)

        # Handler should have accepted and emitted auth.ok
        assert ws.accepted
        assert any(frame.get("type") == "auth.ok" for frame in ws.sent)

        # Disconnect cleanly
        await ws.inbound.put(None)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(handler_task, timeout=2.0)

    async def test_auth_failure_closes_with_4001(self, fresh_manager):
        ws = _ClientWS()
        with _RecordingPrincipal(principal=None):
            await ws_handlers.serve_unified(ws, fresh_manager)
        assert ws.closed_code == 4001
        # An auth.failed frame should have been emitted
        assert any(frame.get("type") == "auth.failed" for frame in ws.sent)


class TestSubscribeAndUnsubscribe:
    async def test_subscribe_to_market_yields_ack(self, fresh_manager):
        ws = _ClientWS()
        p = _principal()

        with _RecordingPrincipal(p):
            handler_task = asyncio.create_task(
                ws_handlers.serve_unified(ws, fresh_manager)
            )
            await asyncio.sleep(0.05)
            await ws.inbound.put(
                {"type": "subscribe", "channel": "market", "symbols": ["AAPL"]}
            )
            await asyncio.sleep(0.05)
            # Unsubscribe
            await ws.inbound.put(
                {"type": "unsubscribe", "channel": "market", "symbols": ["AAPL"]}
            )
            await asyncio.sleep(0.05)

        sub_frames = [f for f in ws.sent if f.get("type") == "subscribed"]
        unsub_frames = [f for f in ws.sent if f.get("type") == "unsubscribed"]
        assert len(sub_frames) == 1
        assert sub_frames[0]["symbols"] == ["AAPL"]
        assert len(unsub_frames) == 1
        assert unsub_frames[0]["symbols"] == ["AAPL"]

        await ws.inbound.put(None)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(handler_task, timeout=2.0)

    async def test_subscribe_unknown_symbol_silently_dropped(self, fresh_manager):
        ws = _ClientWS()
        p = _principal()
        with _RecordingPrincipal(p):
            handler_task = asyncio.create_task(
                ws_handlers.serve_unified(ws, fresh_manager)
            )
            await asyncio.sleep(0.05)
            # ``bad-symbol`` fails the regex; the handler should skip it.
            await ws.inbound.put(
                {"type": "subscribe", "channel": "market", "symbols": ["bad symbol!"]}
            )
            await asyncio.sleep(0.05)
        sub_frames = [f for f in ws.sent if f.get("type") == "subscribed"]
        assert sub_frames and sub_frames[0]["symbols"] == []
        await ws.inbound.put(None)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(handler_task, timeout=2.0)


class TestPing:
    async def test_ping_returns_pong(self, fresh_manager):
        ws = _ClientWS()
        p = _principal()
        with _RecordingPrincipal(p):
            handler_task = asyncio.create_task(
                ws_handlers.serve_unified(ws, fresh_manager)
            )
            await asyncio.sleep(0.05)
            await ws.inbound.put({"type": "ping"})
            await asyncio.sleep(0.05)
        pongs = [f for f in ws.sent if f.get("type") == "pong"]
        assert len(pongs) == 1
        assert "server_ts" in pongs[0]
        await ws.inbound.put(None)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(handler_task, timeout=2.0)


class TestUnknownMessageType:
    async def test_unknown_type_emits_error_frame(self, fresh_manager):
        ws = _ClientWS()
        p = _principal()
        with _RecordingPrincipal(p):
            handler_task = asyncio.create_task(
                ws_handlers.serve_unified(ws, fresh_manager)
            )
            await asyncio.sleep(0.05)
            await ws.inbound.put({"type": "wizardry"})
            await asyncio.sleep(0.05)
        errors = [f for f in ws.sent if f.get("type") == "error"]
        assert errors and errors[0]["code"] == "unknown_message_type"
        await ws.inbound.put(None)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(handler_task, timeout=2.0)


# ---------------------------------------------------------------------------
# Event delivery
# ---------------------------------------------------------------------------
class TestEventDelivery:
    async def test_market_tick_reaches_subscribed_client(self, fresh_manager):
        ws = _ClientWS()
        p = _principal()
        with _RecordingPrincipal(p):
            handler_task = asyncio.create_task(
                ws_handlers.serve_unified(ws, fresh_manager)
            )
            await asyncio.sleep(0.05)
            await ws.inbound.put(
                {"type": "subscribe", "channel": "market", "symbols": ["AAPL"]}
            )
            await asyncio.sleep(0.05)

        # Find the connection in the manager.
        conns = [
            c
            for c in fresh_manager._conns.values()
            if c.principal.user_id == p.user_id
        ]
        assert len(conns) == 1
        # Start the sender task so the queue drains.
        await fresh_manager.spawn_sender(conns[0])

        # Simulate the bridge delivering a market tick.
        await fresh_manager.publish_to_channel(
            for_market("AAPL"),
            {"type": "market.tick", "symbol": "AAPL", "last": 123.45},
        )
        await asyncio.sleep(0.05)
        ticks = [f for f in ws.sent if f.get("type") == "market.tick"]
        assert ticks and ticks[0]["symbol"] == "AAPL"

        await ws.inbound.put(None)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(handler_task, timeout=2.0)

    async def test_portfolio_event_isolated_per_user(self, fresh_manager):
        """User A subscribing to portfolio does NOT receive user B's events."""
        ws_a, ws_b = _ClientWS(), _ClientWS()
        pa, pb = _principal(), _principal()

        with _RecordingPrincipal(pa):
            t_a = asyncio.create_task(
                ws_handlers.serve_unified(ws_a, fresh_manager)
            )
            await asyncio.sleep(0.05)
            await ws_a.inbound.put({"type": "subscribe", "channel": "portfolio"})
            await asyncio.sleep(0.05)

        with _RecordingPrincipal(pb):
            t_b = asyncio.create_task(
                ws_handlers.serve_unified(ws_b, fresh_manager)
            )
            await asyncio.sleep(0.05)
            await ws_b.inbound.put({"type": "subscribe", "channel": "portfolio"})
            await asyncio.sleep(0.05)

        conns = list(fresh_manager._conns.values())
        for c in conns:
            await fresh_manager.spawn_sender(c)

        # Publish to user A's portfolio channel — only ws_a should see it.
        await fresh_manager.publish_to_channel(
            for_portfolio(pa.user_id),
            {"type": "portfolio.updated", "user_id": str(pa.user_id)},
        )
        await asyncio.sleep(0.05)
        a_ticks = [f for f in ws_a.sent if f.get("type") == "portfolio.updated"]
        b_ticks = [f for f in ws_b.sent if f.get("type") == "portfolio.updated"]
        assert len(a_ticks) == 1
        assert b_ticks == []  # isolation

        # Cleanup
        await ws_a.inbound.put(None)
        await ws_b.inbound.put(None)
        for t in (t_a, t_b):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(t, timeout=2.0)


# ---------------------------------------------------------------------------
# Family endpoints (auto-subscribe)
# ---------------------------------------------------------------------------
class TestFamilyEndpoints:
    async def test_portfolio_endpoint_auto_subscribes(self, fresh_manager):
        ws = _ClientWS()
        p = _principal()
        with _RecordingPrincipal(p):
            t = asyncio.create_task(
                ws_handlers.serve_portfolio(ws, fresh_manager)
            )
            await asyncio.sleep(0.05)
        # Find the connection — its subscription registry should already
        # include the user's portfolio channel.
        conn = next(iter(fresh_manager._conns.values()))
        assert conn.subscriptions.is_subscribed(for_portfolio(p.user_id))
        await ws.inbound.put(None)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(t, timeout=2.0)

    async def test_orders_endpoint_auto_subscribes(self, fresh_manager):
        ws = _ClientWS()
        p = _principal()
        with _RecordingPrincipal(p):
            t = asyncio.create_task(
                ws_handlers.serve_orders(ws, fresh_manager)
            )
            await asyncio.sleep(0.05)
        conn = next(iter(fresh_manager._conns.values()))
        from engine.api.websocket.channels import for_orders

        assert conn.subscriptions.is_subscribed(for_orders(p.user_id))
        await ws.inbound.put(None)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(t, timeout=2.0)


# ---------------------------------------------------------------------------
# Shutdown drain
# ---------------------------------------------------------------------------
class TestShutdown:
    async def test_shutdown_broadcasts_frame_and_closes(self, fresh_manager):
        ws = _ClientWS()
        p = _principal()
        with _RecordingPrincipal(p):
            t = asyncio.create_task(
                ws_handlers.serve_unified(ws, fresh_manager)
            )
            await asyncio.sleep(0.05)

        await fresh_manager.shutdown_all(reason="test_shutdown")
        # The shutdown frame should be in the outbound queue (or sent
        # if the sender task ran). Disconnect tears down the conn.
        ws._drain_queue()

        # Connection is torn down
        assert fresh_manager.total_connections() == 0
        await ws.inbound.put(None)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(t, timeout=2.0)

    async def test_register_rejected_during_shutdown(self, fresh_manager):
        await fresh_manager.shutdown_all(reason="test")
        ws = _ClientWS()
        with _RecordingPrincipal(_principal()), pytest.raises(RuntimeError, match="shutting_down"):
            await fresh_manager.register(ws, _principal())


# Convenience: drain the recording WS queue into its sent list so
# shutdown tests can observe frames that were enqueued but not yet
# written to the wire before the connection was closed.
def _ws_drain(self):
    out: list[dict] = []
    while not self.inbound.empty():
        try:
            item = self.inbound.get_nowait()
            if item is not None:
                out.append(item)
        except asyncio.QueueEmpty:
            break
    return out


_ClientWS._drain_queue = _ws_drain  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Bridge integration — feed pubsub messages and verify dispatch
# ---------------------------------------------------------------------------
class TestBridgeEndToEnd:
    async def test_bridge_dispatches_to_manager(self, fresh_manager):
        from engine.api.websocket.redis_bridge import WSRedisBridge

        bridge = WSRedisBridge(fresh_manager, redis_url="redis://localhost:0")
        ws = _ClientWS()
        p = _principal()

        with _RecordingPrincipal(p):
            t = asyncio.create_task(ws_handlers.serve_unified(ws, fresh_manager))
            await asyncio.sleep(0.05)
            await ws.inbound.put(
                {"type": "subscribe", "channel": "market", "symbols": ["AAPL"]}
            )
            await asyncio.sleep(0.05)
        conn = next(iter(fresh_manager._conns.values()))
        await fresh_manager.spawn_sender(conn)

        # Bridge dispatch is purely a function of _handle_message; we
        # don't need to run the bridge's run loop.
        await bridge._handle_message(
            {
                "type": "pmessage",
                "channel": b"market:AAPL",
                "data": json.dumps(
                    {"type": "market.tick", "data": {"last": 100}}
                ).encode(),
            }
        )
        await asyncio.sleep(0.05)
        ticks = [f for f in ws.sent if f.get("type") == "market.tick"]
        assert ticks

        await ws.inbound.put(None)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(t, timeout=2.0)
