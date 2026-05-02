"""Tests for PaperBroker metrics emission (gh#136 follow-up).

The adapter emits the following metrics through the active
``MetricsBackend`` (all tagged with ``broker``):

- ``paper_broker.submit`` — counter, exactly once per ``submit`` call.
  ``outcome ∈ {filled, resting, rejected}`` plus ``order_type``.
- ``paper_broker.cancel`` — counter, exactly once per ``cancel`` call.
  ``outcome ∈ {cancelled, unknown}``.
- ``paper_broker.simulate_fill`` — counter, exactly once per
  ``simulate_fill`` call. ``outcome ∈ {filled, unknown, no_price}``.
- ``paper_broker.pending`` — gauge, recomputed after every
  state-changing operation.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from engine.core.brokers.base import BrokerError, BrokerRejectError
from engine.core.brokers.paper import PaperBroker
from engine.core.oms.order import Order
from engine.core.oms.states import OrderSide, OrderType
from engine.observability.metrics import RecordingBackend


def _market_buy(symbol: str = "AAPL", qty: str = "10") -> Order:
    return Order(
        symbol=symbol,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal(qty),
    )


def _limit_buy(symbol: str = "AAPL", qty: str = "10") -> Order:
    return Order(
        symbol=symbol,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal(qty),
        limit_price=Decimal("100"),
    )


def _counter_total(backend: RecordingBackend, name: str) -> float:
    return sum(v for (n, _t), v in backend.counters.items() if n == name)


def _counter_with(
    backend: RecordingBackend, name: str, tags: dict[str, str]
) -> float:
    expected = tuple(sorted(tags.items()))
    return sum(
        v
        for (n, t), v in backend.counters.items()
        if n == name and all(item in t for item in expected)
    )


def _gauge_with(
    backend: RecordingBackend, name: str, tags: dict[str, str]
) -> float | None:
    expected = tuple(sorted(tags.items()))
    matches = [
        v
        for (n, t), v in backend.gauges.items()
        if n == name and all(item in t for item in expected)
    ]
    return matches[-1] if matches else None


@pytest.fixture
def metrics() -> RecordingBackend:
    return RecordingBackend()


class TestSubmitMarket:
    async def test_market_fill_emits_filled_outcome(self, metrics):
        broker = PaperBroker(
            price_for=lambda _: Decimal("100"), metrics=metrics
        )
        await broker.submit(_market_buy())

        assert (
            _counter_with(
                metrics,
                "paper_broker.submit",
                {"outcome": "filled", "order_type": "market", "broker": "paper"},
            )
            == 1
        )

    async def test_market_no_price_emits_rejected_outcome(self, metrics):
        broker = PaperBroker(price_for=lambda _: None, metrics=metrics)
        with pytest.raises(BrokerRejectError):
            await broker.submit(_market_buy())

        assert (
            _counter_with(
                metrics,
                "paper_broker.submit",
                {"outcome": "rejected", "order_type": "market"},
            )
            == 1
        )


class TestSubmitLimit:
    async def test_limit_emits_resting_outcome_and_pending_gauge(self, metrics):
        broker = PaperBroker(
            price_for=lambda _: Decimal("100"), metrics=metrics
        )
        await broker.submit(_limit_buy())

        assert (
            _counter_with(
                metrics,
                "paper_broker.submit",
                {"outcome": "resting", "order_type": "limit"},
            )
            == 1
        )
        assert _gauge_with(metrics, "paper_broker.pending", {"broker": "paper"}) == 1.0

    async def test_two_resting_orders_bring_pending_to_two(self, metrics):
        broker = PaperBroker(
            price_for=lambda _: Decimal("100"), metrics=metrics
        )
        await broker.submit(_limit_buy())
        await broker.submit(_limit_buy(symbol="MSFT"))

        assert _gauge_with(metrics, "paper_broker.pending", {"broker": "paper"}) == 2.0


class TestCancel:
    async def test_cancel_known_emits_cancelled_and_drops_pending_gauge(self, metrics):
        broker = PaperBroker(
            price_for=lambda _: Decimal("100"), metrics=metrics
        )
        result = await broker.submit(_limit_buy())

        await broker.cancel(
            order_id=result.order_id, broker_order_id=result.broker_order_id
        )

        assert (
            _counter_with(
                metrics,
                "paper_broker.cancel",
                {"outcome": "cancelled", "broker": "paper"},
            )
            == 1
        )
        assert _gauge_with(metrics, "paper_broker.pending", {"broker": "paper"}) == 0.0

    async def test_cancel_unknown_emits_unknown_outcome(self, metrics):
        broker = PaperBroker(
            price_for=lambda _: Decimal("100"), metrics=metrics
        )

        with pytest.raises(BrokerRejectError):
            await broker.cancel(
                order_id=uuid.uuid4(), broker_order_id="DOES-NOT-EXIST"
            )

        assert (
            _counter_with(
                metrics,
                "paper_broker.cancel",
                {"outcome": "unknown"},
            )
            == 1
        )


class TestSimulateFill:
    async def test_simulate_fill_known_emits_filled_outcome(self, metrics):
        broker = PaperBroker(
            price_for=lambda _: Decimal("100"), metrics=metrics
        )
        result = await broker.submit(_limit_buy())

        await broker.simulate_fill(broker_order_id=result.broker_order_id)

        assert (
            _counter_with(
                metrics,
                "paper_broker.simulate_fill",
                {"outcome": "filled"},
            )
            == 1
        )
        assert _gauge_with(metrics, "paper_broker.pending", {"broker": "paper"}) == 0.0

    async def test_simulate_fill_unknown_emits_unknown_outcome(self, metrics):
        broker = PaperBroker(
            price_for=lambda _: Decimal("100"), metrics=metrics
        )

        with pytest.raises(BrokerError):
            await broker.simulate_fill(broker_order_id="DOES-NOT-EXIST")

        assert (
            _counter_with(
                metrics,
                "paper_broker.simulate_fill",
                {"outcome": "unknown"},
            )
            == 1
        )

    async def test_simulate_fill_no_price_emits_no_price_outcome(self, metrics):
        # Limit submit does not call ``price_for`` (orders just rest), so
        # a resolver returning ``None`` only fires when ``simulate_fill``
        # asks for the fill price.
        broker = PaperBroker(price_for=lambda _: None, metrics=metrics)
        result = await broker.submit(_limit_buy())

        with pytest.raises(BrokerRejectError):
            await broker.simulate_fill(broker_order_id=result.broker_order_id)

        assert (
            _counter_with(
                metrics,
                "paper_broker.simulate_fill",
                {"outcome": "no_price"},
            )
            == 1
        )


class TestNameTagged:
    async def test_custom_name_propagates_to_broker_tag(self, metrics):
        broker = PaperBroker(
            price_for=lambda _: Decimal("100"),
            name="paper-staging",
            metrics=metrics,
        )
        await broker.submit(_market_buy())

        assert (
            _counter_with(
                metrics,
                "paper_broker.submit",
                {"broker": "paper-staging"},
            )
            == 1
        )


class TestDefaultBackend:
    async def test_resolves_get_metrics_when_not_injected(self):
        from engine.observability.metrics import NullBackend, set_metrics

        recording = RecordingBackend()
        set_metrics(recording)
        try:
            broker = PaperBroker(price_for=lambda _: Decimal("100"))
            await broker.submit(_market_buy())
            assert _counter_total(recording, "paper_broker.submit") == 1
        finally:
            set_metrics(NullBackend())
