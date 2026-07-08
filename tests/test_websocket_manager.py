"""Unit tests for the WebSocket connection manager (gh#7)."""

from __future__ import annotations

import uuid

import pytest
from fastapi import WebSocketDisconnect

from engine.api.websocket.manager import (
    VALID_TOPICS,
    ConnectionManager,
    Topic,
    UserTopicManager,
    get_manager,
)


class _FakeWS:
    """Minimal WebSocket stand-in. Captures every send_json call."""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[dict] = []
        self.fail = fail
        self.id = uuid.uuid4()

    async def send_json(self, payload: dict) -> None:
        if self.fail:
            raise RuntimeError("simulated send failure")
        self.sent.append(payload)

    # Required so ConnectionManager can use it as a dict key.
    def __hash__(self) -> int:
        return hash(self.id)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _FakeWS) and other.id == self.id


@pytest.fixture
def manager() -> UserTopicManager:
    return UserTopicManager()


@pytest.fixture
def user_id() -> uuid.UUID:
    return uuid.uuid4()


class TestTopicEnum:
    def test_valid_topics_match_enum(self):
        assert frozenset(t.value for t in Topic) == VALID_TOPICS

    def test_documented_topics(self):
        assert frozenset({"portfolio", "backtest", "order", "alert"}) == VALID_TOPICS


class TestAttachDetach:
    async def test_attach_increments_count(self, manager, user_id):
        ws = _FakeWS()
        await manager.attach(user_id, ws)
        assert manager.user_connection_count(user_id) == 1
        assert manager.total_connections() == 1

    async def test_detach_zeroes_count(self, manager, user_id):
        ws = _FakeWS()
        await manager.attach(user_id, ws)
        await manager.detach(user_id, ws)
        assert manager.user_connection_count(user_id) == 0
        assert manager.total_connections() == 0

    async def test_detach_unknown_is_noop(self, manager, user_id):
        await manager.detach(user_id, _FakeWS())
        assert manager.total_connections() == 0

    async def test_two_users_isolated(self, manager):
        u1, u2 = uuid.uuid4(), uuid.uuid4()
        ws1, ws2 = _FakeWS(), _FakeWS()
        await manager.attach(u1, ws1)
        await manager.attach(u2, ws2)
        assert manager.user_connection_count(u1) == 1
        assert manager.user_connection_count(u2) == 1
        assert manager.total_connections() == 2


class TestSubscribe:
    async def test_subscribe_filters_invalid(self, manager, user_id):
        ws = _FakeWS()
        await manager.attach(user_id, ws)
        result = await manager.subscribe(user_id, ws, ["portfolio", "wizard"])
        assert result == {"portfolio"}

    async def test_subscribe_accumulates(self, manager, user_id):
        ws = _FakeWS()
        await manager.attach(user_id, ws)
        await manager.subscribe(user_id, ws, ["portfolio"])
        result = await manager.subscribe(user_id, ws, ["backtest"])
        assert result == {"portfolio", "backtest"}

    async def test_unsubscribe_removes(self, manager, user_id):
        ws = _FakeWS()
        await manager.attach(user_id, ws)
        await manager.subscribe(user_id, ws, ["portfolio", "backtest"])
        result = await manager.unsubscribe(user_id, ws, ["portfolio"])
        assert result == {"backtest"}

    async def test_subscribe_unattached_returns_empty(self, manager, user_id):
        ws = _FakeWS()  # never attached
        result = await manager.subscribe(user_id, ws, ["portfolio"])
        assert result == set()


class TestBroadcast:
    async def test_only_subscribed_recipients(self, manager, user_id):
        a, b = _FakeWS(), _FakeWS()
        await manager.attach(user_id, a)
        await manager.attach(user_id, b)
        await manager.subscribe(user_id, a, ["portfolio"])
        await manager.subscribe(user_id, b, ["alert"])
        n = await manager.broadcast(user_id=user_id, topic="portfolio", payload={"v": 1})
        assert n == 1
        assert a.sent == [{"topic": "portfolio", "data": {"v": 1}}]
        assert b.sent == []

    async def test_unknown_topic_yields_zero(self, manager, user_id):
        a = _FakeWS()
        await manager.attach(user_id, a)
        await manager.subscribe(user_id, a, ["portfolio"])
        n = await manager.broadcast(user_id=user_id, topic="wizard", payload={"v": 1})
        assert n == 0
        assert a.sent == []

    async def test_no_recipients_returns_zero(self, manager, user_id):
        a = _FakeWS()
        await manager.attach(user_id, a)
        # No subscriptions at all.
        n = await manager.broadcast(user_id=user_id, topic="portfolio", payload={"v": 1})
        assert n == 0
        assert a.sent == []

    async def test_send_failure_does_not_break_others(self, manager, user_id):
        good = _FakeWS()
        bad = _FakeWS(fail=True)
        await manager.attach(user_id, good)
        await manager.attach(user_id, bad)
        await manager.subscribe(user_id, good, ["portfolio"])
        await manager.subscribe(user_id, bad, ["portfolio"])
        n = await manager.broadcast(user_id=user_id, topic="portfolio", payload={"v": 7})
        # Both were recipients — broadcast counts them; only the working
        # one received.
        assert n == 2
        assert good.sent == [{"topic": "portfolio", "data": {"v": 7}}]
        assert bad.sent == []  # failed silently

    async def test_broadcast_to_unknown_user_returns_zero(self, manager):
        n = await manager.broadcast(user_id=uuid.uuid4(), topic="portfolio", payload={"v": 1})
        assert n == 0


class TestSingleton:
    def test_get_manager_is_idempotent(self):
        a = get_manager()
        b = get_manager()
        assert a is b


class _ChannelWS:
    """Minimal WebSocket stand-in for the channel-based ConnectionManager.

    ``sent`` records every successful ``send_json`` payload. ``mode``
    selects success (``"ok"``), a hard disconnect (``"disconnect"``) or
    a generic send error (``"error"``) so we can exercise every branch
    of ``ConnectionManager._safe_send``.
    """

    def __init__(self, mode: str = "ok") -> None:
        self.sent: list[dict] = []
        self.mode = mode

    async def send_json(self, payload: dict) -> None:
        if self.mode == "disconnect":
            raise WebSocketDisconnect
        if self.mode == "error":
            raise RuntimeError("simulated send failure")
        self.sent.append(payload)


class TestChannelConnectionManagerBroadcast:
    """Regression coverage for the channel-based ``ConnectionManager``.

    ``broadcast`` / ``send`` fan out through ``_safe_send``, which must
    report ``True`` on a successful send. An earlier build omitted that
    ``return True`` so every successful delivery read back as a failure:
    ``_fanout`` routed the connection into ``_cleanup_failed`` and
    detached it. These tests pin the success contract and the cleanup
    contract so the bug cannot silently return.
    """

    @pytest.fixture
    def cm(self) -> ConnectionManager:
        return ConnectionManager()

    async def test_broadcast_delivers_and_keeps_connections(self, cm):
        """A successful broadcast must deliver to every recipient and
        leave every connection registered and subscribed."""
        good_a = _ChannelWS("ok")
        good_b = _ChannelWS("ok")
        await cm.connect("conn-a", good_a, user_id="alice")
        await cm.connect("conn-b", good_b, user_id="bob")
        assert await cm.subscribe("conn-a", "portfolio")
        assert await cm.subscribe("conn-b", "portfolio")

        sent = await cm.broadcast("portfolio", {"v": 1})

        # Both recipients counted as delivered (this was the bug: returned 0).
        assert sent == 2
        assert good_a.sent == [{"v": 1}]
        assert good_b.sent == [{"v": 1}]
        # And neither was cleaned up as a failure.
        assert cm.connection_count == 2
        assert cm.is_connected("conn-a")
        assert cm.is_connected("conn-b")
        assert cm.channel_count == 1

    async def test_send_single_returns_true_on_success(self, cm):
        """``send`` reports ``True`` only when the delivery succeeded."""
        ws = _ChannelWS("ok")
        await cm.connect("conn-1", ws, user_id="alice")

        ok = await cm.send("conn-1", {"v": 9})

        assert ok is True
        assert ws.sent == [{"v": 9}]
        assert cm.is_connected("conn-1")

    async def test_broadcast_cleans_up_only_failed_connections(self, cm):
        """A failing send is detached; a concurrent successful send is not."""
        good = _ChannelWS("ok")
        bad = _ChannelWS("error")
        await cm.connect("conn-good", good, user_id="alice")
        await cm.connect("conn-bad", bad, user_id="bob")
        await cm.subscribe("conn-good", "portfolio")
        await cm.subscribe("conn-bad", "portfolio")

        sent = await cm.broadcast("portfolio", {"v": 2})

        assert sent == 1
        assert good.sent == [{"v": 2}]
        # The failed connection was pruned; the good one survived.
        assert cm.is_connected("conn-good")
        assert not cm.is_connected("conn-bad")
        assert cm.connection_count == 1

    async def test_broadcast_cleans_up_disconnected_connection(self, cm):
        """``WebSocketDisconnect`` is treated as a dead connection too."""
        good = _ChannelWS("ok")
        dead = _ChannelWS("disconnect")
        await cm.connect("conn-good", good, user_id="alice")
        await cm.connect("conn-dead", dead, user_id="bob")
        await cm.subscribe("conn-good", "portfolio")
        await cm.subscribe("conn-dead", "portfolio")

        sent = await cm.broadcast("portfolio", {"v": 3})

        assert sent == 1
        assert cm.is_connected("conn-good")
        assert not cm.is_connected("conn-dead")
