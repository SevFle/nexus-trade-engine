"""Unit tests for the client-id-keyed ConnectionManager facade (SEV-275).

Exercises the ``connect`` / ``disconnect`` / ``send_personal`` /
``broadcast_all`` convenience API layered on top of
:class:`engine.api.ws.connection_manager.ConnectionManager`, together with
the heartbeat cleanup and auto-cleanup-on-dead-socket behaviour.

These tests are fully framework-agnostic: they talk to a tiny
:class:`WebSocketLike` :class:`~typing.Protocol` implemented by a
``FakeWebSocket`` double, so no live server (and no starlette/uvicorn
runtime) is required.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Protocol

import pytest

from engine.api.ws.connection_manager import ConnectionManager
from engine.api.ws.exceptions import QueueFullError
from engine.api.ws.protocol import EventMessage

# ---------------------------------------------------------------------------
# Framework-agnostic WebSocket double
# ---------------------------------------------------------------------------


class WebSocketLike(Protocol):
    """Structural type a ConnectionManager interacts with.

    Mirrors the subset of :class:`starlette.WebSocket`'s surface that the
    manager actually calls (``send_json`` / ``close``). Using a Protocol
    keeps these tests decoupled from any concrete web framework.
    """

    async def send_json(self, payload: dict[str, Any]) -> None: ...

    async def close(self, code: int = ..., reason: str = ...) -> None: ...


class FakeWebSocket:
    """In-memory WebSocket stand-in.

    Records every outbound payload and optionally simulates a socket that
    is already closed (``fail_on_send``) so dead-connection cleanup can be
    exercised without a real network peer.
    """

    def __init__(self, *, fail_on_send: bool = False) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self.close_code: int | None = None
        self.close_reason: str | None = None
        self.fail_on_send = fail_on_send

    async def send_json(self, payload: dict[str, Any]) -> None:
        if self.fail_on_send:
            raise RuntimeError("simulated closed socket")
        self.sent.append(payload)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True
        self.close_code = code
        self.close_reason = reason


async def _drain(steps: int = 12) -> None:
    """Yield to the loop so background sender-loop tasks flush their queues."""
    for _ in range(steps):
        await asyncio.sleep(0)


def _events(ws: FakeWebSocket) -> list[dict[str, Any]]:
    """Return only ``event`` payloads delivered to ``ws`` (skip acks)."""
    return [m for m in ws.sent if m.get("type") == "event"]


async def _wait_for(predicate, *, timeout: float = 2.0, step: float = 0.01) -> bool:
    """Poll ``predicate`` until it is truthy or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return predicate()


# ---------------------------------------------------------------------------
# Connect facade
# ---------------------------------------------------------------------------


class TestConnect:
    async def test_connect_registers_client_and_sends_ack(self):
        manager = ConnectionManager(heartbeat_interval=999.0)
        ws: WebSocketLike = FakeWebSocket()

        client_id = await manager.connect(ws, client_id="alice")

        # connect() returns the opaque id that keys the connection table.
        assert isinstance(client_id, str) and client_id
        assert manager.connection_count == 1
        assert manager.get_connection(client_id) is not None

        # connect() must acknowledge the connection back to the client.
        await _drain()
        acks = [m for m in ws.sent if m.get("type") == "ack"]  # type: ignore[attr-defined]
        assert len(acks) == 1
        assert acks[0]["status"] == "ok"

        await manager.disconnect(client_id)


# ---------------------------------------------------------------------------
# Targeted personal delivery
# ---------------------------------------------------------------------------


class TestSendPersonal:
    async def test_send_personal_delivers_to_target_only(self):
        manager = ConnectionManager(heartbeat_interval=999.0)
        ws_a: WebSocketLike = FakeWebSocket()
        ws_b: WebSocketLike = FakeWebSocket()
        cid_a = await manager.connect(ws_a, "alice")
        cid_b = await manager.connect(ws_b, "bob")
        await _drain()  # flush the connect acks

        msg = EventMessage(channel="orders", room="user:alice", payload={"v": 1})

        # Targeted send reports delivery and reaches only the intended client.
        assert await manager.send_personal(cid_a, msg) is True
        await _drain()
        assert any(m["payload"] == {"v": 1} for m in _events(ws_a))  # type: ignore[attr-defined]
        assert _events(ws_b) == []  # type: ignore[attr-defined]

        # An unknown client id is reported as not-delivered (no raise).
        assert await manager.send_personal("does-not-exist", msg) is False

        await manager.disconnect(cid_a)
        await manager.disconnect(cid_b)


# ---------------------------------------------------------------------------
# Fan-out to every client
# ---------------------------------------------------------------------------


class TestBroadcastAll:
    async def test_broadcast_all_delivers_to_every_client(self):
        manager = ConnectionManager(heartbeat_interval=999.0)
        sockets: list[WebSocketLike] = [FakeWebSocket() for _ in range(3)]
        cids = [await manager.connect(ws, f"u{i}") for i, ws in enumerate(sockets)]
        await _drain()

        msg = EventMessage(channel="portfolio", room="global", payload={"px": 42})
        delivered = await manager.broadcast_all(msg)
        assert delivered == 3
        await _drain()

        for ws in sockets:
            events = _events(ws)  # type: ignore[arg-type]
            assert len(events) == 1
            assert events[0]["payload"] == {"px": 42}

        for cid in cids:
            await manager.disconnect(cid)

    async def test_broadcast_to_empty_returns_zero(self):
        """Edge case: broadcasting with zero clients is a safe no-op."""
        manager = ConnectionManager()
        assert manager.connection_count == 0

        delivered = await manager.broadcast_all(EventMessage(channel="portfolio", room="global"))
        assert delivered == 0
        assert manager.connection_count == 0


# ---------------------------------------------------------------------------
# Disconnect lifecycle
# ---------------------------------------------------------------------------


class TestDisconnect:
    async def test_disconnect_removes_client(self):
        manager = ConnectionManager(heartbeat_interval=999.0)
        ws: WebSocketLike = FakeWebSocket()
        cid = await manager.connect(ws, "alice")
        assert manager.connection_count == 1

        await manager.disconnect(cid)

        assert manager.connection_count == 0
        assert manager.get_connection(cid) is None
        # After removal, personal sends are refused rather than raising.
        assert await manager.send_personal(cid, EventMessage(channel="x", room="x")) is False

    async def test_double_disconnect_is_noop(self):
        """Edge case: disconnecting twice / unknown ids must not raise."""
        manager = ConnectionManager(heartbeat_interval=999.0)
        ws: WebSocketLike = FakeWebSocket()
        cid = await manager.connect(ws, "alice")

        await manager.disconnect(cid)
        # Second disconnect of an already-removed id is a harmless no-op.
        await manager.disconnect(cid)
        assert manager.connection_count == 0
        # Disconnecting an id that was never registered is equally safe.
        await manager.disconnect("never-registered")
        assert manager.connection_count == 0


# ---------------------------------------------------------------------------
# Heartbeat-driven cleanup
# ---------------------------------------------------------------------------


class TestHeartbeatCleanup:
    async def test_heartbeat_removes_stale_connection(self):
        manager = ConnectionManager(heartbeat_interval=0.02)
        ws: WebSocketLike = FakeWebSocket()
        cid = await manager.connect(ws, "alice")
        assert manager.connection_count == 1

        # Backdate last_seen so the connection looks unresponsive; the
        # heartbeat loop should evict it on its next tick.
        info = manager.get_connection(cid)
        assert info is not None
        info.last_seen = time.monotonic() - 1000.0

        removed = await _wait_for(lambda: manager.connection_count == 0)
        assert removed, "heartbeat did not evict the stale connection in time"
        assert manager.get_connection(cid) is None


# ---------------------------------------------------------------------------
# Auto-cleanup when the underlying socket dies
# ---------------------------------------------------------------------------


class TestAutoCleanupOnDeadSocket:
    async def test_dead_socket_is_auto_unregistered(self):
        manager = ConnectionManager(heartbeat_interval=999.0)
        ws: WebSocketLike = FakeWebSocket(fail_on_send=True)
        cid = await manager.connect(ws, "alice")

        # The connect() ack delivery fails because the socket is closed.
        # The sender loop must auto-unregister the connection instead of
        # leaving a zombie entry that future broadcasts keep targeting.
        cleaned = await _wait_for(lambda: manager.connection_count == 0)
        assert cleaned, "dead socket was not auto-cleaned up"
        assert manager.get_connection(cid) is None

    async def test_full_queue_drops_message_without_killing_connection(self):
        """A saturated send-queue raises QueueFullError but keeps the client."""
        manager = ConnectionManager(send_queue_size=1, heartbeat_interval=999.0)
        ws: WebSocketLike = FakeWebSocket()
        cid = await manager.connect(ws, "alice")
        await _drain()

        msg = EventMessage(channel="portfolio", room="global")
        # Fill the single-slot queue without draining, then overflow it.
        await manager.send(cid, msg)  # fills the slot
        with pytest.raises(QueueFullError):
            await manager.send(cid, msg)  # overflows -> QueueFullError

        # The connection survives the backpressure event.
        assert manager.connection_count == 1
        assert manager.get_connection(cid) is not None

        await manager.disconnect(cid)
