"""
Comprehensive unit tests for order_manager.py — Order model, OrderStatus/OrderType
enums, status transitions, and edge cases not covered by test_order_manager.py.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from engine.core.cost_model import DefaultCostModel
from engine.core.execution.base import FillResult
from engine.core.order_manager import Order, OrderManager, OrderStatus, OrderType
from engine.core.portfolio import Portfolio
from engine.core.risk_engine import RiskEngine
from engine.core.signal import Side, Signal


class TestOrderStatusEnum:
    def test_all_statuses_exist(self):
        expected = {
            "pending", "validated", "costed", "risk_approved", "risk_rejected",
            "submitted", "filled", "partially_filled", "cancelled", "rejected", "failed",
        }
        actual = {s.value for s in OrderStatus}
        assert actual == expected

    def test_is_string_enum(self):
        assert isinstance(OrderStatus.FILLED, str)
        assert OrderStatus.FILLED == "filled"


class TestOrderTypeEnum:
    def test_all_types_exist(self):
        expected = {"market", "limit", "stop", "stop_limit"}
        actual = {t.value for t in OrderType}
        assert actual == expected

    def test_default_is_market(self):
        order = _make_order()
        assert order.order_type == OrderType.MARKET


class TestOrderModel:
    def test_auto_generated_id(self):
        o1 = _make_order()
        o2 = _make_order()
        assert o1.id != o2.id

    def test_auto_generated_created_at(self):
        o = _make_order()
        assert isinstance(o.created_at, datetime)
        assert o.created_at.tzinfo is not None

    def test_default_status_pending(self):
        o = _make_order()
        assert o.status == OrderStatus.PENDING

    def test_default_status_history_empty(self):
        o = _make_order()
        assert o.status_history == []

    def test_optional_fields_default_none(self):
        o = _make_order()
        assert o.cost_breakdown is None
        assert o.fill_price is None
        assert o.fill_quantity is None
        assert o.filled_at is None
        assert o.limit_price is None

    def test_limit_price_set(self):
        o = _make_order()
        o2 = _make_order_with_overrides(limit_price=155.0)
        assert o2.limit_price == 155.0


class TestOrderTransition:
    def test_transition_changes_status(self):
        o = _make_order()
        o.transition(OrderStatus.VALIDATED, "passed validation")
        assert o.status == OrderStatus.VALIDATED

    def test_transition_appends_to_history(self):
        o = _make_order()
        o.transition(OrderStatus.VALIDATED, "passed")
        o.transition(OrderStatus.COSTED, "costed")
        assert len(o.status_history) == 2

    def test_transition_history_contains_from_to(self):
        o = _make_order()
        o.transition(OrderStatus.VALIDATED, "reason1")
        entry = o.status_history[0]
        assert entry["from"] == OrderStatus.PENDING
        assert entry["to"] == OrderStatus.VALIDATED
        assert entry["reason"] == "reason1"
        assert "timestamp" in entry

    def test_multiple_transitions_track_all(self):
        o = _make_order()
        transitions = [
            OrderStatus.VALIDATED,
            OrderStatus.COSTED,
            OrderStatus.RISK_APPROVED,
            OrderStatus.SUBMITTED,
            OrderStatus.FILLED,
        ]
        for s in transitions:
            o.transition(s)
        assert len(o.status_history) == 5
        assert o.status == OrderStatus.FILLED

    def test_transition_with_empty_reason(self):
        o = _make_order()
        o.transition(OrderStatus.CANCELLED)
        assert o.status_history[0]["reason"] == ""


class TestOrderManagerSetBackend:
    def test_set_backend(self):
        om = _make_order_manager()
        backend = _FakeBackend()
        om.set_execution_backend(backend)
        assert om.execution_backend is backend


class TestOrderManagerSequentialOrders:
    async def test_multiple_buys_accumulate(self):
        om = _make_order_manager()
        for _ in range(3):
            signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=5)
            await om.process_signal(signal, market_price=100.0)
        assert om.portfolio.positions["AAPL"].quantity == 15
        assert len(om.completed_orders) == 3

    async def test_buy_then_sell(self):
        om = _make_order_manager()
        buy = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        await om.process_signal(buy, market_price=100.0)

        sell = Signal.sell(symbol="AAPL", strategy_id="test", quantity=10)
        order = await om.process_signal(sell, market_price=120.0)

        assert order.status == OrderStatus.FILLED
        assert "AAPL" not in om.portfolio.positions

    async def test_partial_sell(self):
        om = _make_order_manager()
        buy = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        await om.process_signal(buy, market_price=100.0)

        sell = Signal.sell(symbol="AAPL", strategy_id="test", quantity=5)
        order = await om.process_signal(sell, market_price=120.0)

        assert order.status == OrderStatus.FILLED
        assert om.portfolio.positions["AAPL"].quantity == 5


class TestOrderManagerCalculateQuantity:
    def test_weight_based_calculation(self):
        om = _make_order_manager()
        signal = Signal.buy(symbol="AAPL", strategy_id="test", weight=0.5)
        qty = om._calculate_quantity(signal, price=100.0)
        expected = int(100_000.0 * 0.5 // 100.0)
        assert qty == expected

    def test_zero_price_returns_zero(self):
        om = _make_order_manager()
        signal = Signal.buy(symbol="AAPL", strategy_id="test", weight=0.5)
        qty = om._calculate_quantity(signal, price=0.0)
        assert qty == 0

    def test_negative_price_returns_zero(self):
        om = _make_order_manager()
        signal = Signal.buy(symbol="AAPL", strategy_id="test", weight=0.5)
        qty = om._calculate_quantity(signal, price=-10.0)
        assert qty == 0

    def test_full_weight(self):
        om = _make_order_manager()
        signal = Signal.buy(symbol="AAPL", strategy_id="test", weight=1.0)
        qty = om._calculate_quantity(signal, price=100.0)
        expected = int(100_000.0 * 1.0 // 100.0)
        assert qty == expected

    def test_tiny_weight(self):
        om = _make_order_manager()
        signal = Signal.buy(symbol="AAPL", strategy_id="test", weight=0.001)
        qty = om._calculate_quantity(signal, price=100.0)
        expected = int(100_000.0 * 0.001 // 100.0)
        assert qty == expected


class TestOrderManagerValidateOrder:
    def test_zero_quantity_rejected(self):
        om = _make_order_manager()
        order = _make_order_with_overrides(quantity=0)
        assert om._validate_order(order, price=100.0) is False

    def test_buy_within_cash_approved(self):
        om = _make_order_manager()
        order = _make_order()
        assert om._validate_order(order, price=100.0) is True

    def test_buy_exceeds_cash_rejected(self):
        om = _make_order_manager()
        order = _make_order_with_overrides(quantity=10_000)
        assert om._validate_order(order, price=100.0) is False

    def test_sell_with_sufficient_held_approved(self):
        om = _make_order_manager()
        om.portfolio.open_position("AAPL", 20, 100.0)
        order = _make_order_with_overrides(side=Side.SELL, quantity=10)
        assert om._validate_order(order, price=100.0) is True

    def test_sell_more_than_held_rejected(self):
        om = _make_order_manager()
        om.portfolio.open_position("AAPL", 5, 100.0)
        order = _make_order_with_overrides(side=Side.SELL, quantity=10)
        assert om._validate_order(order, price=100.0) is False

    def test_sell_without_position_rejected(self):
        om = _make_order_manager()
        order = _make_order_with_overrides(side=Side.SELL, quantity=10)
        assert om._validate_order(order, price=100.0) is False


class TestOrderManagerRejectedNotInCompleted:
    async def test_validation_rejected_not_in_completed(self):
        om = _make_order_manager()
        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=100_000)
        await om.process_signal(signal, market_price=150.0)
        assert len(om.completed_orders) == 0

    async def test_risk_rejected_not_in_completed(self):
        om = _make_order_manager()
        om.risk_engine.circuit_breaker_active = True
        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=1)
        await om.process_signal(signal, market_price=100.0)
        assert len(om.completed_orders) == 0

    async def test_failed_fill_in_completed(self):
        om = _make_order_manager()
        om.set_execution_backend(_FakeBackend(success=False))
        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        await om.process_signal(signal, market_price=100.0)
        assert len(om.completed_orders) == 1


def _make_order(
    symbol: str = "AAPL",
    side: Side = Side.BUY,
    quantity: int = 10,
) -> Order:
    return Order(
        signal_id="sig-1",
        strategy_id="test",
        symbol=symbol,
        side=side,
        quantity=quantity,
    )


def _make_order_with_overrides(**kwargs) -> Order:
    defaults = {
        "signal_id": "sig-1",
        "strategy_id": "test",
        "symbol": "AAPL",
        "side": Side.BUY,
        "quantity": 10,
    }
    defaults.update(kwargs)
    return Order(**defaults)


def _make_order_manager() -> OrderManager:
    portfolio = Portfolio(initial_cash=100_000.0)
    cost_model = DefaultCostModel()
    risk_engine = RiskEngine(max_position_pct=1.0, max_single_order_value=1_000_000.0)
    om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
    om.set_execution_backend(_FakeBackend(success=True, price=100.0))
    return om


class _FakeBackend:
    def __init__(self, success: bool = True, price: float = 100.0, quantity: int | None = None):
        self._success = success
        self._price = price
        self._quantity = quantity

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def execute(self, order, market_price, costs) -> FillResult:
        if self._success:
            fill_qty = self._quantity if self._quantity is not None else order.quantity
            return FillResult(success=True, price=self._price, quantity=fill_qty)
        return FillResult(success=False, reason="Simulated failure")
