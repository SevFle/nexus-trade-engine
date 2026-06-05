"""Unit tests for engine.api.websocket.connection_manager_v2 (SEV-275)."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress

import pytest

from engine.api.websocket.channels import for_market, for_orders, for_portfolio
from engine.api.websocket.connection_manager_v2 import ConnectionManagerV2
from engine.api.websocket.models import Principal


class _FakeWS:
    """In-memory stand-in for a Starlette WebSocket.

    Captures every send_json call; allows the test driver to inject
    inbound messages; supports ``close`` and ``accept``.
    """

    def __init__(self, *, fail_after: int | None = None) -> None:
        self.id = uuid.uuid4()
        self.sent: list[dict] = []
        self.closed: list[tuple[int, str | None]] = []
        self._fail_after = fail_after
        self._sent_count = 0
        self._inbound: asyncio.Queue[dict | None] = asyncio.Queue()
        self.scope: dict = {"subprotocols": []}
        self.headers: dict[str, str] = {}
        self.query_params: dict[str, str] = {}

    async def accept(self, subprotocol: str | None = None) -> None:
        return None

    async def send_json(self, payload: dict) -> None:
        self._sent_count += 1
        if self._fail_after is not None and self._sent_count > self._fail_after:
            raise RuntimeError("simulated send failure")
        self.sent.append(payload)

    async def receive_json(self) -> dict:
        item = await self._inbound.get()
        if item is None:
            raise RuntimeError("disconnected")
        return item

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed.append((code, reason))

    async def push_inbound(self, msg: dict | None) -> None:
        await self._inbound.put(msg)

    def __hash__(self) -> int:
        return hash(self.id)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _FakeWS) and other.id == self.id


def _principal(role: str = "user") -> Principal:
    return Principal(
        user_id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        role=role,
        scopes=frozenset({"portfolio:read", "orders:read", "market:read"}),
    )


@pytest.fixture
def manager() -> ConnectionManagerV2:
    return ConnectionManagerV2(queue_capacity=8, slow_consumer_grace=1)


# ---------------------------------------------------------------------------
# register / disconnect
# ---------------------------------------------------------------------------
class TestRegister:
    async def test_register_increments_count(self, manager):
        ws = _FakeWS()
        conn = await manager.register(ws, _principal())
        assert manager.total_connections() == 1
        assert manager.user_connection_count(conn.principal.user_id) == 1

    async def test_disconnect_removes_connection(self, manager):
        ws = _FakeWS()
        conn = await manager.register(ws, _principal())
        await manager.disconnect(conn)
        assert manager.total_connections() == 0
        assert conn.closed is True
        assert ws.closed  # underlying socket was closed

    async def test_disconnect_is_idempotent(self, manager):
        ws = _FakeWS()
        conn = await manager.register(ws, _principal())
        await manager.disconnect(conn)
        await manager.disconnect(conn)
        assert len(ws.closed) == 1

    async def test_multiple_connections_per_user(self, manager):
        p = _principal()
        c1 = await manager.register(_FakeWS(), p)
        c2 = await manager.register(_FakeWS(), p)
        assert manager.user_connection_count(p.user_id) == 2
        await manager.disconnect(c1)
        assert manager.user_connection_count(p.user_id) == 1


# ---------------------------------------------------------------------------
# subscribe / unsubscribe
# ---------------------------------------------------------------------------
class TestSubscribe:
    async def test_subscribe_increases_channel_membership(self, manager):
        conn = await manager.register(_FakeWS(), _principal())
        c = for_market("AAPL")
        assert await manager.subscribe(conn, c) is True
        assert manager.total_subscriptions() == 1

    async def test_subscribe_idempotent(self, manager):
        conn = await manager.register(_FakeWS(), _principal())
        c = for_market("AAPL")
        assert await manager.subscribe(conn, c) is True
        assert await manager.subscribe(conn, c) is False
        assert manager.total_subscriptions() == 1

    async def test_unsubscribe_removes_membership(self, manager):
        conn = await manager.register(_FakeWS(), _principal())
        c = for_market("AAPL")
        await manager.subscribe(conn, c)
        assert await manager.unsubscribe(conn, c) is True
        assert manager.total_subscriptions() == 0

    async def test_unsubscribe_unknown_returns_false(self, manager):
        conn = await manager.register(_FakeWS(), _principal())
        assert await manager.unsubscribe(conn, for_market("AAPL")) is False

    async def test_disconnect_clears_subscriptions(self, manager):
        conn = await manager.register(_FakeWS(), _principal())
        await manager.subscribe(conn, for_market("AAPL"))
        await manager.subscribe(conn, for_portfolio(conn.principal.user_id))
        await manager.disconnect(conn)
        assert manager.total_subscriptions() == 0


# ---------------------------------------------------------------------------
# publish_to_channel — fan-out semantics
# ---------------------------------------------------------------------------
class TestPublishToChannel:
    async def test_only_subscribed_recipients_receive(self, manager):
        p = _principal()
        ws_a, ws_b = _FakeWS(), _FakeWS()
        ca = await manager.register(ws_a, p)
        cb = await manager.register(ws_b, p)
        await manager.subscribe(ca, for_market("AAPL"))
        # cb subscribed to a different symbol — must not see AAPL ticks
        await manager.subscribe(cb, for_market("MSFT"))

        delivered = await manager.publish_to_channel(
            for_market("AAPL"), {"type": "market.tick", "symbol": "AAPL"}
        )
        # Frame sits in the outbound queue — spawn_sender wasn't
        # called so it stays there until we drain.
        assert delivered == 1
        assert ca.outbound.qsize() == 1
        assert cb.outbound.qsize() == 0

    async def test_no_subscribers_returns_zero(self, manager):
        n = await manager.publish_to_channel(
            for_market("AAPL"), {"type": "market.tick"}
        )
        assert n == 0

    async def test_cross_user_isolation(self, manager):
        """User A subscribing to its portfolio channel does NOT see
        user B's portfolio events."""
        pa, pb = _principal(), _principal()
        a = await manager.register(_FakeWS(), pa)
        b = await manager.register(_FakeWS(), pb)
        await manager.subscribe(a, for_portfolio(pa.user_id))
        await manager.subscribe(b, for_portfolio(pb.user_id))

        # Publish to user A's portfolio channel
        delivered = await manager.publish_to_channel(
            for_portfolio(pa.user_id),
            {"type": "portfolio.updated", "user_id": str(pa.user_id)},
        )
        assert delivered == 1
        assert a.outbound.qsize() == 1
        assert b.outbound.qsize() == 0  # isolation!

    async def test_publish_to_user_helper(self, manager):
        """publish_to_user targets only the user's connections."""
        pa = _principal()
        a = await manager.register(_FakeWS(), pa)
        await manager.subscribe(a, for_portfolio(pa.user_id))

        delivered = await manager.publish_to_user(
            pa.user_id,
            for_portfolio(pa.user_id),
            {"type": "portfolio.updated"},
        )
        assert delivered == 1

    async def test_publish_to_user_skips_unsubscribed(self, manager):
        pa = _principal()
        a = await manager.register(_FakeWS(), pa)
        # No subscription — nothing delivered.
        delivered = await manager.publish_to_user(
            pa.user_id,
            for_portfolio(pa.user_id),
            {"type": "portfolio.updated"},
        )
        assert delivered == 0


# ---------------------------------------------------------------------------
# Backpressure
# ---------------------------------------------------------------------------
class TestBackpressure:
    async def test_queue_overflow_marks_slow_consumer(self, manager):
        ws = _FakeWS()
        conn = await manager.register(ws, _principal())
        cap = 8
        # Fill the queue exactly.
        for i in range(cap):
            await manager.publish_to_channel(
                for_market("AAPL"), {"type": "market.tick", "i": i}
            ) if False else None
            assert conn.outbound.put_nowait({"i": i}) is None
        # One more — should drop, but the grace budget keeps us alive.
        await manager.publish_to_channel(
            for_market("AAPL"), {"type": "market.tick", "i": cap}
        )
        # First overflow triggers grace, drops an old frame, replaces.
        assert conn.dropped == 1

        # Second overflow exhausts grace — disconnect should be scheduled.
        with suppress(Exception):
            await manager.publish_to_channel(
                for_market("AAPL"), {"type": "market.tick", "i": cap + 1}
            )
        # The disconnect is asynchronous — give it a tick to land.
        await asyncio.sleep(0.05)
        assert conn.closed is True

    async def test_disconnect_drops_pending_frames(self, manager):
        ws = _FakeWS()
        conn = await manager.register(ws, _principal())
        for i in range(4):
            conn.outbound.put_nowait({"i": i})
        await manager.disconnect(conn)
        # Queue may still hold items, but conn.closed is True.
        assert conn.closed is True


# ---------------------------------------------------------------------------
# Sender task — happy path
# ---------------------------------------------------------------------------
class TestSenderTask:
    async def test_sender_drains_queue(self, manager):
        ws = _FakeWS()
        conn = await manager.register(ws, _principal())
        await manager.spawn_sender(conn)
        for i in range(3):
            await conn.outbound.put({"type": "ping", "i": i})
        # Wait until the queue drains.
        for _ in range(50):
            if ws.sent:
                break
            await asyncio.sleep(0.02)
        assert len(ws.sent) == 3
        await manager.disconnect(conn)


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------
class TestShutdown:
    async def test_shutdown_broadcasts_and_closes(self, manager):
        ws_a, ws_b = _FakeWS(), _FakeWS()
        c1 = await manager.register(ws_a, _principal())
        c2 = await manager.register(ws_b, _principal())
        await manager.spawn_sender(c1)
        await manager.spawn_sender(c2)

        await manager.shutdown_all(reason="test")
        assert manager.total_connections() == 0
        assert c1.closed and c2.closed

    async def test_subsequent_register_rejected(self, manager):
        await manager.shutdown_all(reason="test")
        ws = _FakeWS()
        with pytest.raises(RuntimeError, match="shutting_down"):
            await manager.register(ws, _principal())


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------
class TestSnapshot:
    async def test_snapshot_shape(self, manager):
        p = _principal()
        c = await manager.register(_FakeWS(), p)
        await manager.subscribe(c, for_portfolio(p.user_id))
        await manager.subscribe(c, for_orders(p.user_id))
        snap = manager.snapshot()
        assert snap["connections"] == 1
        assert snap["subscriptions"] == 2
        assert snap["by_family"]["portfolio"] == 1
        assert snap["by_family"]["orders"] == 1
        assert snap["shutting_down"] is False
