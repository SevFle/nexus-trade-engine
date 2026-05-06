"""
Comprehensive tests for Portfolio — position tracking, P&L calculation,
tax lot consumption, error paths, and snapshot methods.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from engine.core.cost_model import DefaultCostModel, TaxMethod
from engine.core.portfolio import Portfolio, PortfolioSnapshot, Position


class TestPositionIsZero:
    def test_zero_quantity_is_zero(self):
        pos = Position(symbol="AAPL", quantity=0, avg_cost=100.0)
        assert pos.is_zero is True

    def test_nonzero_quantity_not_zero(self):
        pos = Position(symbol="AAPL", quantity=10, avg_cost=100.0)
        assert pos.is_zero is False


class TestTotalReturnPctZeroCapital:
    def test_zero_initial_cash_returns_zero(self):
        p = Portfolio(initial_cash=0.0)
        assert p.total_return_pct == 0.0


class TestConsumeLotsErrors:
    def test_no_tax_lots_raises(self):
        p = Portfolio(initial_cash=100_000)
        p.positions["AAPL"] = Position(symbol="AAPL", quantity=10, avg_cost=100.0)
        with pytest.raises(ValueError, match="No tax lots found"):
            p.close_position("AAPL", 10, 150.0)

    def test_insufficient_tax_lots_raises(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 10, 100.0)
        p.positions["AAPL"] = Position(symbol="AAPL", quantity=20, avg_cost=100.0)
        with pytest.raises(ValueError, match="Tax lots insufficient"):
            p.close_position("AAPL", 20, 150.0)


class TestOpenPositionInsufficientCash:
    def test_insufficient_cash_raises(self):
        p = Portfolio(initial_cash=100.0)
        with pytest.raises(ValueError, match="Insufficient cash"):
            p.open_position("AAPL", 10, 150.0)


class TestClosePositionErrors:
    def test_no_position_raises(self):
        p = Portfolio(initial_cash=100_000)
        with pytest.raises(ValueError, match="No position for"):
            p.close_position("AAPL", 10, 150.0)

    def test_sell_more_than_held_raises(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 5, 100.0)
        with pytest.raises(ValueError, match="Cannot sell"):
            p.close_position("AAPL", 10, 150.0)


class TestWashSaleGainSkip:
    def test_gain_sell_not_adjusted(self):
        p = Portfolio(initial_cash=200_000)

        p.transaction_date = datetime(2026, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 100.0)

        p.transaction_date = datetime(2026, 3, 1, tzinfo=UTC)
        p.close_position("AAPL", 100, 150.0)

        p.transaction_date = datetime(2026, 3, 10, tzinfo=UTC)
        p.open_position("AAPL", 100, 140.0)

        lots = p.get_tax_lots("AAPL")
        assert len(lots) == 1
        assert lots[0].purchase_price == 140.0


class TestSetTaxMethod:
    def test_set_lifo(self):
        p = Portfolio(initial_cash=100_000, tax_method=TaxMethod.FIFO)
        assert p.tax_method == TaxMethod.FIFO
        p.set_tax_method(TaxMethod.LIFO)
        assert p.tax_method == TaxMethod.LIFO

    def test_set_specific_lot(self):
        p = Portfolio(initial_cash=100_000)
        p.set_tax_method(TaxMethod.SPECIFIC_LOT)
        assert p.tax_method == TaxMethod.SPECIFIC_LOT


class TestPortfolioSnapshotAllocationWeight:
    def test_allocation_weight_with_position(self):
        snap = PortfolioSnapshot(
            cash=50_000.0,
            positions={
                "AAPL": {"quantity": 100, "avg_cost": 100.0, "current_price": 150.0},
            },
            total_value=65_000.0,
            total_return_pct=30.0,
            realized_pnl=0.0,
        )
        weight = snap.allocation_weight("AAPL")
        expected = (100 * 150.0) / 65_000.0 * 100
        assert abs(weight - expected) < 1e-6

    def test_allocation_weight_missing_symbol(self):
        snap = PortfolioSnapshot(
            cash=100_000.0,
            positions={},
            total_value=100_000.0,
            total_return_pct=0.0,
            realized_pnl=0.0,
        )
        assert snap.allocation_weight("AAPL") == 0.0

    def test_allocation_weight_zero_total_value(self):
        snap = PortfolioSnapshot(
            cash=0.0,
            positions={
                "AAPL": {"quantity": 100, "avg_cost": 100.0, "current_price": 0.0},
            },
            total_value=0.0,
            total_return_pct=0.0,
            realized_pnl=0.0,
        )
        assert snap.allocation_weight("AAPL") == 0.0

    def test_allocation_weight_uses_avg_cost_when_no_current_price(self):
        snap = PortfolioSnapshot(
            cash=0.0,
            positions={
                "AAPL": {"quantity": 100, "avg_cost": 100.0, "current_price": 0.0},
            },
            total_value=10_000.0,
            total_return_pct=0.0,
            realized_pnl=0.0,
        )
        weight = snap.allocation_weight("AAPL")
        assert weight == 100.0


class TestPortfolioSnapshotSummary:
    def test_summary_format(self):
        snap = PortfolioSnapshot(
            cash=100_000.0,
            positions={},
            total_value=120_000.0,
            total_return_pct=20.0,
            realized_pnl=500.0,
        )
        s = snap.summary()
        assert "Cash: $100,000" in s
        assert "Value: $120,000" in s
        assert "20.00%" in s


class TestPortfolioPositionMarketValue:
    def test_market_value_uses_current_price(self):
        pos = Position(symbol="AAPL", quantity=50, avg_cost=100.0, current_price=120.0)
        assert pos.market_value == 50 * 120.0

    def test_market_value_falls_back_to_avg_cost(self):
        pos = Position(symbol="AAPL", quantity=50, avg_cost=100.0, current_price=0.0)
        assert pos.market_value == 50 * 100.0


class TestPortfolioUpdatePrices:
    def test_update_prices_sets_current_price(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 150.0)
        p.update_prices({"AAPL": 160.0})
        assert p.positions["AAPL"].current_price == 160.0

    def test_update_prices_ignores_unknown_symbols(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 150.0)
        p.update_prices({"MSFT": 200.0})
        assert "MSFT" not in p.positions


class TestPortfolioSnapshot:
    def test_snapshot_reflects_positions(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 150.0)
        p.update_prices({"AAPL": 160.0})

        snap = p.snapshot()
        assert snap.cash < 100_000
        assert "AAPL" in snap.positions
        assert snap.positions["AAPL"]["quantity"] == 100
        assert snap.positions["AAPL"]["current_price"] == 160.0


class TestPortfolioFullCycle:
    def test_buy_sell_full_cycle_pnl(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 100.0)
        p.transaction_date = datetime(2025, 6, 1, tzinfo=UTC)
        lots = p.close_position("AAPL", 100, 150.0)

        assert len(lots) == 1
        assert p.realized_pnl > 0
        assert "AAPL" not in p.positions
        assert p.cash == 100_000 - 100 * 100.0 + 100 * 150.0

    def test_buy_sell_partial_then_remain(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 100.0)
        p.transaction_date = datetime(2025, 6, 1, tzinfo=UTC)
        p.close_position("AAPL", 50, 150.0)

        assert p.positions["AAPL"].quantity == 50
        assert p.realized_pnl > 0


class TestPortfolioTradeHistory:
    def test_trade_history_records_buy(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 10, 100.0)
        assert len(p.trade_history) == 1
        assert p.trade_history[0].side == "buy"

    def test_trade_history_records_sell(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 10, 100.0)
        p.transaction_date = datetime(2025, 6, 1, tzinfo=UTC)
        p.close_position("AAPL", 10, 150.0)
        assert len(p.trade_history) == 2
        assert p.trade_history[1].side == "sell"

    def test_trade_history_sell_has_lot_ids(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 10, 100.0)
        p.transaction_date = datetime(2025, 6, 1, tzinfo=UTC)
        p.close_position("AAPL", 10, 150.0)
        sell_record = p.trade_history[1]
        assert len(sell_record.lot_ids) == 1
