"""
Comprehensive tests for Portfolio targeting uncovered code paths.

Covers: LIFO/SPECIFIC_LOT tax consumption, wash sale adjustments, multiple buys
averaging, open_position with cost, close_position with cost/tax, total_value with
multiple positions, portfolio_id and transaction_date, get_tax_lots, sell_history
tracking, and more.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from engine.core.cost_model import DefaultCostModel, TaxMethod
from engine.core.portfolio import Portfolio, PortfolioSnapshot, Position, TradeRecord

# ── Portfolio constructor ──


class TestPortfolioConstructor:
    def test_default_initial_cash(self):
        p = Portfolio()
        assert p.initial_cash == 100_000.0
        assert p.cash == 100_000.0

    def test_custom_initial_cash(self):
        p = Portfolio(initial_cash=50_000.0)
        assert p.cash == 50_000.0

    def test_default_cost_model_created(self):
        p = Portfolio()
        assert p._cost_model is not None
        assert isinstance(p._cost_model, DefaultCostModel)

    def test_custom_cost_model(self):
        cm = DefaultCostModel(commission_per_trade=5.0)
        p = Portfolio(initial_cash=100_000, _cost_model=cm)
        assert p._cost_model is cm

    def test_portfolio_id(self):
        pid = uuid4()
        p = Portfolio(initial_cash=100_000, portfolio_id=pid)
        assert p.portfolio_id == pid

    def test_portfolio_id_default_none(self):
        p = Portfolio()
        assert p.portfolio_id is None

    def test_transaction_date_default_none(self):
        p = Portfolio()
        assert p.transaction_date is None

    def test_realized_pnl_starts_at_zero(self):
        p = Portfolio()
        assert p.realized_pnl == 0.0

    def test_default_tax_method_fifo(self):
        p = Portfolio()
        assert p.tax_method == TaxMethod.FIFO


# ── Position properties ──


class TestPositionProperties:
    def test_market_value_with_current_price(self):
        pos = Position(symbol="AAPL", quantity=100, avg_cost=50.0, current_price=150.0)
        assert pos.market_value == 15_000.0

    def test_market_value_fallback_to_avg_cost(self):
        pos = Position(symbol="AAPL", quantity=100, avg_cost=50.0, current_price=0.0)
        assert pos.market_value == 5_000.0

    def test_market_value_zero_quantity(self):
        pos = Position(symbol="AAPL", quantity=0, avg_cost=100.0, current_price=150.0)
        assert pos.market_value == 0

    def test_is_zero_true(self):
        pos = Position(symbol="AAPL", quantity=0)
        assert pos.is_zero is True

    def test_is_zero_false(self):
        pos = Position(symbol="AAPL", quantity=1)
        assert pos.is_zero is False


# ── Total value and return ──


class TestTotalValue:
    def test_total_value_no_positions(self):
        p = Portfolio(initial_cash=100_000)
        assert p.total_value == 100_000

    def test_total_value_with_one_position(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 10, 100.0)
        p.update_prices({"AAPL": 120.0})
        assert p.total_value == p.cash + 10 * 120.0

    def test_total_value_with_multiple_positions(self):
        p = Portfolio(initial_cash=200_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 10, 100.0)
        p.open_position("MSFT", 20, 200.0)
        p.update_prices({"AAPL": 120.0, "MSFT": 220.0})
        expected = p.cash + 10 * 120.0 + 20 * 220.0
        assert p.total_value == expected

    def test_total_return_pct_positive(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 100.0)
        p.update_prices({"AAPL": 110.0})
        assert p.total_return_pct > 0

    def test_total_return_pct_negative(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 100.0)
        p.update_prices({"AAPL": 90.0})
        assert p.total_return_pct < 0

    def test_total_return_pct_zero_initial(self):
        p = Portfolio(initial_cash=0.0)
        assert p.total_return_pct == 0.0


# ── Open position with cost ──


class TestOpenPositionWithCost:
    def test_cost_reduces_cash_extra(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 10, 100.0, cost=50.0)
        expected_cash = 100_000 - (10 * 100.0) - 50.0
        assert p.cash == expected_cash

    def test_zero_cost_same_as_no_cost(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 10, 100.0, cost=0.0)
        expected_cash = 100_000 - 10 * 100.0
        assert p.cash == expected_cash


# ── Multiple buys: avg cost recalculation ──


class TestMultipleBuysAveraging:
    def test_two_buys_avg_cost(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 10, 100.0)
        p.transaction_date = datetime(2025, 2, 1, tzinfo=UTC)
        p.open_position("AAPL", 10, 200.0)

        pos = p.positions["AAPL"]
        assert pos.quantity == 20
        expected_avg = (10 * 100.0 + 10 * 200.0) / 20
        assert pos.avg_cost == pytest.approx(expected_avg)

    def test_three_buys_avg_cost(self):
        p = Portfolio(initial_cash=1_000_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 100.0)
        p.transaction_date = datetime(2025, 2, 1, tzinfo=UTC)
        p.open_position("AAPL", 200, 150.0)
        p.transaction_date = datetime(2025, 3, 1, tzinfo=UTC)
        p.open_position("AAPL", 300, 200.0)

        pos = p.positions["AAPL"]
        assert pos.quantity == 600
        total_cost = 100 * 100.0 + 200 * 150.0 + 300 * 200.0
        assert pos.avg_cost == pytest.approx(total_cost / 600)

    def test_two_buys_creates_two_tax_lots(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 10, 100.0)
        p.transaction_date = datetime(2025, 2, 1, tzinfo=UTC)
        p.open_position("AAPL", 10, 200.0)

        lots = p.get_tax_lots("AAPL")
        assert len(lots) == 2
        assert lots[0].purchase_price == 100.0
        assert lots[1].purchase_price == 200.0


# ── Close position with cost and tax ──


class TestClosePositionWithCostAndTax:
    def test_close_with_cost_only(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 10, 100.0)
        p.transaction_date = datetime(2025, 6, 1, tzinfo=UTC)
        p.close_position("AAPL", 10, 150.0, cost=25.0)

        expected_cash = 100_000 - 10 * 100.0 + (10 * 150.0 - 25.0)
        assert p.cash == pytest.approx(expected_cash)

    def test_close_with_tax_only(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 10, 100.0)
        p.transaction_date = datetime(2025, 6, 1, tzinfo=UTC)
        p.close_position("AAPL", 10, 150.0, tax=100.0)

        expected_cash = 100_000 - 10 * 100.0 + (10 * 150.0 - 100.0)
        assert p.cash == pytest.approx(expected_cash)

    def test_close_with_cost_and_tax(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 10, 100.0)
        p.transaction_date = datetime(2025, 6, 1, tzinfo=UTC)
        p.close_position("AAPL", 10, 150.0, cost=25.0, tax=100.0)

        expected_cash = 100_000 - 10 * 100.0 + (10 * 150.0 - 25.0 - 100.0)
        assert p.cash == pytest.approx(expected_cash)

    def test_realized_pnl_with_costs_and_tax(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 10, 100.0)
        p.transaction_date = datetime(2025, 6, 1, tzinfo=UTC)
        p.close_position("AAPL", 10, 150.0, cost=25.0, tax=100.0)

        expected_pnl = 10 * 150.0 - 10 * 100.0 - 25.0 - 100.0
        assert p.realized_pnl == pytest.approx(expected_pnl)


# ── LIFO tax consumption ──


class TestLIFOConsumption:
    def test_lifo_consumes_latest_lot_first(self):
        p = Portfolio(initial_cash=500_000, tax_method=TaxMethod.LIFO)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 100.0)
        p.transaction_date = datetime(2025, 3, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 120.0)

        p.transaction_date = datetime(2025, 6, 1, tzinfo=UTC)
        consumed = p.close_position("AAPL", 50, 150.0)

        assert len(consumed) == 1
        assert consumed[0]["purchase_price"] == 120.0

    def test_lifo_consumes_across_lots(self):
        p = Portfolio(initial_cash=500_000, tax_method=TaxMethod.LIFO)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 100.0)
        p.transaction_date = datetime(2025, 3, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 120.0)

        p.transaction_date = datetime(2025, 6, 1, tzinfo=UTC)
        consumed = p.close_position("AAPL", 150, 150.0)

        assert len(consumed) == 2
        assert consumed[0]["purchase_price"] == 120.0
        assert consumed[0]["quantity"] == 100
        assert consumed[1]["purchase_price"] == 100.0
        assert consumed[1]["quantity"] == 50


# ── SPECIFIC_LOT tax consumption ──


class TestSpecificLotConsumption:
    def test_specific_lot_preserves_insertion_order(self):
        p = Portfolio(initial_cash=500_000, tax_method=TaxMethod.SPECIFIC_LOT)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 100.0)
        p.transaction_date = datetime(2025, 6, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 80.0)

        p.transaction_date = datetime(2025, 9, 1, tzinfo=UTC)
        consumed = p.close_position("AAPL", 50, 150.0)

        assert len(consumed) == 1
        assert consumed[0]["purchase_price"] == 100.0


# ── Partial sells and remaining positions ──


class TestPartialSells:
    def test_partial_sell_leaves_remaining(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 100.0)
        p.transaction_date = datetime(2025, 6, 1, tzinfo=UTC)
        p.close_position("AAPL", 40, 150.0)

        assert p.positions["AAPL"].quantity == 60

    def test_two_partial_sells(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 100.0)
        p.transaction_date = datetime(2025, 6, 1, tzinfo=UTC)
        p.close_position("AAPL", 30, 150.0)
        p.transaction_date = datetime(2025, 9, 1, tzinfo=UTC)
        p.close_position("AAPL", 30, 160.0)

        assert p.positions["AAPL"].quantity == 40

    def test_full_sell_removes_position(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 100.0)
        p.transaction_date = datetime(2025, 6, 1, tzinfo=UTC)
        p.close_position("AAPL", 100, 150.0)

        assert "AAPL" not in p.positions


# ── Tax lot cleanup ──


class TestTaxLotCleanup:
    def test_fully_consumed_lot_removed(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 100.0)
        lots_before = p.get_tax_lots("AAPL")
        assert len(lots_before) == 1

        p.transaction_date = datetime(2025, 6, 1, tzinfo=UTC)
        p.close_position("AAPL", 100, 150.0)
        lots_after = p.get_tax_lots("AAPL")
        assert len(lots_after) == 0

    def test_partial_consumed_lot_quantity_reduced(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 100.0)

        p.transaction_date = datetime(2025, 6, 1, tzinfo=UTC)
        p.close_position("AAPL", 30, 150.0)

        lots = p.get_tax_lots("AAPL")
        assert len(lots) == 1
        assert lots[0].quantity == 70


# ── Sell history tracking ──


class TestSellHistory:
    def test_sell_history_records_sell(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 100.0)
        p.transaction_date = datetime(2025, 6, 1, tzinfo=UTC)
        p.close_position("AAPL", 50, 150.0)

        assert len(p._sell_history) == 1
        sell = p._sell_history[0]
        assert sell.symbol == "AAPL"
        assert sell.quantity == 50
        assert sell.sell_price == 150.0
        assert sell.cost_basis == 50 * 100.0

    def test_sell_history_gain_calculation(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 100.0)
        p.transaction_date = datetime(2025, 6, 1, tzinfo=UTC)
        p.close_position("AAPL", 100, 150.0)

        sell = p._sell_history[0]
        expected_gain = 100 * 150.0 - 100 * 100.0
        assert sell.gain == pytest.approx(expected_gain)

    def test_sell_history_loss_case(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 150.0)
        p.transaction_date = datetime(2025, 6, 1, tzinfo=UTC)
        p.close_position("AAPL", 100, 100.0)

        sell = p._sell_history[0]
        assert sell.gain < 0


# ── Wash sale adjustment on buy ──


class TestWashSaleOnBuy:
    def test_buy_within_30_days_of_loss_sell_adjusts_price(self):
        p = Portfolio(initial_cash=200_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 150.0)

        p.transaction_date = datetime(2025, 3, 1, tzinfo=UTC)
        p.close_position("AAPL", 100, 100.0)
        assert p._sell_history[0].gain < 0

        p.transaction_date = datetime(2025, 3, 15, tzinfo=UTC)
        p.open_position("AAPL", 100, 110.0)

        lots = p.get_tax_lots("AAPL")
        assert len(lots) == 1
        assert lots[0].purchase_price > 110.0

    def test_buy_outside_30_days_no_adjustment(self):
        p = Portfolio(initial_cash=200_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 150.0)

        p.transaction_date = datetime(2025, 3, 1, tzinfo=UTC)
        p.close_position("AAPL", 100, 100.0)

        p.transaction_date = datetime(2025, 5, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 110.0)

        lots = p.get_tax_lots("AAPL")
        assert lots[0].purchase_price == 110.0


# ── Get tax lots ──


class TestGetTaxLots:
    def test_unknown_symbol_returns_empty(self):
        p = Portfolio()
        assert p.get_tax_lots("UNKNOWN") == []

    def test_returns_all_lots(self):
        p = Portfolio(initial_cash=500_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 50, 100.0)
        p.transaction_date = datetime(2025, 2, 1, tzinfo=UTC)
        p.open_position("AAPL", 50, 120.0)

        lots = p.get_tax_lots("AAPL")
        assert len(lots) == 2


# ── Update prices ──


class TestUpdatePricesComprehensive:
    def test_update_multiple_positions(self):
        p = Portfolio(initial_cash=200_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 10, 100.0)
        p.open_position("MSFT", 20, 200.0)

        p.update_prices({"AAPL": 110.0, "MSFT": 210.0})
        assert p.positions["AAPL"].current_price == 110.0
        assert p.positions["MSFT"].current_price == 210.0

    def test_update_empty_dict_no_change(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 10, 100.0)
        p.update_prices({})
        assert p.positions["AAPL"].current_price == 0.0


# ── Snapshot ──


class TestSnapshotComprehensive:
    def test_snapshot_with_multiple_positions(self):
        p = Portfolio(initial_cash=200_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 10, 100.0)
        p.open_position("MSFT", 20, 200.0)
        p.update_prices({"AAPL": 120.0, "MSFT": 220.0})

        snap = p.snapshot()
        assert "AAPL" in snap.positions
        assert "MSFT" in snap.positions
        assert snap.positions["AAPL"]["current_price"] == 120.0
        assert snap.positions["MSFT"]["current_price"] == 220.0
        assert snap.realized_pnl == 0.0

    def test_snapshot_after_sell_has_realized_pnl(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 10, 100.0)
        p.transaction_date = datetime(2025, 6, 1, tzinfo=UTC)
        p.close_position("AAPL", 10, 150.0)

        snap = p.snapshot()
        assert snap.realized_pnl > 0

    def test_snapshot_is_immutable_copy(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 10, 100.0)
        snap = p.snapshot()

        p.update_prices({"AAPL": 999.0})
        assert snap.positions["AAPL"]["current_price"] != 999.0


# ── PortfolioSnapshot ──


class TestPortfolioSnapshotMethods:
    def test_allocation_weight_mixed_portfolio(self):
        snap = PortfolioSnapshot(
            cash=30_000.0,
            positions={
                "AAPL": {"quantity": 100, "avg_cost": 100.0, "current_price": 150.0},
                "MSFT": {"quantity": 50, "avg_cost": 200.0, "current_price": 250.0},
            },
            total_value=67_500.0,
            total_return_pct=10.0,
            realized_pnl=0.0,
        )
        aapl_weight = snap.allocation_weight("AAPL")
        msft_weight = snap.allocation_weight("MSFT")
        assert aapl_weight > 0
        assert msft_weight > 0
        assert aapl_weight + msft_weight < 100

    def test_summary_string(self):
        snap = PortfolioSnapshot(
            cash=50_000.0,
            positions={},
            total_value=50_000.0,
            total_return_pct=-50.0,
            realized_pnl=0.0,
        )
        s = snap.summary()
        assert "Cash: $50,000" in s
        assert "-50.00%" in s


# ── Set tax method ──


class TestSetTaxMethod:
    def test_change_to_lifo(self):
        p = Portfolio(tax_method=TaxMethod.FIFO)
        p.set_tax_method(TaxMethod.LIFO)
        assert p.tax_method == TaxMethod.LIFO

    def test_change_to_specific_lot(self):
        p = Portfolio(tax_method=TaxMethod.FIFO)
        p.set_tax_method(TaxMethod.SPECIFIC_LOT)
        assert p.tax_method == TaxMethod.SPECIFIC_LOT

    def test_change_back_to_fifo(self):
        p = Portfolio(tax_method=TaxMethod.LIFO)
        p.set_tax_method(TaxMethod.FIFO)
        assert p.tax_method == TaxMethod.FIFO


# ── Trade record tracking ──


class TestTradeRecordTracking:
    def test_trade_record_fields_buy(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2025, 1, 15, tzinfo=UTC)
        p.open_position("AAPL", 10, 100.0, cost=5.0)

        tr = p.trade_history[0]
        assert isinstance(tr, TradeRecord)
        assert tr.timestamp == datetime(2025, 1, 15, tzinfo=UTC)
        assert tr.side == "buy"
        assert tr.symbol == "AAPL"
        assert tr.quantity == 10
        assert tr.price == 100.0
        assert tr.cost == 5.0

    def test_trade_record_fields_sell(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 10, 100.0)
        p.transaction_date = datetime(2025, 6, 1, tzinfo=UTC)
        p.close_position("AAPL", 10, 150.0, cost=3.0, tax=50.0)

        tr = p.trade_history[1]
        assert tr.side == "sell"
        assert tr.price == 150.0
        assert tr.cost == 3.0
        assert tr.tax == 50.0


# ── PortfolioState alias ──


class TestPortfolioStateAlias:
    def test_portfolio_state_is_snapshot(self):
        from engine.core.portfolio import PortfolioState
        assert PortfolioState is PortfolioSnapshot


# ── Consumed lot details ──


class TestConsumedLotDetails:
    def test_consumed_lot_has_is_long_term(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 100.0)

        p.transaction_date = datetime(2025, 6, 1, tzinfo=UTC)
        consumed = p.close_position("AAPL", 50, 150.0)

        assert len(consumed) == 1
        assert "is_long_term" in consumed[0]
        assert consumed[0]["is_long_term"] is False

    def test_consumed_lot_long_term(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2024, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 100.0)

        p.transaction_date = datetime(2025, 6, 1, tzinfo=UTC)
        consumed = p.close_position("AAPL", 50, 150.0)

        assert consumed[0]["is_long_term"] is True

    def test_consumed_lot_has_lot_id(self):
        p = Portfolio(initial_cash=100_000)
        p.transaction_date = datetime(2025, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 100.0)
        lots = p.get_tax_lots("AAPL")
        original_lot_id = lots[0].lot_id

        p.transaction_date = datetime(2025, 6, 1, tzinfo=UTC)
        consumed = p.close_position("AAPL", 100, 150.0)
        assert consumed[0]["lot_id"] == original_lot_id
