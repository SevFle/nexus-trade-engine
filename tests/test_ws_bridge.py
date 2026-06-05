"""Unit tests for the EventBus → ConnectionManager bridge (gh#7 + SEV-275)."""

from __future__ import annotations

import uuid

import pytest

from engine.api.websocket.bridge import (
    EventToWebSocketBridge,
    extract_correlation_id,
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

    def test_market_data_prefix(self):
        assert topic_for_event_type("market.data.update") == Topic.MARKET_DATA

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
# extract_correlation_id
# ---------------------------------------------------------------------------


class TestExtractCorrelationId:
    def test_outer_envelope(self):
        assert extract_correlation_id({"correlation_id": "abc"}) == "abc"

    def test_camel_case_envelope(self):
        assert extract_correlation_id({"correlationId": "abc"}) == "abc"

    def test_inner_data(self):
        assert (
            extract_correlation_id({"data": {"correlation_id": "inner"}}) == "inner"
        )

    def test_inner_camel_case(self):
        assert (
            extract_correlation_id({"data": {"correlationId": "inner"}}) == "inner"
        )

    def test_outer_wins_over_inner(self):
        assert (
            extract_correlation_id(
                {
                    "correlation_id": "outer",
                    "data": {"correlation_id": "inner"},
                }
            )
            == "outer"
        )

    def test_missing_returns_none(self):
        assert extract_correlation_id({}) is None
        assert extract_correlation_id({"data": {}}) is None

    def test_non_string_returns_none(self):
        assert extract_correlation_id({"correlation_id": 42}) is None
        assert extract_correlation_id({"correlation_id": ""}) is None


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
    bridge.attach(["order.filled", "portfolio.updated", "market.data.update"])
    return bus, manager, bridge


class TestBridge:
    async def test_delivers_to_subscribed_user(self, setup):
        bus, manager, _bridge = setup
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
        # SEV-275 envelope shape — payload wrapped with channel/event/seq.
        assert ws.sent[0]["channel"] == "order"
        assert ws.sent[0]["event"] == "order.filled"
        assert ws.sent[0]["data"]["event_type"] == "order.filled"

    async def test_drops_event_without_user_id(self, setup):
        bus, manager, _bridge = setup
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
        bus, manager, _bridge = setup
        user_id = uuid.uuid4()
        ws = _FakeWS()
        await manager.attach(user_id, ws)
        await manager.subscribe(user_id, ws, ["order"])

        # Bridge is attached only to order.filled / portfolio.updated /
        # market.data.update; delivering a different type via the bus
        # does nothing.
        await bus.deliver(
            "system.heartbeat",
            {"event_type": "system.heartbeat", "data": {"user_id": str(user_id)}},
        )
        assert ws.sent == []

    async def test_only_subscribed_topic_receives(self, setup):
        bus, manager, _bridge = setup
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

    async def test_correlation_id_propagates_through_envelope(self, setup):
        bus, manager, _bridge = setup
        user_id = uuid.uuid4()
        ws = _FakeWS()
        await manager.attach(user_id, ws)
        await manager.subscribe(user_id, ws, ["order"])

        await bus.deliver(
            "order.filled",
            {
                "event_type": "order.filled",
                "correlation_id": "req-abc-123",
                "data": {"user_id": str(user_id), "qty": 5},
            },
        )
        assert len(ws.sent) == 1
        assert ws.sent[0]["correlation_id"] == "req-abc-123"

    async def test_market_data_broadcast_to_multiple_users(self, setup):
        bus, manager, _bridge = setup
        u1, u2 = uuid.uuid4(), uuid.uuid4()
        ws1, ws2 = _FakeWS(), _FakeWS()
        await manager.attach(u1, ws1)
        await manager.attach(u2, ws2)
        await manager.subscribe(u1, ws1, ["market_data"])
        await manager.subscribe(u2, ws2, ["market_data"])

        await bus.deliver(
            "market.data.update",
            {
                "event_type": "market.data.update",
                # No user_id → fan-out to all listeners.
                "data": {"symbol": "AAPL", "price": 191.50},
            },
        )
        assert len(ws1.sent) == 1
        assert len(ws2.sent) == 1
        assert ws1.sent[0]["event"] == "market.data.update"
        assert ws2.sent[0]["event"] == "market.data.update"
