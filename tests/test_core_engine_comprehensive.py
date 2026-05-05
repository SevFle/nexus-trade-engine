"""
Comprehensive tests for core engine modules — SEV-264.

Covers edge cases, error conditions, and boundary values for:
- Money / CostBreakdown / TaxLot (cost_model data types)
- DefaultCostModel (commission, spread, slippage, tax, wash sale, dividends)
- Portfolio (open/close/update/snapshot/tax lots)
- OrderManager (full lifecycle, validation, cost rejection, execution failure)
- RiskEngine (drawdown, circuit breaker, position limits, daily caps)
- BacktestRunner (config, result, summary)
- Tax lot tracking (FIFO/LIFO/SPECIFIC_LOT, wash sale integration)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from engine.core.backtest_runner import BacktestConfig, BacktestResult, BacktestRunner
from engine.core.cost_model import (
    CostBreakdown,
    DefaultCostModel,
    ICostModel,
    Money,
    TaxLot,
    TaxMethod,
)
from engine.core.execution.base import FillResult
from engine.core.order_manager import Order, OrderManager, OrderStatus, OrderType
from engine.core.portfolio import Portfolio, PortfolioSnapshot, Position
from engine.core.risk_engine import RiskCheckResult, RiskEngine
from engine.core.signal import Side, Signal
from engine.data.feeds import MarketDataProvider


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════


class _FakeBackend:
    def __init__(self, success=True, price=100.0, quantity=10):
        self._success = success
        self._price = price
        self._quantity = quantity

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def execute(self, order, market_price, costs):
        if self._success:
            return FillResult(success=True, price=self._price, quantity=self._quantity)
        return FillResult(success=False, reason="Simulated failure")


class _SynthProvider(MarketDataProvider):
    def __init__(self, df):
        self._df = df

    async def get_latest_price(self, symbol):
        return float(self._df["close"].iloc[-1]) if not self._df.empty else None

    async def get_ohlcv(self, symbol, period="1y", interval="1d"):
        return self._df

    async def get_multiple_prices(self, symbols):
        if self._df.empty:
            return {}
        return {symbols[0]: float(self._df["close"].iloc[-1])}


def _make_ohlcv(n_bars=100, base=100.0):
    dates = pd.bdate_range("2025-01-01", periods=n_bars)
    rng = np.random.default_rng(42)
    close = base + np.cumsum(rng.normal(0, 0.5, n_bars))
    return pd.DataFrame(
        {
            "open": close - 0.1,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": rng.integers(100_000, 1_000_000, n_bars),
        },
        index=dates,
    )


# ═══════════════════════════════════════════════════════════════════════
# 1. Money data type
# ═══════════════════════════════════════════════════════════════════════


class TestMoney:
    def test_add_same_currency(self):
        result = Money(10.0, "USD") + Money(20.0, "USD")
        assert result.amount == 30.0
        assert result.currency == "USD"

    def test_sub_same_currency(self):
        result = Money(50.0, "USD") - Money(20.0, "USD")
        assert result.amount == 30.0

    def test_is_zero_true(self):
        assert Money(0.0).is_zero is True
        assert Money(1e-11).is_zero is True

    def test_is_zero_false(self):
        assert Money(0.01).is_zero is False

    def test_as_pct_of_normal(self):
        m = Money(5.0)
        assert m.as_pct_of(100.0) == pytest.approx(5.0)

    def test_as_pct_of_zero_total(self):
        m = Money(5.0)
        assert m.as_pct_of(0.0) == 0.0

    def test_as_pct_of_negative(self):
        m = Money(50.0)
        assert m.as_pct_of(200.0) == pytest.approx(25.0)

    def test_default_currency(self):
        m = Money(10.0)
        assert m.currency == "USD"


# ═══════════════════════════════════════════════════════════════════════
# 2. CostBreakdown
# ═══════════════════════════════════════════════════════════════════════


class TestCostBreakdown:
    def test_default_all_zero(self):
        cb = CostBreakdown()
        assert cb.total.amount == 0.0
        assert cb.total_without_tax.amount == 0.0

    def test_total_sums_all_components(self):
        cb = CostBreakdown(
            commission=Money(1.0),
            spread=Money(2.0),
            slippage=Money(3.0),
            exchange_fee=Money(0.5),
            tax_estimate=Money(4.0),
            currency_conversion=Money(1.5),
        )
        assert cb.total.amount == pytest.approx(12.0)

    def test_total_without_tax_excludes_tax(self):
        cb = CostBreakdown(
            commission=Money(1.0),
            tax_estimate=Money(3.0),
        )
        assert cb.total.amount == pytest.approx(4.0)
        assert cb.total_without_tax.amount == pytest.approx(1.0)

    def test_as_dict_keys(self):
        cb = CostBreakdown(commission=Money(1.5))
        d = cb.as_dict()
        assert set(d.keys()) == {
            "commission",
            "spread",
            "slippage",
            "exchange_fee",
            "tax_estimate",
            "currency_conversion",
            "total",
        }

    def test_as_dict_values(self):
        cb = CostBreakdown(
            commission=Money(1.0),
            spread=Money(2.0),
        )
        d = cb.as_dict()
        assert d["commission"] == 1.0
        assert d["spread"] == 2.0
        assert d["total"] == 3.0


# ═══════════════════════════════════════════════════════════════════════
# 3. TaxLot
# ═══════════════════════════════════════════════════════════════════════


class TestTaxLot:
    def test_cost_basis(self):
        lot = TaxLot(symbol="AAPL", quantity=100, purchase_price=150.0, purchase_date=datetime.now(UTC))
        assert lot.cost_basis == 15_000.0

    def test_is_long_term_true(self):
        lot = TaxLot(
            symbol="AAPL",
            quantity=10,
            purchase_price=100.0,
            purchase_date=datetime.now(UTC) - timedelta(days=400),
        )
        assert lot.is_long_term() is True

    def test_is_long_term_false(self):
        lot = TaxLot(
            symbol="AAPL",
            quantity=10,
            purchase_price=100.0,
            purchase_date=datetime.now(UTC) - timedelta(days=100),
        )
        assert lot.is_long_term() is False

    def test_is_long_term_exactly_365(self):
        lot = TaxLot(
            symbol="AAPL",
            quantity=10,
            purchase_price=100.0,
            purchase_date=datetime.now(UTC) - timedelta(days=365),
        )
        assert lot.is_long_term() is True

    def test_is_long_term_with_as_of(self):
        purchase = datetime(2024, 1, 1, tzinfo=UTC)
        lot = TaxLot(symbol="AAPL", quantity=10, purchase_price=100.0, purchase_date=purchase)
        assert lot.is_long_term(as_of=datetime(2025, 1, 1, tzinfo=UTC)) is True
        assert lot.is_long_term(as_of=datetime(2024, 6, 1, tzinfo=UTC)) is False

    def test_default_lot_id(self):
        lot = TaxLot(symbol="AAPL", quantity=10, purchase_price=100.0, purchase_date=datetime.now(UTC))
        assert lot.lot_id == ""


# ═══════════════════════════════════════════════════════════════════════
# 4. DefaultCostModel — additional edge cases
# ═══════════════════════════════════════════════════════════════════════


class TestDefaultCostModelEdgeCases:
    def test_estimate_pct_zero_price(self):
        model = DefaultCostModel(commission_per_trade=1.0)
        pct = model.estimate_pct("AAPL", 0.0, "buy")
        assert pct >= 0

    def test_estimate_spread_side_irrelevant(self):
        model = DefaultCostModel(spread_bps=10.0)
        buy_spread = model.estimate_spread("AAPL", 100.0, "buy")
        sell_spread = model.estimate_spread("AAPL", 100.0, "sell")
        assert buy_spread.amount == sell_spread.amount

    def test_estimate_tax_partial_lot_consumption(self):
        model = DefaultCostModel(short_term_tax_rate=0.37, long_term_tax_rate=0.20)
        sell_date = datetime.now(UTC)
        lots = [
            TaxLot(
                symbol="AAPL",
                quantity=100,
                purchase_price=100.0,
                purchase_date=sell_date - timedelta(days=30),
            ),
            TaxLot(
                symbol="AAPL",
                quantity=100,
                purchase_price=120.0,
                purchase_date=sell_date - timedelta(days=30),
            ),
        ]
        tax = model.estimate_tax("AAPL", 150.0, 150, lots, TaxMethod.FIFO, sell_date=sell_date)
        expected_first_lot = (150.0 - 100.0) * 100 * 0.37
        expected_second_lot = (150.0 - 120.0) * 50 * 0.37
        assert tax.amount == pytest.approx(expected_first_lot + expected_second_lot)

    def test_estimate_tax_no_gain_no_tax(self):
        model = DefaultCostModel()
        sell_date = datetime.now(UTC)
        lots = [
            TaxLot(
                symbol="AAPL",
                quantity=100,
                purchase_price=150.0,
                purchase_date=sell_date - timedelta(days=30),
            ),
        ]
        tax = model.estimate_tax("AAPL", 150.0, 100, lots, TaxMethod.FIFO, sell_date=sell_date)
        assert tax.amount == 0.0

    def test_wash_sale_no_replacement_lots(self):
        model = DefaultCostModel()
        sell_date = datetime.now(UTC)
        result = model.calculate_wash_sale_adjustment("AAPL", sell_date, -500.0, [])
        assert result["is_wash_sale"] is False
        assert result["adjustment"] == 0.0
        assert result["replacement_lots"] == []

    def test_wash_sale_zero_loss(self):
        model = DefaultCostModel()
        sell_date = datetime.now(UTC)
        result = model.calculate_wash_sale_adjustment("AAPL", sell_date, 0.0, [
            {"symbol": "AAPL", "date": sell_date - timedelta(days=5), "price": 100.0, "quantity": 10},
        ])
        assert result["is_wash_sale"] is False

    def test_wash_sale_multiple_replacement_lots(self):
        model = DefaultCostModel()
        sell_date = datetime.now(UTC)
        loss = -1000.0
        buy_history = [
            {"symbol": "AAPL", "date": sell_date - timedelta(days=10), "price": 145.0, "quantity": 50},
            {"symbol": "AAPL", "date": sell_date - timedelta(days=5), "price": 146.0, "quantity": 50},
        ]
        result = model.calculate_wash_sale_adjustment("AAPL", sell_date, loss, buy_history)
        assert result["is_wash_sale"] is True
        assert result["adjustment"] == 1000.0
        assert result["adjustment_per_share"] == 10.0
        assert len(result["replacement_lots"]) == 2

    def test_dividend_tax_zero_amount(self):
        model = DefaultCostModel()
        tax = model.estimate_dividend_tax(0.0, is_qualified=True)
        assert tax.amount == 0.0

    def test_custom_parameters_propagate(self):
        model = DefaultCostModel(
            commission_per_trade=5.0,
            spread_bps=20.0,
            slippage_bps=15.0,
            exchange_fee_per_share=0.005,
        )
        assert model.estimate_commission("X", 1, 100).amount == 5.0
        spread = model.estimate_spread("X", 100.0, "buy")
        assert spread.amount == pytest.approx(100.0 * 20.0 / 10_000)
        slip = model.estimate_slippage("X", 100, 100.0, 0)
        assert slip.amount == pytest.approx(100.0 * 15.0 / 10_000 * 100)

    def test_check_wash_sale_empty_history(self):
        model = DefaultCostModel()
        sell_date = datetime.now(UTC)
        assert model.check_wash_sale("AAPL", sell_date, []) is False

    def test_check_wash_sale_exact_boundary(self):
        model = DefaultCostModel(wash_sale_window_days=30)
        sell_date = datetime.now(UTC)
        buy_at_boundary = sell_date - timedelta(days=30)
        assert model.check_wash_sale("AAPL", sell_date, [
            {"symbol": "AAPL", "date": buy_at_boundary},
        ]) is True

    def test_check_wash_sale_one_day_past_boundary(self):
        model = DefaultCostModel(wash_sale_window_days=30)
        sell_date = datetime.now(UTC)
        buy_past = sell_date - timedelta(days=31)
        assert model.check_wash_sale("AAPL", sell_date, [
            {"symbol": "AAPL", "date": buy_past},
        ]) is False

    def test_estimate_total_no_tax_in_breakdown(self):
        model = DefaultCostModel()
        cb = model.estimate_total("AAPL", 100, 150.0, "buy", 1_000_000)
        assert cb.tax_estimate.amount == 0.0
        assert cb.total.amount > 0

    def test_estimate_tax_specific_lot_uses_original_order(self):
        model = DefaultCostModel(short_term_tax_rate=0.37)
        sell_date = datetime.now(UTC)
        lot_a = TaxLot(
            symbol="AAPL",
            quantity=50,
            purchase_price=100.0,
            purchase_date=sell_date - timedelta(days=10),
        )
        lot_b = TaxLot(
            symbol="AAPL",
            quantity=50,
            purchase_price=120.0,
            purchase_date=sell_date - timedelta(days=5),
        )
        tax = model.estimate_tax(
            "AAPL", 150.0, 100, [lot_b, lot_a], TaxMethod.SPECIFIC_LOT, sell_date=sell_date
        )
        gain_b = (150.0 - 120.0) * 50 * 0.37
        gain_a = (150.0 - 100.0) * 50 * 0.37
        assert tax.amount == pytest.approx(gain_b + gain_a)

    def test_estimate_tax_lifo_ordering(self):
        model = DefaultCostModel(short_term_tax_rate=0.37)
        sell_date = datetime.now(UTC)
        lots = [
            TaxLot(symbol="AAPL", quantity=100, purchase_price=100.0,
                   purchase_date=sell_date - timedelta(days=30)),
            TaxLot(symbol="AAPL", quantity=100, purchase_price=130.0,
                   purchase_date=sell_date - timedelta(days=5)),
        ]
        tax = model.estimate_tax("AAPL", 150.0, 150, lots, TaxMethod.LIFO, sell_date=sell_date)
        gain_new = (150.0 - 130.0) * 100 * 0.37
        gain_old = (150.0 - 100.0) * 50 * 0.37
        assert tax.amount == pytest.approx(gain_new + gain_old)


# ═══════════════════════════════════════════════════════════════════════
# 5. Position
# ═══════════════════════════════════════════════════════════════════════


class TestPosition:
    def test_is_zero(self):
        pos = Position(symbol="AAPL", quantity=0)
        assert pos.is_zero is True

    def test_is_not_zero(self):
        pos = Position(symbol="AAPL", quantity=10)
        assert pos.is_zero is False

    def test_market_value_with_current_price(self):
        pos = Position(symbol="AAPL", quantity=100, avg_cost=50.0, current_price=150.0)
        assert pos.market_value == 15_000.0

    def test_market_value_falls_back_to_avg_cost(self):
        pos = Position(symbol="AAPL", quantity=100, avg_cost=50.0, current_price=0.0)
        assert pos.market_value == 5_000.0

    def test_market_value_zero_quantity(self):
        pos = Position(symbol="AAPL", quantity=0, avg_cost=50.0, current_price=150.0)
        assert pos.market_value == 0.0


# ═══════════════════════════════════════════════════════════════════════
# 6. Portfolio — comprehensive edge cases
# ═══════════════════════════════════════════════════════════════════════


class TestPortfolioInit:
    def test_default_initial_cash(self):
        p = Portfolio()
        assert p.cash == 100_000.0
        assert p.initial_cash == 100_000.0

    def test_custom_initial_cash(self):
        p = Portfolio(initial_cash=50_000.0)
        assert p.cash == 50_000.0

    def test_default_cost_model_created(self):
        p = Portfolio()
        assert p._cost_model is not None

    def test_default_tax_method(self):
        p = Portfolio()
        assert p.tax_method == TaxMethod.FIFO


class TestPortfolioOpenPosition:
    def test_creates_position(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 100, 150.0)
        assert "AAPL" in p.positions
        assert p.positions["AAPL"].quantity == 100
        assert p.positions["AAPL"].avg_cost == 150.0

    def test_deducts_cash(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 100, 150.0)
        assert p.cash == 100_000 - 100 * 150.0

    def test_deducts_cash_with_cost(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 100, 150.0, cost=50.0)
        assert p.cash == 100_000 - 100 * 150.0 - 50.0

    def test_insufficient_cash_raises(self):
        p = Portfolio(initial_cash=100)
        with pytest.raises(ValueError, match="Insufficient cash"):
            p.open_position("AAPL", 100, 150.0)

    def test_adds_to_existing_position(self):
        p = Portfolio(initial_cash=200_000)
        p.open_position("AAPL", 100, 100.0)
        p.open_position("AAPL", 100, 200.0)
        pos = p.positions["AAPL"]
        assert pos.quantity == 200
        assert pos.avg_cost == 150.0

    def test_creates_tax_lot(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2026, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 50, 100.0)
        lots = p.get_tax_lots("AAPL")
        assert len(lots) == 1
        assert lots[0].quantity == 50
        assert lots[0].purchase_price == 100.0

    def test_multiple_buys_create_multiple_lots(self):
        p = Portfolio(initial_cash=200_000)
        p.transaction_date = datetime(2026, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 50, 100.0)
        p.transaction_date = datetime(2026, 2, 1, tzinfo=UTC)
        p.open_position("AAPL", 50, 120.0)
        lots = p.get_tax_lots("AAPL")
        assert len(lots) == 2

    def test_records_trade_history(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2026, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 150.0)
        assert len(p.trade_history) == 1
        assert p.trade_history[0].side == "buy"
        assert p.trade_history[0].symbol == "AAPL"
        assert p.trade_history[0].quantity == 100

    def test_returns_lot_uuid(self):
        p = Portfolio(initial_cash=100_000)
        lot_id = p.open_position("AAPL", 100, 150.0)
        assert isinstance(lot_id, uuid.UUID)

    def test_unknown_symbol_returns_empty_lots(self):
        p = Portfolio()
        assert p.get_tax_lots("UNKNOWN") == []


class TestPortfolioClosePosition:
    def test_removes_position_on_full_close(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 100, 150.0)
        p.close_position("AAPL", 100, 160.0)
        assert "AAPL" not in p.positions

    def test_partial_close_reduces_quantity(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 100, 150.0)
        p.close_position("AAPL", 50, 160.0)
        assert p.positions["AAPL"].quantity == 50

    def test_no_position_raises(self):
        p = Portfolio(initial_cash=100_000)
        with pytest.raises(ValueError, match="No position"):
            p.close_position("AAPL", 10, 150.0)

    def test_oversell_raises(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 10, 100.0)
        with pytest.raises(ValueError, match="Cannot sell"):
            p.close_position("AAPL", 20, 150.0)

    def test_insufficient_tax_lots_raises(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2026, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 10, 100.0)
        with pytest.raises(ValueError, match="Cannot sell"):
            p.close_position("AAPL", 20, 150.0)

    def test_adds_cash_on_sell(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 100, 100.0)
        cash_before = p.cash
        p.close_position("AAPL", 100, 150.0)
        assert p.cash > cash_before

    def test_deducts_cost_and_tax_on_sell(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 100, 100.0)
        p.close_position("AAPL", 100, 150.0, cost=10.0, tax=5.0)
        expected_cash = 100_000 - 100 * 100.0 + 100 * 150.0 - 10.0 - 5.0
        assert p.cash == pytest.approx(expected_cash)

    def test_realized_pnl_on_profit(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 100, 100.0)
        p.close_position("AAPL", 100, 150.0)
        assert p.realized_pnl > 0

    def test_realized_pnl_on_loss(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 100, 150.0)
        p.close_position("AAPL", 100, 100.0)
        assert p.realized_pnl < 0

    def test_records_sell_trade_history(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2026, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 100.0)
        p.close_position("AAPL", 100, 150.0)
        assert len(p.trade_history) == 2
        sell_rec = p.trade_history[1]
        assert sell_rec.side == "sell"
        assert sell_rec.quantity == 100

    def test_consumed_lots_returned(self):
        p = Portfolio(initial_cash=100_000, tax_method=TaxMethod.FIFO)
        base = datetime(2026, 1, 1, tzinfo=UTC)
        p.transaction_date = base
        p.open_position("AAPL", 50, 80.0)
        p.transaction_date = base + timedelta(days=1)
        p.open_position("AAPL", 50, 120.0)
        p.transaction_date = base + timedelta(days=10)
        consumed = p.close_position("AAPL", 60, 150.0)
        assert len(consumed) == 2
        assert consumed[0]["purchase_price"] == 80.0
        assert consumed[0]["quantity"] == 50
        assert consumed[1]["purchase_price"] == 120.0
        assert consumed[1]["quantity"] == 10


class TestPortfolioState:
    def test_total_value_cash_only(self):
        p = Portfolio(initial_cash=100_000)
        assert p.total_value == 100_000.0

    def test_total_value_with_positions(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 100, 100.0)
        p.update_prices({"AAPL": 150.0})
        assert p.total_value == p.cash + 100 * 150.0

    def test_total_return_pct_positive(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 100, 100.0)
        p.update_prices({"AAPL": 150.0})
        assert p.total_return_pct > 0

    def test_total_return_pct_zero_initial(self):
        p = Portfolio(initial_cash=0)
        assert p.total_return_pct == 0.0

    def test_update_prices_ignores_unknown_symbols(self):
        p = Portfolio(initial_cash=100_000)
        p.update_prices({"UNKNOWN": 999.0})

    def test_set_tax_method(self):
        p = Portfolio()
        p.set_tax_method(TaxMethod.LIFO)
        assert p.tax_method == TaxMethod.LIFO


class TestPortfolioSnapshot:
    def test_snapshot_captures_state(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 100, 150.0)
        p.update_prices({"AAPL": 160.0})
        snap = p.snapshot()
        assert snap.cash == p.cash
        assert "AAPL" in snap.positions
        assert snap.total_value == p.total_value
        assert snap.realized_pnl == p.realized_pnl

    def test_allocation_weight(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 100, 100.0)
        p.update_prices({"AAPL": 100.0})
        snap = p.snapshot()
        weight = snap.allocation_weight("AAPL")
        assert weight > 0

    def test_allocation_weight_unknown_symbol(self):
        snap = PortfolioSnapshot(
            cash=100_000.0,
            positions={},
            total_value=100_000.0,
            total_return_pct=0.0,
            realized_pnl=0.0,
        )
        assert snap.allocation_weight("UNKNOWN") == 0.0

    def test_allocation_weight_zero_total_value(self):
        snap = PortfolioSnapshot(
            cash=0.0,
            positions={"AAPL": {"quantity": 10, "avg_cost": 100.0, "current_price": 100.0}},
            total_value=0.0,
            total_return_pct=0.0,
            realized_pnl=0.0,
        )
        assert snap.allocation_weight("AAPL") == 0.0

    def test_summary_string(self):
        snap = PortfolioSnapshot(
            cash=100_000.0,
            positions={},
            total_value=100_000.0,
            total_return_pct=0.0,
            realized_pnl=0.0,
        )
        s = snap.summary()
        assert "Cash" in s
        assert "Value" in s

    def test_portfolio_id_propagation(self):
        pid = uuid.uuid4()
        p = Portfolio(initial_cash=100_000, portfolio_id=pid)
        assert p.portfolio_id == pid


# ═══════════════════════════════════════════════════════════════════════
# 7. OrderManager — comprehensive lifecycle tests
# ═══════════════════════════════════════════════════════════════════════


class TestOrderDataclass:
    def test_order_defaults(self):
        order = Order(signal_id="s1", strategy_id="strat", symbol="AAPL", side=Side.BUY, quantity=10)
        assert order.status == OrderStatus.PENDING
        assert order.order_type == OrderType.MARKET
        assert order.limit_price is None
        assert order.cost_breakdown is None
        assert order.fill_price is None
        assert order.fill_quantity is None

    def test_order_transition(self):
        order = Order(signal_id="s1", strategy_id="strat", symbol="AAPL", side=Side.BUY, quantity=10)
        order.transition(OrderStatus.VALIDATED, "passed")
        assert order.status == OrderStatus.VALIDATED
        assert len(order.status_history) == 1
        assert order.status_history[0]["from"] == OrderStatus.PENDING
        assert order.status_history[0]["to"] == OrderStatus.VALIDATED
        assert order.status_history[0]["reason"] == "passed"

    def test_order_multiple_transitions(self):
        order = Order(signal_id="s1", strategy_id="strat", symbol="AAPL", side=Side.BUY, quantity=10)
        order.transition(OrderStatus.VALIDATED)
        order.transition(OrderStatus.COSTED)
        order.transition(OrderStatus.RISK_APPROVED)
        assert len(order.status_history) == 3


class TestOrderManagerIntegration:
    @pytest.fixture
    def om(self):
        p = Portfolio(initial_cash=100_000)
        cm = DefaultCostModel()
        re = RiskEngine(max_position_pct=1.0, max_single_order_value=1_000_000)
        mgr = OrderManager(cost_model=cm, risk_engine=re, portfolio=p)
        mgr.set_execution_backend(_FakeBackend(success=True, price=100.0, quantity=10))
        return mgr

    async def test_full_buy_lifecycle(self, om):
        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        order = await om.process_signal(signal, market_price=100.0)
        assert order.status == OrderStatus.FILLED
        assert order.fill_price == 100.0
        assert order.fill_quantity == 10

    async def test_sell_after_buy(self, om):
        buy = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        await om.process_signal(buy, market_price=100.0)

        sell = Signal.sell(symbol="AAPL", strategy_id="test", quantity=10)
        order = await om.process_signal(sell, market_price=110.0)
        assert order.status == OrderStatus.FILLED
        assert order.side == Side.SELL

    async def test_zero_quantity_rejected(self, om):
        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=0, weight=0.0)
        order = await om.process_signal(signal, market_price=100.0)
        assert order.quantity == 0
        assert order.status == OrderStatus.REJECTED

    async def test_failed_order_in_completed(self):
        p = Portfolio(initial_cash=100_000)
        cm = DefaultCostModel()
        re = RiskEngine(max_position_pct=1.0, max_single_order_value=1_000_000)
        mgr = OrderManager(cost_model=cm, risk_engine=re, portfolio=p)
        mgr.set_execution_backend(_FakeBackend(success=False))
        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        order = await mgr.process_signal(signal, market_price=100.0)
        assert order.status == OrderStatus.FAILED
        assert len(mgr.completed_orders) == 1

    async def test_cost_breakdown_populated(self, om):
        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        order = await om.process_signal(signal, market_price=100.0)
        assert order.cost_breakdown is not None
        assert "total" in order.cost_breakdown

    async def test_calculate_quantity_with_weight(self):
        p = Portfolio(initial_cash=100_000)
        cm = DefaultCostModel()
        re = RiskEngine(max_position_pct=1.0, max_single_order_value=1_000_000)
        mgr = OrderManager(cost_model=cm, risk_engine=re, portfolio=p)
        mgr.set_execution_backend(_FakeBackend(success=True, price=50.0, quantity=1000))
        signal = Signal.buy(symbol="AAPL", strategy_id="test", weight=0.5)
        order = await mgr.process_signal(signal, market_price=50.0)
        expected_qty = int(100_000 * 0.5 // 50.0)
        assert order.quantity == expected_qty

    async def test_process_signal_zero_price_yields_zero_quantity(self):
        p = Portfolio(initial_cash=100_000)
        cm = DefaultCostModel()
        re = RiskEngine(max_position_pct=1.0, max_single_order_value=1_000_000)
        mgr = OrderManager(cost_model=cm, risk_engine=re, portfolio=p)
        mgr.set_execution_backend(_FakeBackend())
        signal = Signal.buy(symbol="AAPL", strategy_id="test", weight=0.5)
        order = await mgr.process_signal(signal, market_price=0.0)
        assert order.status == OrderStatus.REJECTED

    async def test_backend_not_configured_fails(self):
        p = Portfolio(initial_cash=100_000)
        cm = DefaultCostModel()
        re = RiskEngine(max_position_pct=1.0, max_single_order_value=1_000_000)
        mgr = OrderManager(cost_model=cm, risk_engine=re, portfolio=p)
        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        order = await mgr.process_signal(signal, market_price=100.0)
        assert order.status == OrderStatus.FAILED

    async def test_sell_order_cost_includes_non_tax(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 100, 80.0)
        cm = DefaultCostModel()
        re = RiskEngine(max_position_pct=1.0, max_single_order_value=1_000_000)
        mgr = OrderManager(cost_model=cm, risk_engine=re, portfolio=p)
        mgr.set_execution_backend(_FakeBackend(success=True, price=150.0, quantity=10))
        signal = Signal.sell(symbol="AAPL", strategy_id="test", quantity=10)
        order = await mgr.process_signal(signal, market_price=150.0)
        assert order.status == OrderStatus.FILLED


# ═══════════════════════════════════════════════════════════════════════
# 8. RiskEngine — comprehensive edge cases
# ═══════════════════════════════════════════════════════════════════════


class TestRiskEngineDrawdown:
    def test_drawdown_calculation(self):
        p = Portfolio(initial_cash=100_000)
        re = RiskEngine()
        dd = re._calculate_drawdown(p)
        assert dd == 0.0

    def test_drawdown_with_loss(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 100, 500.0)
        p.update_prices({"AAPL": 400.0})
        re = RiskEngine()
        dd = re._calculate_drawdown(p)
        assert dd > 0

    def test_drawdown_zero_initial(self):
        p = Portfolio(initial_cash=0)
        re = RiskEngine()
        dd = re._calculate_drawdown(p)
        assert dd == 0.0

    def test_drawdown_no_loss(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 100, 100.0)
        p.update_prices({"AAPL": 200.0})
        re = RiskEngine()
        dd = re._calculate_drawdown(p)
        assert dd == 0.0


class TestRiskEngineSellOrders:
    def test_sell_does_not_count_as_new_position(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 100, 100.0)
        re = RiskEngine(max_open_positions=1)
        order = Order(
            signal_id="s1", strategy_id="t", symbol="AAPL", side=Side.SELL, quantity=10
        )
        result = re.check_order(order, p, 100.0)
        assert result.approved

    def test_sell_concentration_not_blocked(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 1000, 100.0)
        p.update_prices({"AAPL": 100.0})
        re = RiskEngine(max_position_pct=0.20)
        order = Order(
            signal_id="s1", strategy_id="t", symbol="AAPL", side=Side.SELL, quantity=100
        )
        result = re.check_order(order, p, 100.0)
        assert result.approved


class TestRiskEngineDailyCount:
    def test_count_increments_on_approval(self):
        p = Portfolio(initial_cash=100_000)
        re = RiskEngine(max_daily_trades=5)
        order = Order(
            signal_id="s1", strategy_id="t", symbol="AAPL", side=Side.BUY, quantity=10
        )
        re.check_order(order, p, 100.0)
        assert re.daily_trade_count == 1

    def test_count_does_not_increment_on_rejection(self):
        p = Portfolio(initial_cash=100_000)
        re = RiskEngine(max_daily_trades=0)
        order = Order(
            signal_id="s1", strategy_id="t", symbol="AAPL", side=Side.BUY, quantity=10
        )
        re.check_order(order, p, 100.0)
        assert re.daily_trade_count == 0


class TestRiskCheckResult:
    def test_post_init_sets_warnings(self):
        r = RiskCheckResult(approved=True)
        assert r.warnings == []

    def test_custom_warnings(self):
        r = RiskCheckResult(approved=False, reason="test", warnings=["w1", "w2"])
        assert len(r.warnings) == 2


class TestRiskEngineCircuitBreakerEdgeCases:
    def test_circuit_breaker_exactly_at_threshold(self):
        p = Portfolio(initial_cash=100_000)
        re = RiskEngine(circuit_breaker_drawdown_pct=0.10, max_position_pct=1.0, max_single_order_value=1_000_000)
        p.open_position("AAPL", 100, 500.0)
        p.update_prices({"AAPL": 400.0})
        order = Order(signal_id="s1", strategy_id="t", symbol="AAPL", side=Side.BUY, quantity=1)
        result = re.check_order(order, p, 400.0)
        assert not result.approved
        assert re.circuit_breaker_active

    def test_circuit_breaker_below_threshold_passes(self):
        p = Portfolio(initial_cash=100_000)
        re = RiskEngine(circuit_breaker_drawdown_pct=0.10, max_position_pct=1.0, max_single_order_value=1_000_000)
        p.open_position("AAPL", 100, 500.0)
        p.update_prices({"AAPL": 480.0})
        order = Order(signal_id="s1", strategy_id="t", symbol="AAPL", side=Side.BUY, quantity=1)
        result = re.check_order(order, p, 480.0)
        assert result.approved

    def test_reset_circuit_breaker_allows_trades(self):
        p = Portfolio(initial_cash=100_000)
        re = RiskEngine(circuit_breaker_drawdown_pct=0.10)
        re.circuit_breaker_active = True
        re.reset_circuit_breaker()
        order = Order(signal_id="s1", strategy_id="t", symbol="AAPL", side=Side.BUY, quantity=1)
        result = re.check_order(order, p, 100.0)
        assert result.approved

    def test_reset_daily_counters(self):
        p = Portfolio(initial_cash=100_000)
        re = RiskEngine(max_daily_trades=1)
        order = Order(signal_id="s1", strategy_id="t", symbol="AAPL", side=Side.BUY, quantity=1)
        re.check_order(order, p, 100.0)
        assert re.daily_trade_count == 1
        re.reset_daily_counters()
        assert re.daily_trade_count == 0


# ═══════════════════════════════════════════════════════════════════════
# 9. BacktestConfig / BacktestResult / BacktestSummary
# ═══════════════════════════════════════════════════════════════════════


class TestBacktestConfig:
    def test_defaults(self):
        cfg = BacktestConfig(strategy_name="test", symbol="AAPL", start_date="2024-01-01", end_date="2024-12-31")
        assert cfg.initial_capital == 100_000.0
        assert cfg.min_bars == 50
        assert cfg.debug is False
        assert cfg.random_seed == 42
        assert cfg.portfolio_id is None

    def test_custom_values(self):
        pid = uuid.uuid4()
        cfg = BacktestConfig(
            strategy_name="test",
            symbol="MSFT",
            start_date="2023-01-01",
            end_date="2023-12-31",
            initial_capital=50_000.0,
            min_bars=20,
            debug=True,
            random_seed=99,
            portfolio_id=pid,
        )
        assert cfg.initial_capital == 50_000.0
        assert cfg.min_bars == 20
        assert cfg.debug is True
        assert cfg.random_seed == 99
        assert cfg.portfolio_id == pid


class TestBacktestResult:
    def test_defaults(self):
        r = BacktestResult()
        assert r.equity_curve == []
        assert r.trades == []
        assert r.metrics == {}
        assert r.final_capital == 0.0
        assert r.total_return_pct == 0.0
        assert r.portfolio_id is None

    def test_with_portfolio_id(self):
        pid = uuid.uuid4()
        r = BacktestResult(portfolio_id=pid)
        assert r.portfolio_id == pid


class TestBacktestRunnerIntegration:
    async def test_runner_processes_all_bars(self):
        df = _make_ohlcv(100)

        class HoldStrat:
            name = "hold"
            version = "1.0"

            def on_bar(self, state, portfolio):
                return []

        provider = _SynthProvider(df)
        config = BacktestConfig(
            strategy_name="hold",
            symbol="TEST",
            start_date=str(df.index[0].date()),
            end_date=str(df.index[-1].date()),
            min_bars=5,
        )
        runner = BacktestRunner(config=config, strategy=HoldStrat(), provider=provider)
        result = await runner.run()
        assert len(result.equity_curve) > 0
        assert result.final_capital == pytest.approx(100_000.0, abs=0.01)

    async def test_runner_with_tz_aware_data(self):
        dates = pd.bdate_range("2024-01-01", periods=60, tz="America/New_York")
        rng = np.random.default_rng(42)
        close = 100 + np.cumsum(rng.normal(0, 0.5, 60))
        df = pd.DataFrame(
            {"open": close - 0.1, "high": close + 0.5, "low": close - 0.5, "close": close, "volume": rng.integers(100_000, 1_000_000, 60)},
            index=dates,
        )

        class HoldStrat:
            name = "hold"
            version = "1.0"

            def on_bar(self, state, portfolio):
                return []

        provider = _SynthProvider(df)
        config = BacktestConfig(strategy_name="hold", symbol="TEST", start_date="2024-01-01", end_date="2024-03-31", min_bars=5)
        runner = BacktestRunner(config=config, strategy=HoldStrat(), provider=provider)
        result = await runner.run()
        assert len(result.equity_curve) > 0


# ═══════════════════════════════════════════════════════════════════════
# 10. Tax Lot Tracking — FIFO/LIFO edge cases
# ═══════════════════════════════════════════════════════════════════════


class TestTaxLotFIFOLIFOEdgeCases:
    def test_fifo_single_lot_partial_sell(self):
        p = Portfolio(initial_cash=100_000, tax_method=TaxMethod.FIFO)
        p.transaction_date = datetime(2026, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 100.0)
        p.transaction_date = datetime(2026, 2, 1, tzinfo=UTC)
        consumed = p.close_position("AAPL", 1, 150.0)
        assert consumed[0]["quantity"] == 1
        assert consumed[0]["purchase_price"] == 100.0
        assert p.get_tax_lots("AAPL")[0].quantity == 99

    def test_lifo_single_lot_partial_sell(self):
        p = Portfolio(initial_cash=100_000, tax_method=TaxMethod.LIFO)
        p.transaction_date = datetime(2026, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 100.0)
        p.transaction_date = datetime(2026, 2, 1, tzinfo=UTC)
        consumed = p.close_position("AAPL", 1, 150.0)
        assert consumed[0]["quantity"] == 1
        assert consumed[0]["purchase_price"] == 100.0

    def test_sell_all_removes_position(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2026, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 100.0)
        p.transaction_date = datetime(2026, 2, 1, tzinfo=UTC)
        p.close_position("AAPL", 100, 150.0)
        assert "AAPL" not in p.positions
        assert p.get_tax_lots("AAPL") == []

    def test_sell_all_lots_consumed(self):
        p = Portfolio(initial_cash=200_000, tax_method=TaxMethod.FIFO)
        base = datetime(2026, 1, 1, tzinfo=UTC)
        p.transaction_date = base
        p.open_position("AAPL", 50, 80.0)
        p.transaction_date = base + timedelta(days=1)
        p.open_position("AAPL", 50, 90.0)
        p.transaction_date = base + timedelta(days=10)
        p.close_position("AAPL", 100, 150.0)
        assert p.get_tax_lots("AAPL") == []

    def test_consume_lots_no_lots_raises(self):
        p = Portfolio(initial_cash=100_000)
        with pytest.raises(ValueError, match="No tax lots"):
            p._consume_lots("AAPL", 10, datetime.now(UTC))

    def test_multiple_round_trips(self):
        p = Portfolio(initial_cash=100_000, tax_method=TaxMethod.FIFO)
        base = datetime(2026, 1, 1, tzinfo=UTC)
        for i in range(5):
            p.transaction_date = base + timedelta(days=i * 20)
            p.open_position("AAPL", 10, 100.0 + i * 10)
            p.transaction_date = base + timedelta(days=i * 20 + 10)
            p.close_position("AAPL", 10, 120.0 + i * 10)
        assert "AAPL" not in p.positions
        assert len(p.trade_history) == 10
        assert p.realized_pnl > 0


class TestWashSaleIntegration:
    def test_wash_sale_adjusts_replacement_basis(self):
        p = Portfolio(initial_cash=200_000)
        base = datetime(2026, 1, 1, tzinfo=UTC)

        p.transaction_date = base
        p.open_position("AAPL", 100, 150.0)

        p.transaction_date = base + timedelta(days=30)
        p.close_position("AAPL", 100, 130.0)

        p.transaction_date = base + timedelta(days=35)
        p.open_position("AAPL", 100, 135.0)

        lots = p.get_tax_lots("AAPL")
        assert len(lots) == 1
        assert lots[0].purchase_price > 135.0

    def test_no_wash_sale_outside_window(self):
        p = Portfolio(initial_cash=200_000)
        base = datetime(2026, 1, 1, tzinfo=UTC)

        p.transaction_date = base
        p.open_position("AAPL", 100, 150.0)

        p.transaction_date = base + timedelta(days=10)
        p.close_position("AAPL", 100, 130.0)

        p.transaction_date = base + timedelta(days=50)
        p.open_position("AAPL", 100, 135.0)

        lots = p.get_tax_lots("AAPL")
        assert lots[0].purchase_price == 135.0

    def test_wash_sale_does_not_apply_to_gain(self):
        p = Portfolio(initial_cash=200_000)
        base = datetime(2026, 1, 1, tzinfo=UTC)

        p.transaction_date = base
        p.open_position("AAPL", 100, 100.0)

        p.transaction_date = base + timedelta(days=10)
        p.close_position("AAPL", 100, 150.0)

        p.transaction_date = base + timedelta(days=15)
        p.open_position("AAPL", 100, 145.0)

        lots = p.get_tax_lots("AAPL")
        assert lots[0].purchase_price == 145.0

    def test_wash_sale_only_matching_symbol(self):
        p = Portfolio(initial_cash=300_000)
        base = datetime(2026, 1, 1, tzinfo=UTC)

        p.transaction_date = base
        p.open_position("AAPL", 100, 150.0)

        p.transaction_date = base + timedelta(days=10)
        p.close_position("AAPL", 100, 130.0)

        p.transaction_date = base + timedelta(days=15)
        p.open_position("MSFT", 100, 300.0)

        aapl_lots = p.get_tax_lots("AAPL")
        assert len(aapl_lots) == 0

        msft_lots = p.get_tax_lots("MSFT")
        assert len(msft_lots) == 1
        assert msft_lots[0].purchase_price == 300.0


# ═══════════════════════════════════════════════════════════════════════
# 11. Signal edge cases
# ═══════════════════════════════════════════════════════════════════════


class TestSignalEdgeCases:
    def test_buy_signal(self):
        s = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        assert s.side == Side.BUY
        assert s.symbol == "AAPL"
        assert s.quantity == 10

    def test_sell_signal(self):
        s = Signal.sell(symbol="AAPL", strategy_id="test", quantity=10)
        assert s.side == Side.SELL

    def test_hold_signal(self):
        s = Signal.hold(symbol="AAPL", strategy_id="test")
        assert s.side == Side.HOLD

    def test_signal_with_max_cost_pct(self):
        s = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10, max_cost_pct=0.01)
        assert s.max_cost_pct == 0.01

    def test_signal_default_weight(self):
        s = Signal.buy(symbol="AAPL", strategy_id="test")
        assert s.weight == 1.0

    def test_signal_id_is_uuid(self):
        s = Signal.buy(symbol="AAPL", strategy_id="test")
        uuid.UUID(s.id)

    def test_signal_instrument_auto_populated(self):
        s = Signal.buy(symbol="AAPL", strategy_id="test")
        assert s.instrument is not None


# ═══════════════════════════════════════════════════════════════════════
# 12. FillResult
# ═══════════════════════════════════════════════════════════════════════


class TestFillResult:
    def test_success_defaults(self):
        fr = FillResult(success=True, price=100.0, quantity=10)
        assert fr.success is True
        assert fr.reason == ""

    def test_failure(self):
        fr = FillResult(success=False, reason="No liquidity")
        assert fr.success is False
        assert fr.price == 0.0
        assert fr.quantity == 0
        assert fr.reason == "No liquidity"
