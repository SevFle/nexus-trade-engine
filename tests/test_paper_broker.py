"""Unit tests for the paper-trading broker (gh#136 follow-up)."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from engine.core.brokers import BrokerAdapter, BrokerRejectError
from engine.core.brokers.paper import PaperBroker
from engine.core.oms import (
    AckEvent,
    CancelEvent,
    FillEvent,
    Order,
    OrderSide,
    OrderType,
)


def _market_buy(symbol: str = "AAPL", qty: str = "10") -> Order:
    return Order(
        symbol=symbol,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal(qty),
    )


def _limit_buy(symbol: str = "AAPL", qty: str = "10", limit: str = "100") -> Order:
    return Order(
        symbol=symbol,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal(qty),
        limit_price=Decimal(limit),
    )


def _prices(table: dict[str, Decimal]):
    return lambda symbol: table.get(symbol)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_satisfies_protocol(self):
        broker = PaperBroker(price_for=_prices({"AAPL": Decimal("100")}))
        assert isinstance(broker, BrokerAdapter)

    def test_default_name(self):
        broker = PaperBroker(price_for=_prices({}))
        assert broker.name == "paper"

    def test_custom_name(self):
        broker = PaperBroker(price_for=_prices({}), name="paper-eu")
        assert broker.name == "paper-eu"

    def test_uppercase_name_rejected(self):
        with pytest.raises(ValueError):
            PaperBroker(price_for=_prices({}), name="PAPER")

    def test_empty_name_rejected(self):
        with pytest.raises(ValueError):
            PaperBroker(price_for=_prices({}), name="")


# ---------------------------------------------------------------------------
# Market submit
# ---------------------------------------------------------------------------


class TestMarketSubmit:
    async def test_market_emits_ack_then_fill(self):
        broker = PaperBroker(price_for=_prices({"AAPL": Decimal("101")}))
        order = _market_buy()
        result = await broker.submit(order)
        assert result.order_id == order.id
        assert result.broker_order_id.startswith("PAPER-")

        events = await broker.drain_events()
        assert len(events) == 2
        assert isinstance(events[0], AckEvent)
        assert isinstance(events[1], FillEvent)
        assert events[1].fill_quantity == Decimal("10")
        assert events[1].fill_price == Decimal("101")

    async def test_market_no_price_rejected(self):
        broker = PaperBroker(price_for=_prices({}))
        with pytest.raises(BrokerRejectError) as exc_info:
            await broker.submit(_market_buy())
        assert exc_info.value.broker_code == "NO_PRICE"

    async def test_market_zero_price_rejected(self):
        broker = PaperBroker(price_for=_prices({"AAPL": Decimal("0")}))
        with pytest.raises(BrokerRejectError):
            await broker.submit(_market_buy())


# ---------------------------------------------------------------------------
# Limit submit + simulate_fill
# ---------------------------------------------------------------------------


class TestLimitFlow:
    async def test_limit_acks_and_rests(self):
        broker = PaperBroker(price_for=_prices({"AAPL": Decimal("100")}))
        order = _limit_buy()
        await broker.submit(order)
        events = await broker.drain_events()
        assert len(events) == 1
        assert isinstance(events[0], AckEvent)
        assert broker.pending_count() == 1
        # No fill yet.
        assert all(not isinstance(ev, FillEvent) for ev in events)

    async def test_simulate_fill_drains_pending(self):
        broker = PaperBroker(price_for=_prices({"AAPL": Decimal("100")}))
        order = _limit_buy()
        result = await broker.submit(order)
        await broker.drain_events()  # discard the Ack

        await broker.simulate_fill(broker_order_id=result.broker_order_id)
        events = await broker.drain_events()
        assert len(events) == 1
        assert isinstance(events[0], FillEvent)
        assert events[0].fill_quantity == order.quantity
        assert events[0].fill_price == Decimal("100")
        assert broker.pending_count() == 0

    async def test_simulate_fill_with_explicit_price(self):
        broker = PaperBroker(price_for=_prices({}))
        order = _limit_buy(limit="100")
        result = await broker.submit(order)
        await broker.drain_events()

        await broker.simulate_fill(
            broker_order_id=result.broker_order_id,
            fill_price=Decimal("99.5"),
        )
        events = await broker.drain_events()
        assert events[0].fill_price == Decimal("99.5")

    async def test_simulate_fill_unknown_raises(self):
        broker = PaperBroker(price_for=_prices({}))
        with pytest.raises(Exception):
            await broker.simulate_fill(broker_order_id="PAPER-doesnotexist")


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


class TestCancel:
    async def test_cancel_resting_order(self):
        broker = PaperBroker(price_for=_prices({"AAPL": Decimal("100")}))
        order = _limit_buy()
        result = await broker.submit(order)
        await broker.drain_events()

        await broker.cancel(
            order_id=order.id, broker_order_id=result.broker_order_id
        )
        events = await broker.drain_events()
        assert len(events) == 1
        assert isinstance(events[0], CancelEvent)
        assert events[0].requested is False
        assert broker.pending_count() == 0

    async def test_cancel_unknown_rejected(self):
        broker = PaperBroker(price_for=_prices({}))
        with pytest.raises(BrokerRejectError) as exc_info:
            await broker.cancel(
                order_id=uuid.uuid4(), broker_order_id="PAPER-nope"
            )
        assert exc_info.value.broker_code == "NOT_PENDING"

    async def test_cancel_after_fill_rejected(self):
        broker = PaperBroker(price_for=_prices({"AAPL": Decimal("100")}))
        # Market order auto-fills, so it's never pending.
        result = await broker.submit(_market_buy())
        await broker.drain_events()

        with pytest.raises(BrokerRejectError):
            await broker.cancel(
                order_id=uuid.uuid4(),
                broker_order_id=result.broker_order_id,
            )


# ---------------------------------------------------------------------------
# Events iterator
# ---------------------------------------------------------------------------


class TestEventsIterator:
    async def test_async_for_yields_buffered_events(self):
        broker = PaperBroker(price_for=_prices({"AAPL": Decimal("101")}))
        await broker.submit(_market_buy())
        # Pull two events via the async iterator without blocking.
        it = broker.events()
        first = await anext(it)
        second = await anext(it)
        assert isinstance(first, AckEvent)
        assert isinstance(second, FillEvent)
