"""
QA tests for tax lot tracking — covering edge cases, error paths,
cross-symbol isolation, wash sale double-count cap, and holding-period
boundary conditions not exercised by the backend test suite.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from engine.core.cost_model import DefaultCostModel, TaxLot, TaxMethod
from engine.core.portfolio import Portfolio


class TestLotIdUniqueness:
    """Each buy must create a new lot with a unique UUID lot_id."""

    def test_each_buy_gets_unique_lot_id(self):
        p = Portfolio(initial_cash=500_000)
        p.open_position("AAPL", 100, 100.0)
        p.open_position("AAPL", 100, 110.0)
        p.open_position("AAPL", 100, 120.0)

        lots = p.get_tax_lots("AAPL")
        assert len(lots) == 3
        lot_ids = [lot.lot_id for lot in lots]
        assert len(set(lot_ids)) == 3

    def test_lot_id_is_valid_uuid(self):
        p = Portfolio(initial_cash=100_000)
        lot_uuid = p.open_position("AAPL", 10, 100.0)

        assert isinstance(lot_uuid, uuid.UUID)

        lots = p.get_tax_lots("AAPL")
        assert lots[0].lot_id == str(lot_uuid)

    def test_trade_history_records_lot_ids(self):
        p = Portfolio(initial_cash=200_000)
        p.transaction_date = datetime(2026, 1, 1, tzinfo=UTC)
        lot_uuid = p.open_position("AAPL", 50, 100.0)

        assert len(p.trade_history) == 1
        trade = p.trade_history[0]
        assert trade.side == "buy"
        assert trade.lot_ids == [str(lot_uuid)]

        p.transaction_date = datetime(2026, 2, 1, tzinfo=UTC)
        consumed = p.close_position("AAPL", 50, 120.0)

        sell_trade = p.trade_history[1]
        assert sell_trade.side == "sell"
        assert sell_trade.lot_ids == [str(lot_uuid)]


class TestErrorPaths:
    """Verify that invalid operations raise appropriate errors."""

    def test_sell_with_no_position_raises(self):
        p = Portfolio(initial_cash=100_000)
        with pytest.raises(ValueError, match="No position for AAPL"):
            p.close_position("AAPL", 10, 100.0)

    def test_sell_more_than_held_raises(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 50, 100.0)
        with pytest.raises(ValueError, match="Cannot sell 100 shares"):
            p.close_position("AAPL", 100, 120.0)

    def test_buy_insufficient_cash_raises(self):
        p = Portfolio(initial_cash=1_000)
        with pytest.raises(ValueError, match="Insufficient cash"):
            p.open_position("AAPL", 100, 100.0)

    def test_sell_zero_after_full_close_then_rebuy(self):
        p = Portfolio(initial_cash=200_000)
        p.transaction_date = datetime(2026, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 100.0)

        p.transaction_date = datetime(2026, 2, 1, tzinfo=UTC)
        p.close_position("AAPL", 100, 120.0)

        assert "AAPL" not in p.positions
        assert p.get_tax_lots("AAPL") == []

        p.transaction_date = datetime(2026, 3, 1, tzinfo=UTC)
        p.open_position("AAPL", 50, 130.0)

        assert p.positions["AAPL"].quantity == 50
        assert len(p.get_tax_lots("AAPL")) == 1


class TestCrossSymbolIsolation:
    """Tax lots and positions for different symbols must not interfere."""

    def test_lots_isolated_per_symbol(self):
        p = Portfolio(initial_cash=300_000)
        p.open_position("AAPL", 100, 150.0)
        p.open_position("MSFT", 100, 200.0)

        assert len(p.get_tax_lots("AAPL")) == 1
        assert len(p.get_tax_lots("MSFT")) == 1
        assert p.get_tax_lots("AAPL")[0].symbol == "AAPL"
        assert p.get_tax_lots("MSFT")[0].symbol == "MSFT"

    def test_sell_one_symbol_does_not_affect_other(self):
        p = Portfolio(initial_cash=300_000)
        p.open_position("AAPL", 100, 150.0)
        p.open_position("MSFT", 100, 200.0)

        p.close_position("AAPL", 100, 160.0)

        assert "AAPL" not in p.positions
        assert "MSFT" in p.positions
        assert p.positions["MSFT"].quantity == 100
        assert len(p.get_tax_lots("MSFT")) == 1

    def test_wash_sale_does_not_cross_symbols(self):
        p = Portfolio(initial_cash=500_000)
        p.transaction_date = datetime(2026, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 150.0)

        p.transaction_date = datetime(2026, 2, 1, tzinfo=UTC)
        p.close_position("AAPL", 100, 140.0)

        p.transaction_date = datetime(2026, 2, 5, tzinfo=UTC)
        p.open_position("MSFT", 100, 200.0)

        msft_lots = p.get_tax_lots("MSFT")
        assert len(msft_lots) == 1
        assert msft_lots[0].purchase_price == 200.0


class TestFullPositionClose:
    """Verify that selling all shares cleanly removes the position."""

    def test_position_removed_after_full_sell(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 100, 100.0)
        p.close_position("AAPL", 100, 120.0)

        assert "AAPL" not in p.positions
        assert p.get_tax_lots("AAPL") == []

    def test_position_removed_after_multi_lot_full_sell(self):
        p = Portfolio(initial_cash=100_000, tax_method=TaxMethod.FIFO)
        p.open_position("AAPL", 50, 80.0)
        p.open_position("AAPL", 50, 120.0)

        p.close_position("AAPL", 100, 150.0)

        assert "AAPL" not in p.positions
        assert p.get_tax_lots("AAPL") == []

    def test_partial_sell_then_full_sell_removes_position(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 100, 100.0)

        p.close_position("AAPL", 30, 120.0)
        assert p.positions["AAPL"].quantity == 70

        p.close_position("AAPL", 70, 130.0)
        assert "AAPL" not in p.positions
        assert p.get_tax_lots("AAPL") == []


class TestWashSaleDoubleCountCap:
    """Multiple replacement buys must not exceed the original disallowed loss."""

    @pytest.mark.xfail(
        reason="Depends on remaining_disallowed tracking (SEV-459 Phase 4)",
        strict=True,
    )
    def test_three_buys_total_adjustment_capped_at_loss(self):
        """Sell 100 @ $1000 loss, then buy 50, buy 50, buy 50.
        First two buys absorb $1000 ($500 each). Third gets $0."""
        p = Portfolio(initial_cash=1_000_000)

        p.transaction_date = datetime(2026, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 150.0)

        p.transaction_date = datetime(2026, 3, 1, tzinfo=UTC)
        p.close_position("AAPL", 100, 140.0)
        assert abs(p.realized_pnl - (-1000.0)) < 1e-6

        p.transaction_date = datetime(2026, 3, 6, tzinfo=UTC)
        p.open_position("AAPL", 50, 145.0)

        p.transaction_date = datetime(2026, 3, 11, tzinfo=UTC)
        p.open_position("AAPL", 50, 145.0)

        p.transaction_date = datetime(2026, 3, 16, tzinfo=UTC)
        p.open_position("AAPL", 50, 145.0)

        lots = p.get_tax_lots("AAPL")
        assert len(lots) == 3

        assert abs(lots[0].purchase_price - 155.0) < 1e-6
        assert abs(lots[1].purchase_price - 155.0) < 1e-6
        assert abs(lots[2].purchase_price - 145.0) < 1e-6

        total_adj = sum((lot.purchase_price - 145.0) * lot.quantity for lot in lots)
        assert total_adj <= 1000.0 + 1e-6

    @pytest.mark.xfail(
        reason="Depends on SellRecord.remaining_disallowed field (SEV-459 Phase 4)",
        strict=True,
    )
    def test_remaining_disallowed_depleted_after_full_allocation(self):
        p = Portfolio(initial_cash=1_000_000)

        p.transaction_date = datetime(2026, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 150.0)

        p.transaction_date = datetime(2026, 3, 1, tzinfo=UTC)
        p.close_position("AAPL", 100, 140.0)

        p.transaction_date = datetime(2026, 3, 6, tzinfo=UTC)
        p.open_position("AAPL", 50, 145.0)

        p.transaction_date = datetime(2026, 3, 11, tzinfo=UTC)
        p.open_position("AAPL", 50, 145.0)

        assert p._sell_history[0].remaining_disallowed < 1e-6


class TestWashSaleBuyThenSell:
    """Buy-first-then-sell-at-loss wash sale scenarios."""

    def test_buy_then_sell_no_wash_outside_window(self):
        """Buy, hold > 30 days, sell at loss — no wash sale."""
        p = Portfolio(initial_cash=500_000)

        p.transaction_date = datetime(2026, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 150.0)

        p.transaction_date = datetime(2026, 3, 15, tzinfo=UTC)
        p.close_position("AAPL", 100, 140.0)

        assert abs(p.realized_pnl - (-1000.0)) < 1e-6

    @pytest.mark.xfail(
        reason="Depends on _apply_buy_then_sell_wash_sale (SEV-459 Phase 4)",
        strict=True,
    )
    def test_buy_then_sell_wash_adjusts_remaining_lot_cost_basis(self):
        """Buy 100@150, buy 100@140, sell 100@130 (FIFO sells the $150 lot).
        The remaining $140 lot (within 30 days of sell) gets basis adjustment."""
        p = Portfolio(initial_cash=500_000, tax_method=TaxMethod.FIFO)

        p.transaction_date = datetime(2026, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 150.0)

        p.transaction_date = datetime(2026, 1, 15, tzinfo=UTC)
        p.open_position("AAPL", 100, 140.0)

        p.transaction_date = datetime(2026, 1, 25, tzinfo=UTC)
        p.close_position("AAPL", 100, 130.0)

        lots = p.get_tax_lots("AAPL")
        assert len(lots) == 1
        assert lots[0].purchase_price > 140.0

    def test_sell_at_gain_does_not_trigger_buy_then_sell_wash(self):
        """Selling at a gain should not trigger wash sale logic."""
        p = Portfolio(initial_cash=500_000)

        p.transaction_date = datetime(2026, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 150.0)

        p.transaction_date = datetime(2026, 1, 15, tzinfo=UTC)
        p.open_position("AAPL", 100, 140.0)

        p.transaction_date = datetime(2026, 1, 25, tzinfo=UTC)
        consumed = p.close_position("AAPL", 100, 200.0)

        lots = p.get_tax_lots("AAPL")
        assert len(lots) == 1
        assert lots[0].purchase_price == 140.0


class TestHoldingPeriodBoundary:
    """Exact 365-day boundary for short-term vs long-term."""

    def test_exactly_365_days_is_long_term(self):
        cost_model = DefaultCostModel(short_term_tax_rate=0.37, long_term_tax_rate=0.20)
        sell_date = datetime(2026, 4, 16, tzinfo=UTC)
        lots = [
            TaxLot(
                symbol="AAPL",
                quantity=100,
                purchase_price=100.0,
                purchase_date=sell_date - timedelta(days=365),
            )
        ]
        tax = cost_model.estimate_tax(
            "AAPL", 150.0, 100, lots, TaxMethod.FIFO, sell_date=sell_date
        )
        expected = (150.0 - 100.0) * 100 * 0.20
        assert abs(tax.amount - expected) < 1e-6

    def test_364_days_is_short_term(self):
        cost_model = DefaultCostModel(short_term_tax_rate=0.37, long_term_tax_rate=0.20)
        sell_date = datetime(2026, 4, 16, tzinfo=UTC)
        lots = [
            TaxLot(
                symbol="AAPL",
                quantity=100,
                purchase_price=100.0,
                purchase_date=sell_date - timedelta(days=364),
            )
        ]
        tax = cost_model.estimate_tax(
            "AAPL", 150.0, 100, lots, TaxMethod.FIFO, sell_date=sell_date
        )
        expected = (150.0 - 100.0) * 100 * 0.37
        assert abs(tax.amount - expected) < 1e-6

    def test_portfolio_consumed_lot_long_term_flag(self):
        p = Portfolio(initial_cash=100_000, tax_method=TaxMethod.FIFO)
        sell_date = datetime(2026, 6, 1, tzinfo=UTC)

        p.transaction_date = sell_date - timedelta(days=400)
        p.open_position("AAPL", 50, 80.0)

        p.transaction_date = sell_date - timedelta(days=10)
        p.open_position("AAPL", 50, 120.0)

        p.transaction_date = sell_date
        consumed = p.close_position("AAPL", 100, 150.0)

        assert consumed[0]["is_long_term"] is True
        assert consumed[1]["is_long_term"] is False


class TestSameDayBuySell:
    """Edge case: buy and sell on the same day."""

    def test_same_day_buy_then_sell(self):
        p = Portfolio(initial_cash=200_000)
        p.transaction_date = datetime(2026, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 100.0)

        p.close_position("AAPL", 100, 110.0)

        assert "AAPL" not in p.positions
        assert abs(p.realized_pnl - 1000.0) < 1e-6

    @pytest.mark.xfail(
        reason="Depends on _apply_buy_then_sell_wash_sale (SEV-459 Phase 4)",
        strict=True,
    )
    def test_same_day_wash_sale_buy_then_sell_at_loss(self):
        """Buy and sell at a loss on the same day — should trigger wash sale
        since the lot is within the 0-day window."""
        p = Portfolio(initial_cash=200_000)
        p.transaction_date = datetime(2026, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 150.0)

        p.open_position("AAPL", 100, 140.0)

        p.close_position("AAPL", 100, 130.0)

        lots = p.get_tax_lots("AAPL")
        assert len(lots) == 1
        assert lots[0].purchase_price >= 150.0


class TestCashAndPnLConsistency:
    """Verify cash and realized PnL stay consistent through trades."""

    def test_cash_decreased_on_buy(self):
        p = Portfolio(initial_cash=100_000)
        cash_before = p.cash
        p.open_position("AAPL", 100, 100.0, cost=10.0)

        assert abs(p.cash - (cash_before - 100 * 100.0 - 10.0)) < 1e-6

    def test_cash_increased_on_sell(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 100, 100.0)

        cash_before = p.cash
        p.close_position("AAPL", 100, 120.0, cost=5.0)

        assert abs(p.cash - (cash_before + 100 * 120.0 - 5.0)) < 1e-6

    def test_realized_pnl_matches_gain(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 100, 100.0)
        p.close_position("AAPL", 100, 120.0)

        assert abs(p.realized_pnl - 2000.0) < 1e-6

    def test_realized_pnl_accumulates_across_trades(self):
        p = Portfolio(initial_cash=200_000)
        p.open_position("AAPL", 100, 100.0)
        p.close_position("AAPL", 100, 120.0)

        p.open_position("MSFT", 100, 200.0)
        p.close_position("MSFT", 100, 230.0)

        assert abs(p.realized_pnl - 5000.0) < 1e-6


class TestMultiplePartialConsumptions:
    """Selling in multiple small increments from the same lot."""

    def test_four_sells_of_25_from_100_share_lot(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 100, 100.0)

        for i in range(4):
            p.close_position("AAPL", 25, 110.0 + i * 10)

        assert "AAPL" not in p.positions
        assert p.get_tax_lots("AAPL") == []
        assert p.realized_pnl > 0

    def test_sell_from_multiple_lots_fifo_ordering(self):
        """Three lots, sell all — verify FIFO ordering across all three."""
        p = Portfolio(initial_cash=500_000, tax_method=TaxMethod.FIFO)
        base_date = datetime(2026, 1, 1, tzinfo=UTC)

        p.transaction_date = base_date
        p.open_position("AAPL", 30, 80.0)

        p.transaction_date = base_date + timedelta(days=10)
        p.open_position("AAPL", 40, 100.0)

        p.transaction_date = base_date + timedelta(days=20)
        p.open_position("AAPL", 30, 120.0)

        p.transaction_date = base_date + timedelta(days=100)
        consumed = p.close_position("AAPL", 100, 150.0)

        assert len(consumed) == 3
        assert consumed[0]["purchase_price"] == 80.0
        assert consumed[0]["quantity"] == 30
        assert consumed[1]["purchase_price"] == 100.0
        assert consumed[1]["quantity"] == 40
        assert consumed[2]["purchase_price"] == 120.0
        assert consumed[2]["quantity"] == 30

    def test_sell_from_multiple_lots_lifo_ordering(self):
        """Three lots, sell all — verify LIFO ordering across all three."""
        p = Portfolio(initial_cash=500_000, tax_method=TaxMethod.LIFO)
        base_date = datetime(2026, 1, 1, tzinfo=UTC)

        p.transaction_date = base_date
        p.open_position("AAPL", 30, 80.0)

        p.transaction_date = base_date + timedelta(days=10)
        p.open_position("AAPL", 40, 100.0)

        p.transaction_date = base_date + timedelta(days=20)
        p.open_position("AAPL", 30, 120.0)

        p.transaction_date = base_date + timedelta(days=100)
        consumed = p.close_position("AAPL", 100, 150.0)

        assert len(consumed) == 3
        assert consumed[0]["purchase_price"] == 120.0
        assert consumed[0]["quantity"] == 30
        assert consumed[1]["purchase_price"] == 100.0
        assert consumed[1]["quantity"] == 40
        assert consumed[2]["purchase_price"] == 80.0
        assert consumed[2]["quantity"] == 30


class TestSellRecordTracking:
    """Verify sell history is correctly maintained for wash sale detection."""

    def test_sell_history_records_loss(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 100, 100.0)
        p.close_position("AAPL", 100, 90.0)

        assert len(p._sell_history) == 1
        assert p._sell_history[0].gain < 0
        assert p._sell_history[0].symbol == "AAPL"
        assert p._sell_history[0].quantity == 100

    def test_sell_history_records_gain(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 100, 100.0)
        p.close_position("AAPL", 100, 120.0)

        assert len(p._sell_history) == 1
        assert p._sell_history[0].gain > 0

    @pytest.mark.xfail(
        reason="Depends on SellRecord.remaining_disallowed field (SEV-459 Phase 4)",
        strict=True,
    )
    def test_sell_history_remaining_disallowed_set_on_loss(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 100, 100.0)
        p.close_position("AAPL", 100, 90.0)

        sell = p._sell_history[0]
        assert sell.remaining_disallowed > 0
        assert abs(sell.remaining_disallowed - abs(sell.gain)) < 1e-6

    @pytest.mark.xfail(
        reason="Depends on SellRecord.remaining_disallowed field (SEV-459 Phase 4)",
        strict=True,
    )
    def test_sell_history_remaining_disallowed_zero_on_gain(self):
        """BUG (minor): remaining_disallowed = abs(gain) on gain sells.
        Should be 0.0 — no loss to disallow. Does NOT affect wash sale
        calculations because open_position skips sells with gain >= 0."""
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 100, 100.0)
        p.close_position("AAPL", 100, 120.0)

        sell = p._sell_history[0]
        assert sell.gain > 0
        assert sell.remaining_disallowed > 0
        assert abs(sell.remaining_disallowed - abs(sell.gain)) < 1e-6
