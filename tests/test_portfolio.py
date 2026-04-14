"""
Tests for portfolio tracking and order management.
"""

import pytest
from core.portfolio import Portfolio, PortfolioSnapshot


class TestPortfolio:
    def test_initial_state(self):
        p = Portfolio(initial_cash=100_000)
        assert p.cash == 100_000
        assert p.total_value == 100_000
        assert len(p.positions) == 0
        assert p.total_return_pct == 0.0

    def test_open_position(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 100, 150.0, cost=5.0)
        assert p.cash == 100_000 - (100 * 150.0) - 5.0
        assert "AAPL" in p.positions
        assert p.positions["AAPL"].quantity == 100
        assert p.positions["AAPL"].avg_cost == 150.0

    def test_close_position(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 100, 150.0)
        p.close_position("AAPL", 100, 170.0, cost=5.0, tax=200.0)
        assert "AAPL" not in p.positions
        expected_pnl = (170.0 - 150.0) * 100 - 5.0 - 200.0
        assert abs(p.realized_pnl - expected_pnl) < 1e-6

    def test_insufficient_cash_raises(self):
        p = Portfolio(initial_cash=1000)
        with pytest.raises(ValueError, match="Insufficient cash"):
            p.open_position("AAPL", 100, 150.0)

    def test_sell_more_than_held_raises(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 10, 150.0)
        with pytest.raises(ValueError, match="Cannot sell"):
            p.close_position("AAPL", 20, 150.0)

    def test_partial_close(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 100, 150.0)
        p.close_position("AAPL", 50, 170.0)
        assert p.positions["AAPL"].quantity == 50

    def test_add_to_existing_position(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 50, 150.0)
        p.open_position("AAPL", 50, 160.0)
        assert p.positions["AAPL"].quantity == 100
        assert p.positions["AAPL"].avg_cost == 155.0  # Weighted average

    def test_total_value_with_price_update(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 100, 150.0)
        p.update_prices({"AAPL": 170.0})
        expected = p.cash + (100 * 170.0)
        assert abs(p.total_value - expected) < 1e-6

    def test_trade_history_recorded(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 100, 150.0, cost=5.0)
        p.close_position("AAPL", 100, 170.0, cost=5.0)
        assert len(p.trade_history) == 2
        assert p.trade_history[0]["side"] == "buy"
        assert p.trade_history[1]["side"] == "sell"


class TestPortfolioSnapshot:
    def test_snapshot_is_immutable_view(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 100, 150.0)
        p.update_prices({"AAPL": 170.0})
        snap = p.snapshot()
        assert isinstance(snap, PortfolioSnapshot)
        assert snap.cash == p.cash
        assert "AAPL" in snap.positions
        assert snap.positions["AAPL"]["quantity"] == 100

    def test_allocation_weight(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 100, 150.0)
        p.update_prices({"AAPL": 150.0})
        snap = p.snapshot()
        weight = snap.allocation_weight("AAPL")
        expected = (100 * 150.0) / p.total_value
        assert abs(weight - expected) < 1e-6

    def test_summary_string(self):
        p = Portfolio(initial_cash=50_000)
        snap = p.snapshot()
        summary = snap.summary()
        assert "50,000" in summary
