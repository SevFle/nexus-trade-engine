"""Tests for the RiskEngine — pre-trade validation, position limits, circuit breakers."""

from __future__ import annotations

import uuid

import pytest

from engine.core.order_manager import Order
from engine.core.portfolio import Portfolio
from engine.core.risk_engine import RiskCheckResult, RiskEngine
from engine.core.signal import Side


def _make_order(
    symbol: str = "AAPL",
    side: Side = Side.BUY,
    quantity: int = 10,
    strategy_id: str = "test",
) -> Order:
    return Order(
        signal_id=str(uuid.uuid4()),
        strategy_id=strategy_id,
        symbol=symbol,
        side=side,
        quantity=quantity,
    )


@pytest.fixture
def portfolio() -> Portfolio:
    return Portfolio(initial_cash=100_000.0)


@pytest.fixture
def engine() -> RiskEngine:
    return RiskEngine(
        max_position_pct=0.20,
        max_daily_trades=3,
        circuit_breaker_drawdown_pct=0.10,
        max_single_order_value=50_000.0,
    )


class TestPositionConcentration:
    def test_order_exceeds_max_position_pct_rejected(self, engine, portfolio):
        portfolio.open_position("AAPL", 10, 100.0)
        portfolio.update_prices({"AAPL": 100.0})

        order = _make_order("AAPL", Side.BUY, 300)
        result = engine.check_order(order, portfolio, market_price=100.0)
        assert not result.approved
        assert "Position" in result.reason or "portfolio" in result.reason.lower()

    def test_within_limit_approved(self, engine, portfolio):
        order = _make_order("AAPL", Side.BUY, 10)
        result = engine.check_order(order, portfolio, market_price=100.0)
        assert result.approved


class TestDailyTradeLimit:
    def test_daily_trade_limit_reached(self, engine, portfolio):
        for _ in range(3):
            order = _make_order("AAPL", Side.BUY, 1)
            result = engine.check_order(order, portfolio, market_price=100.0)
            assert result.approved

        order = _make_order("AAPL", Side.BUY, 1)
        result = engine.check_order(order, portfolio, market_price=100.0)
        assert not result.approved
        assert "Daily trade limit" in result.reason

    def test_reset_daily_counters(self, engine, portfolio):
        for _ in range(3):
            order = _make_order("AAPL", Side.BUY, 1)
            engine.check_order(order, portfolio, market_price=100.0)

        engine.reset_daily_counters()

        order = _make_order("AAPL", Side.BUY, 1)
        result = engine.check_order(order, portfolio, market_price=100.0)
        assert result.approved


class TestCircuitBreaker:
    def test_circuit_breaker_triggers_on_drawdown(self, engine, portfolio):
        portfolio.open_position("AAPL", 100, 500.0)
        portfolio.update_prices({"AAPL": 1.0})

        order = _make_order("AAPL", Side.BUY, 1)
        result = engine.check_order(order, portfolio, market_price=1.0)
        assert not result.approved
        assert "Circuit breaker" in result.reason
        assert engine.circuit_breaker_active

    def test_circuit_breaker_blocks_subsequent_orders(self, engine, portfolio):
        engine.circuit_breaker_active = True

        order = _make_order("AAPL", Side.BUY, 1)
        result = engine.check_order(order, portfolio, market_price=100.0)
        assert not result.approved
        assert "Circuit breaker active" in result.reason

    def test_reset_circuit_breaker(self, engine, portfolio):
        engine.circuit_breaker_active = True
        engine.reset_circuit_breaker()

        order = _make_order("AAPL", Side.BUY, 1)
        result = engine.check_order(order, portfolio, market_price=100.0)
        assert result.approved


class TestOrderValueCap:
    def test_order_value_exceeds_cap(self, portfolio):
        engine = RiskEngine(max_single_order_value=1000.0)
        order = _make_order("AAPL", Side.BUY, 100)
        result = engine.check_order(order, portfolio, market_price=50.0)
        assert not result.approved
        assert "exceeds max" in result.reason

    def test_order_value_within_cap(self, portfolio):
        engine = RiskEngine(max_single_order_value=100_000.0)
        order = _make_order("AAPL", Side.BUY, 10)
        result = engine.check_order(order, portfolio, market_price=100.0)
        assert result.approved


class TestCashWarning:
    def test_large_cash_usage_warning(self, portfolio):
        engine = RiskEngine(max_position_pct=1.0, max_single_order_value=1_000_000.0)
        order = _make_order("AAPL", Side.BUY, 600)
        result = engine.check_order(order, portfolio, market_price=100.0)
        assert result.approved
        assert any("cash" in w.lower() for w in result.warnings)

    def test_no_warning_for_small_orders(self, engine, portfolio):
        order = _make_order("AAPL", Side.BUY, 10)
        result = engine.check_order(order, portfolio, market_price=100.0)
        assert result.approved
        assert len(result.warnings) == 0


class TestMaxOpenPositions:
    def test_max_open_positions_reached(self, portfolio):
        engine = RiskEngine(max_open_positions=2)

        for sym in ["AAPL", "MSFT"]:
            portfolio.open_position(sym, 10, 100.0)

        order = _make_order("GOOGL", Side.BUY, 10)
        result = engine.check_order(order, portfolio, market_price=100.0)
        assert not result.approved
        assert "Max open positions" in result.reason

    def test_existing_symbol_does_not_count_as_new(self, portfolio):
        engine = RiskEngine(max_open_positions=1)
        portfolio.open_position("AAPL", 10, 100.0)

        order = _make_order("AAPL", Side.BUY, 5)
        result = engine.check_order(order, portfolio, market_price=100.0)
        assert result.approved


class TestRiskCheckResultDefaults:
    def test_warnings_defaults_to_empty_list(self):
        result = RiskCheckResult(approved=True)
        assert result.warnings == []

    def test_drawdown_zero_when_initial_cash_zero(self):
        zero_portfolio = Portfolio(initial_cash=0)
        engine = RiskEngine()
        order = _make_order("AAPL", Side.BUY, 1)
        result = engine.check_order(order, zero_portfolio, market_price=100.0)
        assert result.approved
