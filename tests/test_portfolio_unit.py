"""
Comprehensive unit tests for portfolio.py — Position, PortfolioSnapshot,
Portfolio properties, and edge cases not covered by test_tax_lots.py.
"""

from datetime import UTC, datetime, timedelta

import pytest

from engine.core.cost_model import TaxMethod
from engine.core.portfolio import (
    Portfolio,
    PortfolioSnapshot,
    PortfolioState,
    Position,
    SellRecord,
    TradeRecord,
)


class TestPositionProperties:
    def test_is_zero_with_zero_quantity(self):
        pos = Position(symbol="AAPL", quantity=0, avg_cost=100.0)
        assert pos.is_zero is True

    def test_is_zero_with_nonzero_quantity(self):
        pos = Position(symbol="AAPL", quantity=10, avg_cost=100.0)
        assert pos.is_zero is False

    def test_market_value_uses_current_price(self):
        pos = Position(symbol="AAPL", quantity=100, avg_cost=100.0, current_price=150.0)
        assert pos.market_value == 15_000.0

    def test_market_value_falls_back_to_avg_cost(self):
        pos = Position(symbol="AAPL", quantity=100, avg_cost=100.0, current_price=0.0)
        assert pos.market_value == 10_000.0

    def test_market_value_zero_quantity(self):
        pos = Position(symbol="AAPL", quantity=0, avg_cost=100.0, current_price=150.0)
        assert pos.market_value == 0.0

    def test_default_values(self):
        pos = Position(symbol="AAPL")
        assert pos.quantity == 0
        assert pos.avg_cost == 0.0
        assert pos.current_price == 0.0
        assert pos.is_zero is True


class TestTradeRecord:
    def test_default_cost_and_tax(self):
        tr = TradeRecord(
            timestamp=datetime.now(UTC),
            side="buy",
            symbol="AAPL",
            quantity=10,
            price=100.0,
        )
        assert tr.cost == 0.0
        assert tr.tax == 0.0
        assert tr.lot_ids == []

    def test_with_cost_and_tax(self):
        tr = TradeRecord(
            timestamp=datetime.now(UTC),
            side="sell",
            symbol="AAPL",
            quantity=10,
            price=100.0,
            cost=5.0,
            tax=3.0,
            lot_ids=["lot-1"],
        )
        assert tr.cost == 5.0
        assert tr.tax == 3.0
        assert tr.lot_ids == ["lot-1"]


class TestSellRecord:
    def test_fields(self):
        sr = SellRecord(
            symbol="AAPL",
            sell_date=datetime.now(UTC),
            quantity=100,
            sell_price=150.0,
            cost_basis=10_000.0,
            gain=5_000.0,
        )
        assert sr.symbol == "AAPL"
        assert sr.quantity == 100
        assert sr.gain == 5_000.0


class TestPortfolioProperties:
    def test_cash_property(self):
        p = Portfolio(initial_cash=50_000.0)
        assert p.cash == 50_000.0

    def test_total_value_empty_portfolio(self):
        p = Portfolio(initial_cash=100_000.0)
        assert p.total_value == 100_000.0

    def test_total_value_with_positions(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        p.update_prices({"AAPL": 150.0})
        assert p.total_value > p.cash

    def test_total_return_pct_positive(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 100, 100.0)
        p.update_prices({"AAPL": 110.0})
        ret = p.total_return_pct
        assert ret > 0

    def test_total_return_pct_zero_initial(self):
        p = Portfolio(initial_cash=0)
        assert p.total_return_pct == 0.0

    def test_realized_pnl_starts_at_zero(self):
        p = Portfolio(initial_cash=100_000.0)
        assert p.realized_pnl == 0.0


class TestPortfolioUpdatePrices:
    def test_update_sets_current_price(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        p.update_prices({"AAPL": 150.0})
        assert p.positions["AAPL"].current_price == 150.0

    def test_update_ignores_unknown_symbols(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        p.update_prices({"MSFT": 200.0})
        assert p.positions["AAPL"].current_price == 0.0

    def test_update_multiple_symbols(self):
        p = Portfolio(initial_cash=200_000.0)
        p.open_position("AAPL", 10, 100.0)
        p.open_position("MSFT", 20, 200.0)
        p.update_prices({"AAPL": 110.0, "MSFT": 210.0})
        assert p.positions["AAPL"].current_price == 110.0
        assert p.positions["MSFT"].current_price == 210.0

    def test_update_empty_dict_no_error(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        p.update_prices({})
        assert p.positions["AAPL"].current_price == 0.0


class TestPortfolioSnapshot:
    def test_snapshot_contains_expected_fields(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        p.update_prices({"AAPL": 150.0})
        snap = p.snapshot()
        assert isinstance(snap, PortfolioSnapshot)
        assert snap.cash < 100_000.0
        assert "AAPL" in snap.positions
        assert snap.total_value > 0
        assert isinstance(snap.total_return_pct, float)
        assert isinstance(snap.realized_pnl, float)

    def test_snapshot_positions_detail(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        snap = p.snapshot()
        aapl = snap.positions["AAPL"]
        assert aapl["quantity"] == 10
        assert aapl["avg_cost"] == 100.0

    def test_allocation_weight_with_position(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 100, 100.0)
        p.update_prices({"AAPL": 100.0})
        snap = p.snapshot()
        weight = snap.allocation_weight("AAPL")
        assert weight > 0

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
            positions={"AAPL": {"quantity": 10, "avg_cost": 100.0, "current_price": 100.0}},
            total_value=0.0,
            total_return_pct=0.0,
            realized_pnl=0.0,
        )
        assert snap.allocation_weight("AAPL") == 0.0

    def test_summary_format(self):
        snap = PortfolioSnapshot(
            cash=50_000.0,
            positions={},
            total_value=50_000.0,
            total_return_pct=-50.0,
            realized_pnl=0.0,
        )
        s = snap.summary()
        assert "Cash:" in s
        assert "Value:" in s
        assert "Return:" in s

    def test_position_uses_current_price_or_avg_cost_for_allocation(self):
        snap = PortfolioSnapshot(
            cash=50_000.0,
            positions={
                "AAPL": {"quantity": 100, "avg_cost": 100.0, "current_price": 0},
            },
            total_value=60_000.0,
            total_return_pct=0.0,
            realized_pnl=0.0,
        )
        weight = snap.allocation_weight("AAPL")
        assert weight == pytest.approx((100 * 100.0 / 60_000.0) * 100)


class TestPortfolioOpenPosition:
    def test_insufficient_cash_raises(self):
        p = Portfolio(initial_cash=100.0)
        with pytest.raises(ValueError, match="Insufficient cash"):
            p.open_position("AAPL", 10, 200.0)

    def test_open_position_deducts_cash(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        assert p.cash == 100_000.0 - (10 * 100.0)

    def test_open_position_with_cost(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0, cost=50.0)
        assert p.cash == 100_000.0 - (10 * 100.0) - 50.0

    def test_open_position_adds_to_existing(self):
        p = Portfolio(initial_cash=200_000.0)
        p.open_position("AAPL", 10, 100.0)
        p.open_position("AAPL", 10, 200.0)
        assert p.positions["AAPL"].quantity == 20
        expected_avg = (10 * 100.0 + 10 * 200.0) / 20
        assert p.positions["AAPL"].avg_cost == pytest.approx(expected_avg)

    def test_open_position_creates_tax_lot(self):
        p = Portfolio(initial_cash=100_000.0)
        lot_id = p.open_position("AAPL", 10, 100.0)
        lots = p.get_tax_lots("AAPL")
        assert len(lots) == 1
        assert lots[0].quantity == 10
        assert lots[0].purchase_price == 100.0

    def test_open_position_records_trade(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        assert len(p.trade_history) == 1
        assert p.trade_history[0].side == "buy"
        assert p.trade_history[0].symbol == "AAPL"

    def test_open_position_returns_uuid(self):
        import uuid
        p = Portfolio(initial_cash=100_000.0)
        result = p.open_position("AAPL", 10, 100.0)
        assert isinstance(result, uuid.UUID)


class TestPortfolioClosePosition:
    def test_close_nonexistent_raises(self):
        p = Portfolio(initial_cash=100_000.0)
        with pytest.raises(ValueError, match="No position for"):
            p.close_position("AAPL", 10, 100.0)

    def test_close_more_than_held_raises(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        with pytest.raises(ValueError, match="Cannot sell"):
            p.close_position("AAPL", 20, 100.0)

    def test_close_no_tax_lots_raises(self):
        p = Portfolio(initial_cash=100_000.0)
        p.positions["AAPL"] = Position(symbol="AAPL", quantity=10, avg_cost=100.0)
        with pytest.raises(ValueError, match="No tax lots found"):
            p.close_position("AAPL", 10, 100.0)

    def test_close_full_position_removes_position(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        p.close_position("AAPL", 10, 150.0)
        assert "AAPL" not in p.positions

    def test_close_updates_realized_pnl(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        p.close_position("AAPL", 10, 150.0)
        assert p.realized_pnl > 0

    def test_close_returns_consumed_lots(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        consumed = p.close_position("AAPL", 10, 150.0)
        assert len(consumed) == 1
        assert consumed[0]["quantity"] == 10

    def test_close_adds_to_cash(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        cash_after_buy = p.cash
        p.close_position("AAPL", 10, 150.0)
        assert p.cash > cash_after_buy

    def test_close_with_cost_and_tax(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        cash_after_buy = p.cash
        p.close_position("AAPL", 10, 150.0, cost=10.0, tax=5.0)
        expected = cash_after_buy + 10 * 150.0 - 10.0 - 5.0
        assert p.cash == pytest.approx(expected)

    def test_close_records_sell_trade(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        p.close_position("AAPL", 10, 150.0)
        sells = [t for t in p.trade_history if t.side == "sell"]
        assert len(sells) == 1
        assert sells[0].symbol == "AAPL"
        assert sells[0].quantity == 10


class TestPortfolioSetTaxMethod:
    def test_set_tax_method_to_lifo(self):
        p = Portfolio(initial_cash=100_000.0, tax_method=TaxMethod.FIFO)
        p.set_tax_method(TaxMethod.LIFO)
        assert p.tax_method == TaxMethod.LIFO

    def test_set_tax_method_to_specific_lot(self):
        p = Portfolio(initial_cash=100_000.0)
        p.set_tax_method(TaxMethod.SPECIFIC_LOT)
        assert p.tax_method == TaxMethod.SPECIFIC_LOT


class TestPortfolioGetTaxLots:
    def test_empty_for_unknown_symbol(self):
        p = Portfolio(initial_cash=100_000.0)
        assert p.get_tax_lots("AAPL") == []

    def test_returns_lots_after_open(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        p.open_position("AAPL", 5, 110.0)
        lots = p.get_tax_lots("AAPL")
        assert len(lots) == 2


class TestPortfolioTransactionDate:
    def test_transaction_date_used_for_lots(self):
        p = Portfolio(initial_cash=100_000.0)
        p.transaction_date = datetime(2025, 6, 15, tzinfo=UTC)
        p.open_position("AAPL", 10, 100.0)
        lots = p.get_tax_lots("AAPL")
        assert lots[0].purchase_date.year == 2025
        assert lots[0].purchase_date.month == 6
        assert lots[0].purchase_date.day == 15


class TestPortfolioStateAlias:
    def test_portfolio_state_is_snapshot(self):
        assert PortfolioState is PortfolioSnapshot
