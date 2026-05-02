"""Unit tests for the WebSocket connection manager (gh#7)."""

from __future__ import annotations

import uuid

import pytest

from engine.api.websocket.manager import (
    VALID_TOPICS,
    ConnectionManager,
    Topic,
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
def manager() -> ConnectionManager:
    return ConnectionManager()


@pytest.fixture
def user_id() -> uuid.UUID:
    return uuid.uuid4()


class TestTopicEnum:
    def test_valid_topics_match_enum(self):
        assert VALID_TOPICS == frozenset(t.value for t in Topic)

    def test_documented_topics(self):
        assert VALID_TOPICS == frozenset({"portfolio", "backtest", "order", "alert"})


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
        n = await manager.broadcast(
            user_id=user_id, topic="portfolio", payload={"v": 1}
        )
        assert n == 1
        assert a.sent == [{"topic": "portfolio", "data": {"v": 1}}]
        assert b.sent == []

    async def test_unknown_topic_yields_zero(self, manager, user_id):
        a = _FakeWS()
        await manager.attach(user_id, a)
        await manager.subscribe(user_id, a, ["portfolio"])
        n = await manager.broadcast(
            user_id=user_id, topic="wizard", payload={"v": 1}
        )
        assert n == 0
        assert a.sent == []

    async def test_no_recipients_returns_zero(self, manager, user_id):
        a = _FakeWS()
        await manager.attach(user_id, a)
        # No subscriptions at all.
        n = await manager.broadcast(
            user_id=user_id, topic="portfolio", payload={"v": 1}
        )
        assert n == 0
        assert a.sent == []

    async def test_send_failure_does_not_break_others(self, manager, user_id):
        good = _FakeWS()
        bad = _FakeWS(fail=True)
        await manager.attach(user_id, good)
        await manager.attach(user_id, bad)
        await manager.subscribe(user_id, good, ["portfolio"])
        await manager.subscribe(user_id, bad, ["portfolio"])
        n = await manager.broadcast(
            user_id=user_id, topic="portfolio", payload={"v": 7}
        )
        # Both were recipients — broadcast counts them; only the working
        # one received.
        assert n == 2
        assert good.sent == [{"topic": "portfolio", "data": {"v": 7}}]
        assert bad.sent == []  # failed silently

    async def test_broadcast_to_unknown_user_returns_zero(self, manager):
        n = await manager.broadcast(
            user_id=uuid.uuid4(), topic="portfolio", payload={"v": 1}
        )
        assert n == 0


class TestSingleton:
    def test_get_manager_is_idempotent(self):
        a = get_manager()
        b = get_manager()
        assert a is b
