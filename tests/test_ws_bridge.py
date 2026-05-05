"""Unit tests for the EventBus → ConnectionManager bridge (gh#7 follow-up)."""

from __future__ import annotations

import uuid

import pytest

from engine.api.websocket.bridge import (
    EventToWebSocketBridge,
    extract_user_id,
    topic_for_event_type,
)
from engine.api.websocket.manager import ConnectionManager, Topic

# ---------------------------------------------------------------------------
# topic_for_event_type
# ---------------------------------------------------------------------------


class TestTopicMapping:
    def test_order_prefix(self):
        assert topic_for_event_type("order.created") == Topic.ORDER
        assert topic_for_event_type("order.filled") == Topic.ORDER

    def test_portfolio_prefix(self):
        assert topic_for_event_type("portfolio.updated") == Topic.PORTFOLIO

    def test_backtest_prefix(self):
        assert topic_for_event_type("backtest.completed") == Topic.BACKTEST

    def test_alert_prefix(self):
        assert topic_for_event_type("alert.triggered") == Topic.ALERT

    def test_unknown_prefix_returns_none(self):
        assert topic_for_event_type("system.heartbeat") is None
        assert topic_for_event_type("") is None


# ---------------------------------------------------------------------------
# extract_user_id
# ---------------------------------------------------------------------------


class TestExtractUserId:
    def test_snake_case(self):
        u = uuid.uuid4()
        assert extract_user_id({"user_id": str(u)}) == u

    def test_camel_case(self):
        u = uuid.uuid4()
        assert extract_user_id({"userId": str(u)}) == u

    def test_uuid_object_passes_through(self):
        u = uuid.uuid4()
        assert extract_user_id({"user_id": u}) == u

    def test_missing_returns_none(self):
        assert extract_user_id({}) is None
        assert extract_user_id(None) is None

    def test_unparseable_returns_none(self):
        assert extract_user_id({"user_id": "not-a-uuid"}) is None
        assert extract_user_id({"user_id": 123}) is None


# ---------------------------------------------------------------------------
# Bridge integration with a fake bus
# ---------------------------------------------------------------------------


class _FakeBus:
    """Stand-in for the real EventBus.

    Only models the subscribe / unsubscribe surface the bridge uses.
    Tests can call ``deliver`` to simulate the bus delivering a payload.
    """

    def __init__(self) -> None:
        self._handlers: dict = {}

    def subscribe(self, event_type, handler) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    def unsubscribe(self, event_type, handler) -> None:
        if event_type in self._handlers:
            self._handlers[event_type] = [
                h for h in self._handlers[event_type] if h is not handler
            ]

    async def deliver(self, event_type, payload) -> None:
        for h in self._handlers.get(event_type, []):
            await h(payload)


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.id = uuid.uuid4()

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    def __hash__(self) -> int:
        return hash(self.id)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _FakeWS) and other.id == self.id


@pytest.fixture
async def setup():
    bus = _FakeBus()
    manager = ConnectionManager()
    bridge = EventToWebSocketBridge(bus=bus, manager=manager)
    bridge.attach(["order.filled", "portfolio.updated"])
    return bus, manager, bridge


class TestBridge:
    async def test_delivers_to_subscribed_user(self, setup):
        bus, manager, bridge = setup
        user_id = uuid.uuid4()
        ws = _FakeWS()
        await manager.attach(user_id, ws)
        await manager.subscribe(user_id, ws, ["order"])

        await bus.deliver(
            "order.filled",
            {
                "event_type": "order.filled",
                "data": {"user_id": str(user_id), "qty": 10},
            },
        )
        assert len(ws.sent) == 1
        assert ws.sent[0]["topic"] == "order"
        assert ws.sent[0]["data"]["event_type"] == "order.filled"

    async def test_drops_event_without_user_id(self, setup):
        bus, manager, bridge = setup
        user_id = uuid.uuid4()
        ws = _FakeWS()
        await manager.attach(user_id, ws)
        await manager.subscribe(user_id, ws, ["order"])

        await bus.deliver(
            "order.filled",
            {"event_type": "order.filled", "data": {"qty": 10}},
        )
        assert ws.sent == []

    async def test_drops_unrouted_event(self, setup):
        bus, manager, bridge = setup
        user_id = uuid.uuid4()
        ws = _FakeWS()
        await manager.attach(user_id, ws)
        await manager.subscribe(user_id, ws, ["order"])

        # Bridge is attached only to order.filled / portfolio.updated;
        # delivering a different type via the bus does nothing.
        await bus.deliver(
            "system.heartbeat",
            {"event_type": "system.heartbeat", "data": {"user_id": str(user_id)}},
        )
        assert ws.sent == []

    async def test_only_subscribed_topic_receives(self, setup):
        bus, manager, bridge = setup
        user_id = uuid.uuid4()
        ws = _FakeWS()
        await manager.attach(user_id, ws)
        # Subscribed to portfolio, not order.
        await manager.subscribe(user_id, ws, ["portfolio"])

        await bus.deliver(
            "order.filled",
            {"event_type": "order.filled", "data": {"user_id": str(user_id)}},
        )
        assert ws.sent == []

    async def test_detach_unsubscribes(self, setup):
        bus, manager, bridge = setup
        user_id = uuid.uuid4()
        ws = _FakeWS()
        await manager.attach(user_id, ws)
        await manager.subscribe(user_id, ws, ["order"])

        bridge.detach()
        await bus.deliver(
            "order.filled",
            {"event_type": "order.filled", "data": {"user_id": str(user_id)}},
        )
        assert ws.sent == []
