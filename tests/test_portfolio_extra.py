"""Additional coverage for engine.core.portfolio uncovered lines.

Targets:
  - Position.is_zero (line 21)
  - Portfolio.total_return_pct with zero initial_cash (line 88)
  - _consume_lots ValueError for no tax lots (line 99)
  - _consume_lots ValueError for insufficient lots (line 137)
  - open_position ValueError for insufficient cash (line 157)
  - open_position wash sale cost basis adjustment (line 166)
  - close_position ValueError for no position (line 231)
  - close_position ValueError for selling more than held (line 235)
  - Portfolio.set_tax_method (line 309)
  - PortfolioSnapshot.allocation_weight edge cases (lines 323-328)
  - PortfolioSnapshot.summary (line 331)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from engine.core.cost_model import TaxMethod
from engine.core.portfolio import Portfolio, PortfolioSnapshot, Position


class TestPosition:
    def test_is_zero_with_quantity(self):
        p = Position(symbol="AAPL", quantity=100, avg_cost=150.0)
        assert p.is_zero is False

    def test_is_zero_with_zero_quantity(self):
        p = Position(symbol="AAPL", quantity=0, avg_cost=150.0)
        assert p.is_zero is True

    def test_market_value_with_current_price(self):
        p = Position(symbol="AAPL", quantity=10, avg_cost=100.0, current_price=150.0)
        assert p.market_value == 1500.0

    def test_market_value_with_zero_current_price(self):
        p = Position(symbol="AAPL", quantity=10, avg_cost=100.0, current_price=0.0)
        assert p.market_value == 1000.0


class TestPortfolioErrors:
    def test_consume_lots_no_lots_raises(self):
        p = Portfolio(initial_cash=100_000.0)
        with pytest.raises(ValueError, match="No tax lots found"):
            p._consume_lots("AAPL", 10, datetime.now(UTC))

    def test_consume_lots_insufficient_raises(self):
        p = Portfolio(initial_cash=100_000.0)
        p.transaction_date = datetime(2024, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 5, 100.0)
        p.transaction_date = datetime(2024, 6, 1, tzinfo=UTC)
        with pytest.raises(ValueError, match="Tax lots insufficient"):
            p.close_position("AAPL", 10, 150.0)

    def test_open_position_insufficient_cash_raises(self):
        p = Portfolio(initial_cash=100.0)
        p.transaction_date = datetime(2024, 1, 1, tzinfo=UTC)
        with pytest.raises(ValueError, match="Insufficient cash"):
            p.open_position("AAPL", 10, 150.0)

    def test_close_position_no_position_raises(self):
        p = Portfolio(initial_cash=100_000.0)
        p.transaction_date = datetime(2024, 1, 1, tzinfo=UTC)
        with pytest.raises(ValueError, match="No position for"):
            p.close_position("AAPL", 10, 150.0)

    def test_close_position_sell_more_than_held_raises(self):
        p = Portfolio(initial_cash=100_000.0)
        p.transaction_date = datetime(2024, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 5, 100.0)
        p.transaction_date = datetime(2024, 6, 1, tzinfo=UTC)
        with pytest.raises(ValueError, match="Cannot sell"):
            p.close_position("AAPL", 10, 150.0)


class TestPortfolioWashSaleAdjustment:
    def test_wash_sale_adjusts_cost_basis_on_buyback(self):
        p = Portfolio(initial_cash=100_000.0)
        p.transaction_date = datetime(2024, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 10, 100.0)

        p.transaction_date = datetime(2024, 2, 1, tzinfo=UTC)
        p.close_position("AAPL", 10, 80.0)
        assert p.realized_pnl < 0

        p.transaction_date = datetime(2024, 2, 15, tzinfo=UTC)
        lot_id = p.open_position("AAPL", 10, 90.0)

        lots = p.get_tax_lots("AAPL")
        assert len(lots) == 1
        assert lots[0].purchase_price > 90.0


class TestPortfolioSnapshot:
    def test_allocation_weight_no_position(self):
        snap = PortfolioSnapshot(
            cash=100_000.0,
            positions={},
            total_value=100_000.0,
            total_return_pct=0.0,
            realized_pnl=0.0,
        )
        assert snap.allocation_weight("AAPL") == 0.0

    def test_allocation_weight_zero_total(self):
        snap = PortfolioSnapshot(
            cash=0.0,
            positions={"AAPL": {"quantity": 10, "avg_cost": 100.0, "current_price": 0.0}},
            total_value=0.0,
            total_return_pct=0.0,
            realized_pnl=0.0,
        )
        assert snap.allocation_weight("AAPL") == 0.0

    def test_allocation_weight_with_position(self):
        snap = PortfolioSnapshot(
            cash=50_000.0,
            positions={
                "AAPL": {"quantity": 100, "avg_cost": 100.0, "current_price": 150.0},
            },
            total_value=65_000.0,
            total_return_pct=0.0,
            realized_pnl=0.0,
        )
        weight = snap.allocation_weight("AAPL")
        assert abs(weight - (15000.0 / 65000.0 * 100)) < 1e-6

    def test_allocation_weight_uses_avg_cost_when_no_current_price(self):
        snap = PortfolioSnapshot(
            cash=50_000.0,
            positions={
                "AAPL": {"quantity": 100, "avg_cost": 100.0, "current_price": 0.0},
            },
            total_value=60_000.0,
            total_return_pct=0.0,
            realized_pnl=0.0,
        )
        weight = snap.allocation_weight("AAPL")
        assert abs(weight - (10000.0 / 60000.0 * 100)) < 1e-6

    def test_summary_string(self):
        snap = PortfolioSnapshot(
            cash=100_000.0,
            positions={},
            total_value=100_000.0,
            total_return_pct=0.0,
            realized_pnl=0.0,
        )
        s = snap.summary()
        assert "Cash:" in s
        assert "Value:" in s
        assert "Return:" in s


class TestPortfolioTaxMethod:
    def test_set_tax_method(self):
        p = Portfolio(initial_cash=100_000.0, tax_method=TaxMethod.FIFO)
        assert p.tax_method == TaxMethod.FIFO
        p.set_tax_method(TaxMethod.LIFO)
        assert p.tax_method == TaxMethod.LIFO

    def test_default_tax_method_is_fifo(self):
        p = Portfolio(initial_cash=100_000.0)
        assert p.tax_method == TaxMethod.FIFO


class TestPortfolioZeroInitialCash:
    def test_total_return_pct_zero_initial(self):
        p = Portfolio(initial_cash=0.0)
        assert p.total_return_pct == 0.0


class TestPortfolioUpdatePrices:
    def test_update_prices_sets_current_price(self):
        p = Portfolio(initial_cash=100_000.0)
        p.transaction_date = datetime(2024, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 10, 100.0)
        p.update_prices({"AAPL": 150.0})
        assert p.positions["AAPL"].current_price == 150.0

    def test_update_prices_ignores_unknown_symbols(self):
        p = Portfolio(initial_cash=100_000.0)
        p.update_prices({"MSFT": 200.0})
        assert "MSFT" not in p.positions


class TestPortfolioGetTaxLots:
    def test_get_tax_lots_empty(self):
        p = Portfolio(initial_cash=100_000.0)
        assert p.get_tax_lots("AAPL") == []

    def test_get_tax_lots_returns_lots(self):
        p = Portfolio(initial_cash=100_000.0)
        p.transaction_date = datetime(2024, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 10, 100.0)
        lots = p.get_tax_lots("AAPL")
        assert len(lots) == 1
        assert lots[0].symbol == "AAPL"
        assert lots[0].quantity == 10


class TestPortfolioId:
    def test_portfolio_id_default_none(self):
        p = Portfolio(initial_cash=100_000.0)
        assert p.portfolio_id is None

    def test_portfolio_id_set(self):
        import uuid

        pid = uuid.uuid4()
        p = Portfolio(initial_cash=100_000.0, portfolio_id=pid)
        assert p.portfolio_id == pid
