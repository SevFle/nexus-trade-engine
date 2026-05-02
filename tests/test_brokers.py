"""Unit tests for the broker adapter Protocol + registry (gh#136 partial)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest

from engine.core.brokers import (
    BrokerAdapter,
    BrokerAuthError,
    BrokerConnectionError,
    BrokerError,
    BrokerRejectError,
    SubmittedOrder,
    get_broker,
    list_brokers,
    register_broker,
)
from engine.core.brokers.registry import _reset_for_tests
from engine.core.oms import (
    AckEvent,
    Order,
    OrderEvent,
    OrderSide,
    OrderType,
)


@pytest.fixture(autouse=True)
def _reset():
    _reset_for_tests()
    yield
    _reset_for_tests()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _market_buy() -> Order:
    return Order(
        symbol="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("10"),
    )


class _FakeBroker:
    """Minimal Protocol-compatible adapter."""

    def __init__(self, name: str = "fake") -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    async def submit(self, order: Order) -> SubmittedOrder:
        return SubmittedOrder(order_id=order.id, broker_order_id=f"BRK-{order.id}")

    async def cancel(self, *, order_id: uuid.UUID, broker_order_id: str) -> None:
        return None

    async def events(self) -> AsyncIterator[OrderEvent]:  # type: ignore[override]
        # Empty stream — sufficient for Protocol satisfaction in tests.
        return
        yield  # pragma: no cover - generator marker


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class TestErrorHierarchy:
    def test_subtypes_inherit(self):
        assert issubclass(BrokerAuthError, BrokerError)
        assert issubclass(BrokerConnectionError, BrokerError)
        assert issubclass(BrokerRejectError, BrokerError)

    def test_reject_carries_broker_code(self):
        e = BrokerRejectError("insufficient buying power", broker_code="MARGIN_001")
        assert e.broker_code == "MARGIN_001"
        assert "buying power" in str(e)

    def test_reject_default_broker_code_none(self):
        e = BrokerRejectError("rejected")
        assert e.broker_code is None


# ---------------------------------------------------------------------------
# SubmittedOrder DTO
# ---------------------------------------------------------------------------


class TestSubmittedOrder:
    def test_carries_both_ids(self):
        oid = uuid.uuid4()
        result = SubmittedOrder(order_id=oid, broker_order_id="BRK-1")
        assert result.order_id == oid
        assert result.broker_order_id == "BRK-1"


# ---------------------------------------------------------------------------
# Protocol contract
# ---------------------------------------------------------------------------


class TestProtocol:
    def test_fake_broker_satisfies_protocol(self):
        assert isinstance(_FakeBroker(), BrokerAdapter)

    def test_incomplete_does_not_satisfy(self):
        class Incomplete:
            @property
            def name(self) -> str:
                return "x"

        assert not isinstance(Incomplete(), BrokerAdapter)

    async def test_fake_submit_returns_submitted_order(self):
        adapter = _FakeBroker()
        order = _market_buy()
        result = await adapter.submit(order)
        assert isinstance(result, SubmittedOrder)
        assert result.order_id == order.id


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_register_and_lookup(self):
        adapter = _FakeBroker(name="alpaca")
        register_broker(adapter)
        assert get_broker("alpaca") is adapter

    def test_lookup_unknown_raises(self):
        with pytest.raises(KeyError):
            get_broker("zzz")

    def test_list_returns_sorted(self):
        register_broker(_FakeBroker(name="alpaca"))
        register_broker(_FakeBroker(name="ibkr"))
        register_broker(_FakeBroker(name="binance"))
        assert list_brokers() == ["alpaca", "binance", "ibkr"]

    def test_re_register_overwrites(self):
        register_broker(_FakeBroker(name="alpaca"))
        replacement = _FakeBroker(name="alpaca")
        register_broker(replacement)
        assert get_broker("alpaca") is replacement

    def test_register_rejects_non_protocol(self):
        with pytest.raises(TypeError):
            register_broker("not-an-adapter")  # type: ignore[arg-type]

    def test_register_rejects_empty_name(self):
        with pytest.raises(ValueError):
            register_broker(_FakeBroker(name=""))

    def test_register_rejects_uppercase_name(self):
        with pytest.raises(ValueError):
            register_broker(_FakeBroker(name="ALPACA"))


# ---------------------------------------------------------------------------
# Sanity: an adapter can produce OMS-compatible events
# ---------------------------------------------------------------------------


class TestEventCompatibility:
    def test_broker_event_is_an_order_event(self):
        # The Protocol's events() returns AsyncIterator[OrderEvent]. As a
        # quick sanity check that the type alias accepts the adapter's
        # actual event shapes.
        from datetime import UTC, datetime

        ev: OrderEvent = AckEvent(
            occurred_at=datetime.now(tz=UTC), broker_order_id="X"
        )
        assert isinstance(ev, AckEvent)
