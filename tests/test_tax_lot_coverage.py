"""
Supplementary QA tests covering edge cases and gaps in tax lot tracking.

Areas addressed:
- Buy creates lot with unique lot_id
- Cost basis includes fees on open_position
- Wash sale buy-then-sell (buy first, then sell at loss within 30 days)
- Wash sale double-count protection across multiple replacement buys
- Sell more shares than held (ValueError)
- Same-day buy/sell
- LIFO long-term holding period flag
- Multiple sells consuming from same lots
- Cash deduction includes fees
- Realized PnL accounts for fees
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from engine.core.cost_model import DefaultCostModel, TaxLot, TaxMethod
from engine.core.portfolio import Portfolio


class TestLotLifecycle:
    def test_open_position_returns_unique_lot_id(self):
        p = Portfolio(initial_cash=200_000)
        p.transaction_date = datetime(2026, 1, 1, tzinfo=UTC)
        lot_id_1 = p.open_position("AAPL", 50, 100.0)
        lot_id_2 = p.open_position("AAPL", 50, 110.0)

        assert isinstance(lot_id_1, uuid.UUID)
        assert isinstance(lot_id_2, uuid.UUID)
        assert lot_id_1 != lot_id_2

    def test_tax_lot_stores_symbol_quantity_price_date(self):
        p = Portfolio(initial_cash=100_000)
        dt = datetime(2026, 3, 15, tzinfo=UTC)
        p.transaction_date = dt
        p.open_position("MSFT", 75, 250.0)

        lots = p.get_tax_lots("MSFT")
        assert len(lots) == 1
        lot = lots[0]
        assert lot.symbol == "MSFT"
        assert lot.quantity == 75
        assert lot.purchase_price == 250.0
        assert lot.purchase_date == dt

    def test_cost_basis_includes_fees(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2026, 1, 1, tzinfo=UTC)
        fee = 9.99
        p.open_position("AAPL", 100, 100.0, cost=fee)

        lots = p.get_tax_lots("AAPL")
        assert len(lots) == 1
        assert lots[0].purchase_price == 100.0

        expected_cash = 100_000 - (100 * 100.0 + fee)
        assert abs(p.cash - expected_cash) < 1e-6

    def test_cash_deducted_includes_fees(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2026, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 50, 100.0, cost=5.0)

        assert abs(p.cash - (100_000 - 5000.0 - 5.0)) < 1e-6


class TestEdgeCaseSellMoreThanHeld:
    def test_sell_more_than_position_raises_value_error(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2026, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 50, 100.0)

        with pytest.raises(ValueError, match="Cannot sell"):
            p.close_position("AAPL", 60, 110.0)

    def test_sell_with_no_position_raises_value_error(self):
        p = Portfolio(initial_cash=100_000)
        with pytest.raises(ValueError, match="No position"):
            p.close_position("AAPL", 10, 100.0)

    def test_sell_more_than_all_lots_cover(self):
        p = Portfolio(initial_cash=100_000, tax_method=TaxMethod.FIFO)
        base = datetime(2026, 1, 1, tzinfo=UTC)

        p.transaction_date = base
        p.open_position("AAPL", 30, 100.0)

        p.transaction_date = base + timedelta(days=10)
        p.open_position("AAPL", 20, 105.0)

        with pytest.raises(ValueError, match="Cannot sell"):
            p.close_position("AAPL", 60, 110.0)


class TestSameDayBuySell:
    def test_same_day_buy_then_sell(self):
        p = Portfolio(initial_cash=100_000)
        dt = datetime(2026, 6, 1, tzinfo=UTC)

        p.transaction_date = dt
        p.open_position("AAPL", 100, 150.0)

        p.transaction_date = dt
        consumed = p.close_position("AAPL", 100, 160.0)

        assert len(consumed) == 1
        assert consumed[0]["is_long_term"] is False
        assert p.realized_pnl == pytest.approx(100 * (160.0 - 150.0))

    def test_same_day_partial_buy_sell(self):
        p = Portfolio(initial_cash=100_000)
        dt = datetime(2026, 6, 1, tzinfo=UTC)

        p.transaction_date = dt
        p.open_position("AAPL", 100, 100.0)

        p.transaction_date = dt
        p.close_position("AAPL", 40, 110.0)

        lots = p.get_tax_lots("AAPL")
        assert len(lots) == 1
        assert lots[0].quantity == 60

    def test_same_day_buy_sell_at_loss_no_long_term(self):
        p = Portfolio(initial_cash=100_000)
        dt = datetime(2026, 6, 1, tzinfo=UTC)

        p.transaction_date = dt
        p.open_position("AAPL", 100, 150.0)

        p.transaction_date = dt
        consumed = p.close_position("AAPL", 100, 140.0)

        assert consumed[0]["is_long_term"] is False
        assert p.realized_pnl < 0


class TestWashSaleBuyThenSell:
    def test_buy_then_sell_at_loss_within_30_days_triggers_wash(self):
        p = Portfolio(initial_cash=200_000)
        cost_model = DefaultCostModel()

        buy_date = datetime(2026, 1, 15, tzinfo=UTC)
        sell_date = datetime(2026, 2, 1, tzinfo=UTC)
        rebuy_date = datetime(2026, 2, 10, tzinfo=UTC)

        p.transaction_date = buy_date
        p.open_position("AAPL", 100, 150.0)

        p.transaction_date = sell_date
        p.close_position("AAPL", 100, 140.0)

        assert p.realized_pnl < 0

        sell_loss = p._sell_history[-1].gain
        assert sell_loss < 0

        p.transaction_date = rebuy_date
        p.open_position("AAPL", 100, 145.0)

        lots = p.get_tax_lots("AAPL")
        assert len(lots) == 1
        assert lots[0].purchase_price > 145.0

        expected_adjustment = abs(sell_loss) / 100
        assert abs(lots[0].purchase_price - (145.0 + expected_adjustment)) < 1e-6

    def test_buy_then_sell_outside_window_no_wash(self):
        p = Portfolio(initial_cash=200_000)

        buy_date = datetime(2026, 1, 1, tzinfo=UTC)
        sell_date = datetime(2026, 2, 1, tzinfo=UTC)
        rebuy_date = datetime(2026, 4, 1, tzinfo=UTC)

        p.transaction_date = buy_date
        p.open_position("AAPL", 100, 150.0)

        p.transaction_date = sell_date
        p.close_position("AAPL", 100, 140.0)

        p.transaction_date = rebuy_date
        p.open_position("AAPL", 100, 145.0)

        lots = p.get_tax_lots("AAPL")
        assert len(lots) == 1
        assert lots[0].purchase_price == 145.0


class TestWashSaleDoubleCountProtection:
    def test_multiple_replacement_bots_dont_exceed_original_loss(self):
        p = Portfolio(initial_cash=500_000)

        p.transaction_date = datetime(2026, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 150.0)

        p.transaction_date = datetime(2026, 3, 1, tzinfo=UTC)
        p.close_position("AAPL", 100, 140.0)

        loss = p._sell_history[-1].gain
        assert loss < 0
        total_loss = abs(loss)

        p.transaction_date = datetime(2026, 3, 5, tzinfo=UTC)
        p.open_position("AAPL", 50, 142.0)

        p.transaction_date = datetime(2026, 3, 10, tzinfo=UTC)
        p.open_position("AAPL", 50, 143.0)

        lots = p.get_tax_lots("AAPL")
        assert len(lots) == 2

        total_adjustment = sum(
            (lot.purchase_price - base_price) * lot.quantity
            for lot, base_price in [(lots[0], 142.0), (lots[1], 143.0)]
        )
        assert total_adjustment <= total_loss + 1e-6

    def test_wash_sale_only_applies_to_matching_symbol(self):
        p = Portfolio(initial_cash=300_000)

        p.transaction_date = datetime(2026, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 150.0)

        p.transaction_date = datetime(2026, 3, 1, tzinfo=UTC)
        p.close_position("AAPL", 100, 140.0)

        p.transaction_date = datetime(2026, 3, 5, tzinfo=UTC)
        p.open_position("MSFT", 100, 300.0)

        msft_lots = p.get_tax_lots("MSFT")
        assert len(msft_lots) == 1
        assert msft_lots[0].purchase_price == 300.0


class TestMultipleSellsConsumingLots:
    def test_fifo_three_sells_drain_lots_in_order(self):
        p = Portfolio(initial_cash=200_000, tax_method=TaxMethod.FIFO)
        base = datetime(2026, 1, 1, tzinfo=UTC)

        p.transaction_date = base
        p.open_position("AAPL", 100, 80.0)

        p.transaction_date = base + timedelta(days=10)
        p.open_position("AAPL", 100, 90.0)

        p.transaction_date = base + timedelta(days=20)
        p.open_position("AAPL", 100, 100.0)

        p.transaction_date = base + timedelta(days=30)
        c1 = p.close_position("AAPL", 100, 110.0)
        assert c1[0]["purchase_price"] == 80.0

        p.transaction_date = base + timedelta(days=40)
        c2 = p.close_position("AAPL", 100, 115.0)
        assert c2[0]["purchase_price"] == 90.0

        p.transaction_date = base + timedelta(days=50)
        c3 = p.close_position("AAPL", 100, 120.0)
        assert c3[0]["purchase_price"] == 100.0

        assert p.get_tax_lots("AAPL") == []
        assert "AAPL" not in p.positions

    def test_lifo_three_sells_drain_newest_first(self):
        p = Portfolio(initial_cash=200_000, tax_method=TaxMethod.LIFO)
        base = datetime(2026, 1, 1, tzinfo=UTC)

        p.transaction_date = base
        p.open_position("AAPL", 100, 80.0)

        p.transaction_date = base + timedelta(days=10)
        p.open_position("AAPL", 100, 90.0)

        p.transaction_date = base + timedelta(days=20)
        p.open_position("AAPL", 100, 100.0)

        p.transaction_date = base + timedelta(days=30)
        c1 = p.close_position("AAPL", 100, 110.0)
        assert c1[0]["purchase_price"] == 100.0

        p.transaction_date = base + timedelta(days=40)
        c2 = p.close_position("AAPL", 100, 115.0)
        assert c2[0]["purchase_price"] == 90.0

        p.transaction_date = base + timedelta(days=50)
        c3 = p.close_position("AAPL", 100, 120.0)
        assert c3[0]["purchase_price"] == 80.0


class TestRealizedPnlWithFees:
    def test_sell_proceeds_subtract_fees_and_tax(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2026, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 100.0)

        p.transaction_date = datetime(2026, 6, 1, tzinfo=UTC)
        fee = 10.0
        tax = 50.0
        p.close_position("AAPL", 100, 120.0, cost=fee, tax=tax)

        expected_pnl = 100 * 120.0 - 100 * 100.0 - fee - tax
        assert abs(p.realized_pnl - expected_pnl) < 1e-6

    def test_cash_increased_by_proceeds_minus_fees_tax(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2026, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 100.0)

        cash_after_buy = p.cash

        p.transaction_date = datetime(2026, 6, 1, tzinfo=UTC)
        fee = 10.0
        tax = 50.0
        p.close_position("AAPL", 100, 120.0, cost=fee, tax=tax)

        expected_cash = cash_after_buy + 100 * 120.0 - fee - tax
        assert abs(p.cash - expected_cash) < 1e-6


class TestHoldingPeriodEdge:
    def test_exactly_365_days_is_long_term(self):
        p = Portfolio(initial_cash=100_000)
        buy_date = datetime(2025, 1, 1, tzinfo=UTC)
        sell_date = datetime(2026, 1, 1, tzinfo=UTC)

        p.transaction_date = buy_date
        p.open_position("AAPL", 100, 100.0)

        p.transaction_date = sell_date
        consumed = p.close_position("AAPL", 100, 150.0)

        assert consumed[0]["is_long_term"] is True

    def test_364_days_is_short_term(self):
        p = Portfolio(initial_cash=100_000)
        buy_date = datetime(2025, 1, 1, tzinfo=UTC)
        sell_date = datetime(2025, 12, 31, tzinfo=UTC)

        p.transaction_date = buy_date
        p.open_position("AAPL", 100, 100.0)

        p.transaction_date = sell_date
        consumed = p.close_position("AAPL", 100, 150.0)

        assert consumed[0]["is_long_term"] is False

    def test_lifo_long_term_flag_on_old_lot(self):
        p = Portfolio(initial_cash=100_000, tax_method=TaxMethod.LIFO)
        base = datetime(2026, 1, 1, tzinfo=UTC)

        p.transaction_date = base - timedelta(days=400)
        p.open_position("AAPL", 50, 80.0)

        p.transaction_date = base - timedelta(days=10)
        p.open_position("AAPL", 50, 120.0)

        p.transaction_date = base
        consumed = p.close_position("AAPL", 50, 150.0)

        assert consumed[0]["purchase_price"] == 120.0
        assert consumed[0]["is_long_term"] is False

        p.transaction_date = base + timedelta(days=1)
        remaining = p.close_position("AAPL", 50, 155.0)
        assert remaining[0]["purchase_price"] == 80.0
        assert remaining[0]["is_long_term"] is True


class TestInsufficientCash:
    def test_open_position_raises_when_insufficient_cash(self):
        p = Portfolio(initial_cash=1_000)
        with pytest.raises(ValueError, match="Insufficient cash"):
            p.transaction_date = datetime(2026, 1, 1, tzinfo=UTC)
            p.open_position("AAPL", 100, 150.0)


class TestTaxLotCostBasisProperty:
    def test_cost_basis_is_quantity_times_price(self):
        lot = TaxLot(
            symbol="AAPL",
            quantity=100,
            purchase_price=150.0,
            purchase_date=datetime(2026, 1, 1, tzinfo=UTC),
        )
        assert lot.cost_basis == 100 * 150.0

    def test_lot_id_defaults_empty_string(self):
        lot = TaxLot(
            symbol="AAPL",
            quantity=10,
            purchase_price=100.0,
            purchase_date=datetime(2026, 1, 1, tzinfo=UTC),
        )
        assert lot.lot_id == ""


class TestPositionRemoval:
    def test_position_removed_when_fully_closed(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2026, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 100.0)

        p.transaction_date = datetime(2026, 6, 1, tzinfo=UTC)
        p.close_position("AAPL", 100, 110.0)

        assert "AAPL" not in p.positions
        assert p.get_tax_lots("AAPL") == []

    def test_can_reopen_after_full_close(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2026, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 100.0)

        p.transaction_date = datetime(2026, 6, 1, tzinfo=UTC)
        p.close_position("AAPL", 100, 110.0)

        p.transaction_date = datetime(2026, 7, 1, tzinfo=UTC)
        p.open_position("AAPL", 50, 115.0)

        assert "AAPL" in p.positions
        assert p.positions["AAPL"].quantity == 50
        lots = p.get_tax_lots("AAPL")
        assert len(lots) == 1
        assert lots[0].purchase_price == 115.0


class TestTaxLotIsLongTerm:
    def test_is_long_term_at_exactly_365(self):
        dt = datetime(2025, 1, 1, tzinfo=UTC)
        lot = TaxLot(symbol="X", quantity=1, purchase_price=1.0, purchase_date=dt)
        assert lot.is_long_term(as_of=dt + timedelta(days=365)) is True

    def test_is_short_term_at_364(self):
        dt = datetime(2025, 1, 1, tzinfo=UTC)
        lot = TaxLot(symbol="X", quantity=1, purchase_price=1.0, purchase_date=dt)
        assert lot.is_long_term(as_of=dt + timedelta(days=364)) is False


class TestWashSalePartialQuantity:
    def test_wash_sale_with_smaller_rebuy_quantity(self):
        p = Portfolio(initial_cash=300_000)

        p.transaction_date = datetime(2026, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 150.0)

        p.transaction_date = datetime(2026, 3, 1, tzinfo=UTC)
        p.close_position("AAPL", 100, 140.0)

        loss_per_share = abs(p._sell_history[-1].gain) / 100

        p.transaction_date = datetime(2026, 3, 10, tzinfo=UTC)
        p.open_position("AAPL", 50, 142.0)

        lots = p.get_tax_lots("AAPL")
        assert len(lots) == 1
        expected_adjustment = min(50, 100) * (loss_per_share) / 50
        assert lots[0].purchase_price == pytest.approx(142.0 + expected_adjustment)


class TestPortfolioDefaultTaxMethod:
    def test_default_tax_method_is_fifo(self):
        p = Portfolio(initial_cash=100_000)
        assert p.tax_method == TaxMethod.FIFO

    def test_set_tax_method_changes_method(self):
        p = Portfolio(initial_cash=100_000)
        p.set_tax_method(TaxMethod.LIFO)
        assert p.tax_method == TaxMethod.LIFO
