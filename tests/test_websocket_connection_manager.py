"""Unit tests for the channel-based :class:`ConnectionManager` (SEV-298).

These tests cover the new pub/sub WebSocket connection manager in
``engine/api/websocket/manager.py`` — distinct from the legacy
``UserTopicManager`` already covered in ``test_websocket_manager.py``.

Scope (per issue spec):
  1. connect/disconnect lifecycle
  2. subscribe/unsubscribe channel membership + broadcast_to delivery
  3. broadcast_all reaches every connection
  4. reconnect with the same connection_id replaces the old socket
  5. auto-cleanup of dead connections when a send fails
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import WebSocketDisconnect

from engine.api.websocket.manager import ConnectionManager


class _FakeWS:
    """Minimal async WebSocket stand-in.

    Captures every ``send_json`` payload and optionally raises a
    configurable exception to simulate a dead/closing client. The
    connection manager stores sockets as dict *values* keyed by string
    id, so we do not need to be hashable — but we are anyway for
    parity with the other manager's mock.
    """

    def __init__(
        self,
        *,
        fail_with: BaseException | None = None,
        delay: float = 0.0,
        name: str | None = None,
    ) -> None:
        self.sent: list[object] = []
        self.fail_with = fail_with
        self.delay = delay
        self.name = name or str(uuid.uuid4())

    async def send_json(self, payload: object) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append(payload)

    def __hash__(self) -> int:
        return hash(self.name)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _FakeWS) and other.name == self.name

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"_FakeWS(name={self.name!r})"


@pytest.fixture
def manager() -> ConnectionManager:
    return ConnectionManager()


# ---------------------------------------------------------------------------
# 1. connect / disconnect lifecycle
# ---------------------------------------------------------------------------


class TestConnectDisconnect:
    async def test_connect_registers_socket(self, manager):
        ws = _FakeWS()
        await manager.connect("c1", ws)

        assert manager.is_connected("c1")
        assert manager.connection_count == 1
        # The exact socket object is stored.
        assert manager.connections["c1"] is ws

    async def test_connect_multiple_distinct_ids(self, manager):
        a, b, c = _FakeWS(), _FakeWS(), _FakeWS()
        await manager.connect("a", a)
        await manager.connect("b", b)
        await manager.connect("c", c)

        assert manager.connection_count == 3
        assert manager.is_connected("a")
        assert manager.is_connected("b")
        assert manager.is_connected("c")

    async def test_disconnect_removes_socket(self, manager):
        ws = _FakeWS()
        await manager.connect("c1", ws)
        assert manager.is_connected("c1")

        await manager.disconnect("c1")

        assert not manager.is_connected("c1")
        assert manager.connection_count == 0
        assert "c1" not in manager.connections

    async def test_disconnect_is_idempotent_unknown_id(self, manager):
        # Disconnecting an id that was never registered is a silent no-op.
        await manager.disconnect("ghost")
        assert manager.connection_count == 0

    async def test_disconnect_twice_second_is_noop(self, manager):
        ws = _FakeWS()
        await manager.connect("c1", ws)
        await manager.disconnect("c1")
        await manager.disconnect("c1")  # second disconnect
        assert manager.connection_count == 0

    async def test_disconnect_clears_channel_memberships(self, manager):
        ws = _FakeWS()
        await manager.connect("c1", ws)
        await manager.subscribe("c1", "portfolio")
        await manager.subscribe("c1", "alerts")

        await manager.disconnect("c1")

        # No dangling membership, and empty channels are pruned.
        assert not manager.is_subscribed("c1", "portfolio")
        assert not manager.is_subscribed("c1", "alerts")
        assert manager.channel_count == 0

    async def test_disconnect_does_not_affect_other_connections(self, manager):
        a, b = _FakeWS(), _FakeWS()
        await manager.connect("a", a)
        await manager.connect("b", b)

        await manager.disconnect("a")

        assert not manager.is_connected("a")
        assert manager.is_connected("b")
        assert manager.connection_count == 1


# ---------------------------------------------------------------------------
# 2. subscribe / unsubscribe + broadcast_to delivery
# ---------------------------------------------------------------------------


class TestSubscribeUnsubscribe:
    async def test_subscribe_adds_to_channel(self, manager):
        ws = _FakeWS()
        await manager.connect("c1", ws)

        ok = await manager.subscribe("c1", "portfolio")

        assert ok is True
        assert manager.is_subscribed("c1", "portfolio")
        assert "c1" in manager.get_subscribers("portfolio")

    async def test_subscribe_unknown_connection_returns_false(self, manager):
        # Subscribing a connection that isn't registered must fail closed.
        ok = await manager.subscribe("ghost", "portfolio")

        assert ok is False
        assert manager.channel_count == 0  # no orphan channel created
        assert manager.get_subscribers("portfolio") == frozenset()

    async def test_subscribe_is_idempotent(self, manager):
        ws = _FakeWS()
        await manager.connect("c1", ws)

        first = await manager.subscribe("c1", "portfolio")
        second = await manager.subscribe("c1", "portfolio")

        assert first is True
        assert second is True
        assert manager.get_subscribers("portfolio") == frozenset({"c1"})

    async def test_subscribe_multiple_channels(self, manager):
        ws = _FakeWS()
        await manager.connect("c1", ws)
        await manager.subscribe("c1", "portfolio")
        await manager.subscribe("c1", "alerts")

        subs = manager.get_subscriptions("c1")
        assert subs == frozenset({"portfolio", "alerts"})

    async def test_unsubscribe_removes_membership(self, manager):
        ws = _FakeWS()
        await manager.connect("c1", ws)
        await manager.subscribe("c1", "portfolio")

        existed = await manager.unsubscribe("c1", "portfolio")

        assert existed is True
        assert not manager.is_subscribed("c1", "portfolio")

    async def test_unsubscribe_not_a_member_returns_false(self, manager):
        ws = _FakeWS()
        await manager.connect("c1", ws)
        await manager.subscribe("c1", "portfolio")

        existed = await manager.unsubscribe("c1", "alerts")  # different channel

        assert existed is False
        assert manager.is_subscribed("c1", "portfolio")  # untouched

    async def test_unsubscribe_unknown_channel_returns_false(self, manager):
        existed = await manager.unsubscribe("ghost", "nope")
        assert existed is False

    async def test_unsubscribe_prunes_empty_channel(self, manager):
        ws = _FakeWS()
        await manager.connect("c1", ws)
        await manager.subscribe("c1", "portfolio")
        assert manager.channel_count == 1

        await manager.unsubscribe("c1", "portfolio")

        assert manager.channel_count == 0
        assert "portfolio" not in manager.channel_subscriptions


class TestBroadcastToChannel:
    async def test_delivers_only_to_subscribers(self, manager):
        sub, nosub = _FakeWS(), _FakeWS()
        await manager.connect("sub", sub)
        await manager.connect("nosub", nosub)
        await manager.subscribe("sub", "portfolio")

        delivered = await manager.broadcast("portfolio", {"v": 1})

        assert delivered == 1
        assert sub.sent == [{"v": 1}]
        assert nosub.sent == []

    async def test_delivers_to_all_subscribers_of_channel(self, manager):
        a, b, c = _FakeWS(), _FakeWS(), _FakeWS()
        for cid, ws in [("a", a), ("b", b), ("c", c)]:
            await manager.connect(cid, ws)
            await manager.subscribe(cid, "alerts")

        delivered = await manager.broadcast("alerts", {"ping": True})

        assert delivered == 3
        for ws in (a, b, c):
            assert ws.sent == [{"ping": True}]

    async def test_isolates_channels(self, manager):
        portfolio_only = _FakeWS()
        alerts_only = _FakeWS()
        both = _FakeWS()
        await manager.connect("p", portfolio_only)
        await manager.connect("a", alerts_only)
        await manager.connect("b", both)
        await manager.subscribe("p", "portfolio")
        await manager.subscribe("a", "alerts")
        await manager.subscribe("b", "portfolio")
        await manager.subscribe("b", "alerts")

        delivered = await manager.broadcast("portfolio", {"x": 1})

        assert delivered == 2
        assert portfolio_only.sent == [{"x": 1}]
        assert both.sent == [{"x": 1}]
        assert alerts_only.sent == []  # not subscribed to portfolio

    async def test_unknown_channel_returns_zero(self, manager):
        ws = _FakeWS()
        await manager.connect("c1", ws)
        # No subscriptions exist at all.

        delivered = await manager.broadcast("portfolio", {"v": 1})

        assert delivered == 0
        assert ws.sent == []

    async def test_empty_channel_set_returns_zero(self, manager):
        ws = _FakeWS()
        await manager.connect("c1", ws)
        await manager.subscribe("c1", "portfolio")
        await manager.unsubscribe("c1", "portfolio")  # channel now empty/pruned

        delivered = await manager.broadcast("portfolio", {"v": 1})

        assert delivered == 0
        assert ws.sent == []

    async def test_broadcast_returns_count_of_successes_only(self, manager):
        good = _FakeWS()
        bad = _FakeWS(fail_with=RuntimeError("boom"))
        await manager.connect("good", good)
        await manager.connect("bad", bad)
        await manager.subscribe("good", "portfolio")
        await manager.subscribe("bad", "portfolio")

        delivered = await manager.broadcast("portfolio", {"v": 9})

        assert delivered == 1  # only the working socket counts
        assert good.sent == [{"v": 9}]


# ---------------------------------------------------------------------------
# 3. broadcast_all
# ---------------------------------------------------------------------------


class TestBroadcastAll:
    async def test_reaches_every_connection(self, manager):
        a, b, c = _FakeWS(), _FakeWS(), _FakeWS()
        await manager.connect("a", a)
        await manager.connect("b", b)
        await manager.connect("c", c)
        # No subscriptions of any kind.

        delivered = await manager.broadcast_all({"all": True})

        assert delivered == 3
        for ws in (a, b, c):
            assert ws.sent == [{"all": True}]

    async def test_broadcast_all_ignores_channel_membership(self, manager):
        sub, nosub = _FakeWS(), _FakeWS()
        await manager.connect("sub", sub)
        await manager.connect("nosub", nosub)
        await manager.subscribe("sub", "portfolio")

        delivered = await manager.broadcast_all({"v": 1})

        assert delivered == 2
        assert sub.sent == [{"v": 1}]
        assert nosub.sent == [{"v": 1}]

    async def test_broadcast_all_no_connections_returns_zero(self, manager):
        delivered = await manager.broadcast_all({"v": 1})
        assert delivered == 0

    async def test_broadcast_all_counts_successes_only(self, manager):
        good = _FakeWS()
        bad = _FakeWS(fail_with=RuntimeError("dead"))
        await manager.connect("good", good)
        await manager.connect("bad", bad)

        delivered = await manager.broadcast_all({"v": 2})

        assert delivered == 1
        assert good.sent == [{"v": 2}]


# ---------------------------------------------------------------------------
# 4. reconnect with same connection_id replaces old socket
# ---------------------------------------------------------------------------


class TestReconnectSameId:
    async def test_reconnect_replaces_socket_object(self, manager):
        old = _FakeWS()
        await manager.connect("c1", old)

        new = _FakeWS()
        await manager.connect("c1", new)

        # Only one connection tracked, and it is the new socket.
        assert manager.connection_count == 1
        assert manager.is_connected("c1")
        assert manager.connections["c1"] is new
        assert manager.connections["c1"] is not old

    async def test_reconnect_clears_prior_memberships(self, manager):
        """A new socket must not inherit the prior socket's channel subs.

        This is the safety guarantee: messages are never routed to a
        replaced handle just because it reused an id.
        """
        old = _FakeWS()
        await manager.connect("c1", old)
        await manager.subscribe("c1", "portfolio")
        assert manager.is_subscribed("c1", "portfolio")

        new = _FakeWS()
        await manager.connect("c1", new)

        # New socket is registered but carries no inherited subscriptions.
        assert manager.is_connected("c1")
        assert not manager.is_subscribed("c1", "portfolio")
        assert manager.get_subscriptions("c1") == frozenset()
        # Orphan channel membership pruned.
        assert manager.channel_count == 0

    async def test_reconnect_routes_broadcast_to_new_socket_only(self, manager):
        old = _FakeWS()
        await manager.connect("c1", old)
        await manager.subscribe("c1", "portfolio")

        new = _FakeWS()
        await manager.connect("c1", new)
        await manager.subscribe("c1", "portfolio")

        delivered = await manager.broadcast("portfolio", {"v": 5})

        assert delivered == 1
        # Old socket never receives — it was replaced.
        assert old.sent == []
        assert new.sent == [{"v": 5}]

    async def test_reconnect_does_not_double_count(self, manager):
        await manager.connect("c1", _FakeWS())
        await manager.connect("c1", _FakeWS())
        await manager.connect("c1", _FakeWS())

        assert manager.connection_count == 1


# ---------------------------------------------------------------------------
# 5. auto-cleanup of dead connections on failed sends
# ---------------------------------------------------------------------------


class TestAutoCleanupFailedSends:
    async def test_runtime_error_triggers_cleanup(self, manager):
        bad = _FakeWS(fail_with=RuntimeError("network gone"))
        await manager.connect("c1", bad)
        await manager.subscribe("c1", "portfolio")

        await manager.broadcast("portfolio", {"v": 1})

        # Dead connection detached entirely.
        assert not manager.is_connected("c1")
        assert "c1" not in manager.connections
        assert manager.connection_count == 0

    async def test_websocket_disconnect_triggers_cleanup(self, manager):
        bad = _FakeWS(fail_with=WebSocketDisconnect())
        await manager.connect("c1", bad)
        await manager.subscribe("c1", "portfolio")

        await manager.broadcast("portfolio", {"v": 1})

        assert not manager.is_connected("c1")

    async def test_cleanup_removes_membership_from_all_channels(self, manager):
        """Cleanup must prune the dead id from *every* channel, not just
        the one being broadcast to."""
        bad = _FakeWS(fail_with=RuntimeError("boom"))
        await manager.connect("bad", bad)
        await manager.subscribe("bad", "portfolio")
        await manager.subscribe("bad", "alerts")
        await manager.subscribe("bad", "orders")

        # Broadcast to a channel the bad conn is in.
        await manager.broadcast("portfolio", {"v": 1})

        assert not manager.is_connected("bad")
        assert not manager.is_subscribed("bad", "portfolio")
        assert not manager.is_subscribed("bad", "alerts")
        assert not manager.is_subscribed("bad", "orders")
        # All channels empty -> pruned.
        assert manager.channel_count == 0

    async def test_cleanup_prunes_emptied_channels(self, manager):
        sole = _FakeWS(fail_with=RuntimeError("x"))
        await manager.connect("sole", sole)
        await manager.subscribe("sole", "portfolio")

        await manager.broadcast("portfolio", {"v": 1})

        assert "portfolio" not in manager.channel_subscriptions
        assert manager.channel_count == 0

    async def test_failed_send_does_not_break_other_recipients(self, manager):
        """The fanout is concurrent; one dead client can't starve the rest."""
        good_a = _FakeWS()
        good_b = _FakeWS()
        bad = _FakeWS(fail_with=RuntimeError("dead"))
        await manager.connect("a", good_a)
        await manager.connect("b", good_b)
        await manager.connect("bad", bad)
        for cid in ("a", "b", "bad"):
            await manager.subscribe(cid, "portfolio")

        delivered = await manager.broadcast("portfolio", {"v": 42})

        assert delivered == 2
        assert good_a.sent == [{"v": 42}]
        assert good_b.sent == [{"v": 42}]
        # bad cleaned up; the other two remain.
        assert not manager.is_connected("bad")
        assert manager.is_connected("a")
        assert manager.is_connected("b")
        assert manager.connection_count == 2

    async def test_broadcast_all_cleans_up_failed(self, manager):
        good = _FakeWS()
        bad = _FakeWS(fail_with=RuntimeError("boom"))
        await manager.connect("good", good)
        await manager.connect("bad", bad)

        await manager.broadcast_all({"v": 1})

        assert manager.is_connected("good")
        assert not manager.is_connected("bad")
        assert good.sent == [{"v": 1}]

    async def test_send_to_failing_connection_returns_false_and_cleans_up(self, manager):
        bad = _FakeWS(fail_with=RuntimeError("dead"))
        await manager.connect("c1", bad)
        await manager.subscribe("c1", "portfolio")

        ok = await manager.send("c1", {"dm": True})

        assert ok is False
        assert not manager.is_connected("c1")
        assert not manager.is_subscribed("c1", "portfolio")

    async def test_send_to_unknown_connection_returns_false(self, manager):
        ok = await manager.send("ghost", {"v": 1})
        assert ok is False


# ---------------------------------------------------------------------------
# Direct send() behavior
# ---------------------------------------------------------------------------


class TestSendSingle:
    async def test_send_delivers_to_single_connection(self, manager):
        target = _FakeWS()
        other = _FakeWS()
        await manager.connect("target", target)
        await manager.connect("other", other)

        ok = await manager.send("target", {"dm": True})

        assert ok is True
        assert target.sent == [{"dm": True}]
        assert other.sent == []

    async def test_send_does_not_require_subscription(self, manager):
        ws = _FakeWS()
        await manager.connect("c1", ws)
        # No channel subscription — send() is direct.

        ok = await manager.send("c1", {"v": 1})

        assert ok is True
        assert ws.sent == [{"v": 1}]


# ---------------------------------------------------------------------------
# Introspection helpers
# ---------------------------------------------------------------------------


class TestIntrospection:
    async def test_connection_count_tracks_registry(self, manager):
        assert manager.connection_count == 0
        await manager.connect("a", _FakeWS())
        assert manager.connection_count == 1
        await manager.connect("b", _FakeWS())
        assert manager.connection_count == 2
        await manager.disconnect("a")
        assert manager.connection_count == 1

    async def test_channel_count_tracks_registry(self, manager):
        assert manager.channel_count == 0
        await manager.connect("c1", _FakeWS())
        await manager.subscribe("c1", "portfolio")
        assert manager.channel_count == 1
        await manager.subscribe("c1", "alerts")
        assert manager.channel_count == 2
        await manager.unsubscribe("c1", "portfolio")
        assert manager.channel_count == 1

    async def test_is_connected_false_before_connect(self, manager):
        assert manager.is_connected("never") is False

    async def test_is_subscribed_false_by_default(self, manager):
        await manager.connect("c1", _FakeWS())
        assert manager.is_subscribed("c1", "portfolio") is False
        assert manager.is_subscribed("ghost", "portfolio") is False

    async def test_get_subscribers_returns_frozen_copy(self, manager):
        ws = _FakeWS()
        await manager.connect("c1", ws)
        await manager.subscribe("c1", "portfolio")

        subs = manager.get_subscribers("portfolio")
        assert subs == frozenset({"c1"})
        # Mutating the returned frozenset is impossible (immutable) — and
        # mutating the live registry must not change the snapshot we got.
        await manager.subscribe("c1", "alerts")
        assert subs == frozenset({"c1"})  # unchanged

    async def test_get_subscriptions_returns_frozen_copy(self, manager):
        ws = _FakeWS()
        await manager.connect("c1", ws)
        await manager.subscribe("c1", "portfolio")

        subs = manager.get_subscriptions("c1")
        assert subs == frozenset({"portfolio"})
        await manager.subscribe("c1", "alerts")
        assert subs == frozenset({"portfolio"})  # snapshot unchanged

    async def test_get_subscribers_unknown_channel(self, manager):
        assert manager.get_subscribers("ghost") == frozenset()

    async def test_get_subscriptions_unknown_connection(self, manager):
        assert manager.get_subscriptions("ghost") == frozenset()


# ---------------------------------------------------------------------------
# Concurrency / fanout ordering
# ---------------------------------------------------------------------------


class TestConcurrency:
    async def test_slow_recipient_does_not_block_fast_ones(self, manager):
        """A slow client must not stall delivery to faster ones because the
        actual sends run via ``asyncio.gather`` outside the lock."""
        slow = _FakeWS(delay=0.05)
        fast = _FakeWS()
        await manager.connect("slow", slow)
        await manager.connect("fast", fast)
        await manager.subscribe("slow", "portfolio")
        await manager.subscribe("fast", "portfolio")

        delivered = await manager.broadcast("portfolio", {"v": 1})

        assert delivered == 2
        assert slow.sent == [{"v": 1}]
        assert fast.sent == [{"v": 1}]

    async def test_concurrent_broadcasts_are_safe(self, manager):
        """Many overlapping broadcasts must not corrupt shared state."""
        sockets = []
        for i in range(10):
            ws = _FakeWS()
            sockets.append(ws)
            await manager.connect(f"c{i}", ws)
            await manager.subscribe(f"c{i}", "portfolio")

        # Fire several broadcasts concurrently.
        delivered = await asyncio.gather(
            *(manager.broadcast("portfolio", {"i": i}) for i in range(5))
        )

        assert delivered == [10, 10, 10, 10, 10]
        # Each socket received all 5 messages.
        for ws in sockets:
            assert len(ws.sent) == 5
            assert ws.sent == [{"i": i} for i in range(5)]

    async def test_concurrent_subscribes_are_serialized(self, manager):
        """The internal lock serializes mutation; the registry stays
        consistent under concurrent subscribers."""
        for i in range(10):
            await manager.connect(f"c{i}", _FakeWS())

        await asyncio.gather(*(manager.subscribe(f"c{i}", "portfolio") for i in range(10)))

        assert manager.get_subscribers("portfolio") == frozenset(f"c{i}" for i in range(10))
