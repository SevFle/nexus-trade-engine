"""Comprehensive tests for engine.core.portfolio — Portfolio, Position, PortfolioSnapshot."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from engine.core.cost_model import TaxMethod
from engine.core.portfolio import Portfolio, PortfolioSnapshot, Position, SellRecord, TradeRecord


class TestPosition:
    def test_is_zero_when_no_quantity(self):
        pos = Position(symbol="AAPL", quantity=0)
        assert pos.is_zero is True

    def test_is_zero_false_when_holding(self):
        pos = Position(symbol="AAPL", quantity=10)
        assert pos.is_zero is False

    def test_market_value_uses_current_price(self):
        pos = Position(symbol="AAPL", quantity=100, current_price=50.0)
        assert pos.market_value == 5000.0

    def test_market_value_falls_back_to_avg_cost(self):
        pos = Position(symbol="AAPL", quantity=100, avg_cost=30.0, current_price=0.0)
        assert pos.market_value == 3000.0

    def test_market_value_prefers_current_price_over_avg_cost(self):
        pos = Position(symbol="AAPL", quantity=10, avg_cost=100.0, current_price=150.0)
        assert pos.market_value == 1500.0

    def test_zero_quantity_zero_market_value(self):
        pos = Position(symbol="AAPL", quantity=0, current_price=50.0)
        assert pos.market_value == 0.0


class TestPortfolioInit:
    def test_default_initial_cash(self):
        p = Portfolio()
        assert p.cash == 100_000.0

    def test_custom_initial_cash(self):
        p = Portfolio(initial_cash=50_000.0)
        assert p.cash == 50_000.0

    def test_empty_positions(self):
        p = Portfolio()
        assert p.positions == {}

    def test_default_cost_model_created(self):
        p = Portfolio()
        assert p._cost_model is not None

    def test_default_tax_method_fifo(self):
        p = Portfolio()
        assert p.tax_method == TaxMethod.FIFO

    def test_portfolio_id_default_none(self):
        p = Portfolio()
        assert p.portfolio_id is None

    def test_portfolio_id_set(self):
        pid = uuid.uuid4()
        p = Portfolio(portfolio_id=pid)
        assert p.portfolio_id == pid


class TestTotalValue:
    def test_cash_only_equals_initial(self):
        p = Portfolio(initial_cash=100_000.0)
        assert p.total_value == 100_000.0

    def test_with_position(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        p.update_prices({"AAPL": 120.0})
        assert p.total_value == pytest.approx(p.cash + 1200.0)

    def test_multiple_positions(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        p.open_position("MSFT", 5, 200.0)
        p.update_prices({"AAPL": 110.0, "MSFT": 220.0})
        aapl_val = 10 * 110.0
        msft_val = 5 * 220.0
        assert p.total_value == pytest.approx(p.cash + aapl_val + msft_val)


class TestTotalReturnPct:
    def test_zero_return_at_start(self):
        p = Portfolio(initial_cash=100_000.0)
        assert p.total_return_pct == 0.0

    def test_positive_return(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        p.update_prices({"AAPL": 110.0})
        assert p.total_return_pct > 0

    def test_zero_initial_cash(self):
        p = Portfolio(initial_cash=0.0)
        assert p.total_return_pct == 0.0


class TestOpenPosition:
    def test_basic_buy(self):
        p = Portfolio(initial_cash=100_000.0)
        lot_id = p.open_position("AAPL", 10, 100.0)
        assert isinstance(lot_id, uuid.UUID)
        assert "AAPL" in p.positions
        assert p.positions["AAPL"].quantity == 10
        assert p.positions["AAPL"].avg_cost == 100.0

    def test_cash_deducted(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        assert p.cash == 100_000.0 - 1000.0

    def test_cash_deducted_with_cost(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0, cost=5.0)
        assert p.cash == 100_000.0 - 1000.0 - 5.0

    def test_insufficient_cash_raises(self):
        p = Portfolio(initial_cash=100.0)
        with pytest.raises(ValueError, match="Insufficient cash"):
            p.open_position("AAPL", 10, 100.0)

    def test_add_to_existing_position(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        p.open_position("AAPL", 10, 120.0)
        assert p.positions["AAPL"].quantity == 20
        expected_avg = (10 * 100.0 + 10 * 120.0) / 20
        assert p.positions["AAPL"].avg_cost == pytest.approx(expected_avg)

    def test_tax_lot_created(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        lots = p.get_tax_lots("AAPL")
        assert len(lots) == 1
        assert lots[0].quantity == 10
        assert lots[0].purchase_price == 100.0

    def test_multiple_buys_create_multiple_lots(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        p.open_position("AAPL", 5, 110.0)
        lots = p.get_tax_lots("AAPL")
        assert len(lots) == 2

    def test_trade_history_recorded(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0, cost=2.0)
        assert len(p.trade_history) == 1
        trade = p.trade_history[0]
        assert trade.side == "buy"
        assert trade.symbol == "AAPL"
        assert trade.quantity == 10
        assert trade.price == 100.0
        assert trade.cost == 2.0

    def test_no_tax_lots_for_unknown_symbol(self):
        p = Portfolio()
        assert p.get_tax_lots("UNKNOWN") == []

    def test_transaction_date_used_when_set(self):
        ts = datetime(2024, 6, 15, tzinfo=UTC)
        p = Portfolio(initial_cash=100_000.0, transaction_date=ts)
        p.open_position("AAPL", 10, 100.0)
        lots = p.get_tax_lots("AAPL")
        assert lots[0].purchase_date == ts


class TestClosePosition:
    def test_basic_sell(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        consumed = p.close_position("AAPL", 10, 150.0)
        assert len(consumed) == 1
        assert consumed[0]["quantity"] == 10
        assert "AAPL" not in p.positions

    def test_cash_increased_on_sell(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        cash_after_buy = p.cash
        p.close_position("AAPL", 10, 150.0)
        assert p.cash == cash_after_buy + 1500.0

    def test_cash_increased_less_costs(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        cash_after_buy = p.cash
        p.close_position("AAPL", 10, 150.0, cost=5.0, tax=10.0)
        assert p.cash == cash_after_buy + 1500.0 - 5.0 - 10.0

    def test_realized_pnl_profit(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        p.close_position("AAPL", 10, 150.0)
        assert p.realized_pnl > 0
        assert p.realized_pnl == pytest.approx(500.0)

    def test_realized_pnl_loss(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        p.close_position("AAPL", 10, 80.0)
        assert p.realized_pnl < 0
        assert p.realized_pnl == pytest.approx(-200.0)

    def test_realized_pnl_with_costs_and_tax(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        p.close_position("AAPL", 10, 150.0, cost=5.0, tax=10.0)
        assert p.realized_pnl == pytest.approx(500.0 - 5.0 - 10.0)

    def test_partial_sell(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        p.close_position("AAPL", 3, 150.0)
        assert p.positions["AAPL"].quantity == 7

    def test_sell_nonexistent_position_raises(self):
        p = Portfolio(initial_cash=100_000.0)
        with pytest.raises(ValueError, match="No position"):
            p.close_position("AAPL", 10, 150.0)

    def test_sell_more_than_held_raises(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 5, 100.0)
        with pytest.raises(ValueError, match="Cannot sell"):
            p.close_position("AAPL", 10, 150.0)

    def test_sell_history_recorded(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        p.close_position("AAPL", 10, 150.0)
        assert len(p._sell_history) == 1
        sell = p._sell_history[0]
        assert sell.symbol == "AAPL"
        assert sell.sell_price == 150.0
        assert sell.quantity == 10

    def test_trade_history_sell_recorded(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        p.close_position("AAPL", 10, 150.0)
        sells = [t for t in p.trade_history if t.side == "sell"]
        assert len(sells) == 1
        assert sells[0].price == 150.0


class TestFIFOTaxLots:
    def test_fifo_consumes_oldest_first(self):
        p = Portfolio(initial_cash=100_000.0)
        ts1 = datetime(2024, 1, 1, tzinfo=UTC)
        ts2 = datetime(2024, 2, 1, tzinfo=UTC)
        p.transaction_date = ts1
        p.open_position("AAPL", 10, 100.0)
        p.transaction_date = ts2
        p.open_position("AAPL", 10, 120.0)

        p.transaction_date = datetime(2024, 6, 1, tzinfo=UTC)
        consumed = p.close_position("AAPL", 10, 150.0)
        assert len(consumed) == 1
        assert consumed[0]["purchase_price"] == 100.0
        assert consumed[0]["quantity"] == 10

    def test_fifo_spanning_two_lots(self):
        p = Portfolio(initial_cash=100_000.0, tax_method=TaxMethod.FIFO)
        ts1 = datetime(2024, 1, 1, tzinfo=UTC)
        ts2 = datetime(2024, 2, 1, tzinfo=UTC)
        p.transaction_date = ts1
        p.open_position("AAPL", 10, 100.0)
        p.transaction_date = ts2
        p.open_position("AAPL", 10, 120.0)

        p.transaction_date = datetime(2024, 6, 1, tzinfo=UTC)
        consumed = p.close_position("AAPL", 15, 150.0)
        assert len(consumed) == 2
        assert consumed[0]["purchase_price"] == 100.0
        assert consumed[0]["quantity"] == 10
        assert consumed[1]["purchase_price"] == 120.0
        assert consumed[1]["quantity"] == 5

    def test_realized_pnl_fifo_blended_cost(self):
        p = Portfolio(initial_cash=100_000.0, tax_method=TaxMethod.FIFO)
        p.transaction_date = datetime(2024, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 10, 100.0)
        p.transaction_date = datetime(2024, 2, 1, tzinfo=UTC)
        p.open_position("AAPL", 10, 120.0)

        p.transaction_date = datetime(2024, 6, 1, tzinfo=UTC)
        p.close_position("AAPL", 15, 150.0)
        cost_basis = 10 * 100.0 + 5 * 120.0
        proceeds = 15 * 150.0
        assert p.realized_pnl == pytest.approx(proceeds - cost_basis)


class TestLIFOTaxLots:
    def test_lifo_consumes_newest_first(self):
        p = Portfolio(initial_cash=100_000.0, tax_method=TaxMethod.LIFO)
        ts1 = datetime(2024, 1, 1, tzinfo=UTC)
        ts2 = datetime(2024, 2, 1, tzinfo=UTC)
        p.transaction_date = ts1
        p.open_position("AAPL", 10, 100.0)
        p.transaction_date = ts2
        p.open_position("AAPL", 10, 120.0)

        p.transaction_date = datetime(2024, 6, 1, tzinfo=UTC)
        consumed = p.close_position("AAPL", 10, 150.0)
        assert len(consumed) == 1
        assert consumed[0]["purchase_price"] == 120.0


class TestSetTaxMethod:
    def test_change_to_lifo(self):
        p = Portfolio()
        p.set_tax_method(TaxMethod.LIFO)
        assert p.tax_method == TaxMethod.LIFO

    def test_change_to_specific_lot(self):
        p = Portfolio()
        p.set_tax_method(TaxMethod.SPECIFIC_LOT)
        assert p.tax_method == TaxMethod.SPECIFIC_LOT


class TestUpdatePrices:
    def test_updates_current_price(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        p.update_prices({"AAPL": 120.0})
        assert p.positions["AAPL"].current_price == 120.0

    def test_ignores_unknown_symbols(self):
        p = Portfolio(initial_cash=100_000.0)
        p.update_prices({"UNKNOWN": 50.0})

    def test_updates_multiple(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        p.open_position("MSFT", 5, 200.0)
        p.update_prices({"AAPL": 110.0, "MSFT": 210.0})
        assert p.positions["AAPL"].current_price == 110.0
        assert p.positions["MSFT"].current_price == 210.0


class TestSnapshot:
    def test_snapshot_has_correct_cash(self):
        p = Portfolio(initial_cash=100_000.0)
        snap = p.snapshot()
        assert snap.cash == 100_000.0

    def test_snapshot_has_positions(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        snap = p.snapshot()
        assert "AAPL" in snap.positions
        assert snap.positions["AAPL"]["quantity"] == 10

    def test_snapshot_total_value(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        p.update_prices({"AAPL": 120.0})
        snap = p.snapshot()
        assert snap.total_value == pytest.approx(p.total_value)

    def test_snapshot_realized_pnl(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        p.close_position("AAPL", 10, 150.0)
        snap = p.snapshot()
        assert snap.realized_pnl == pytest.approx(500.0)


class TestPortfolioSnapshot:
    def test_allocation_weight(self):
        snap = PortfolioSnapshot(
            cash=50_000.0,
            positions={"AAPL": {"quantity": 100, "avg_cost": 100.0, "current_price": 100.0}},
            total_value=60_000.0,
            total_return_pct=0.0,
            realized_pnl=0.0,
        )
        assert snap.allocation_weight("AAPL") == pytest.approx(100 * 100.0 / 60_000.0 * 100)

    def test_allocation_weight_unknown_symbol(self):
        snap = PortfolioSnapshot(
            cash=100_000.0, positions={}, total_value=100_000.0,
            total_return_pct=0.0, realized_pnl=0.0,
        )
        assert snap.allocation_weight("UNKNOWN") == 0.0

    def test_allocation_weight_zero_total(self):
        snap = PortfolioSnapshot(
            cash=0.0, positions={}, total_value=0.0,
            total_return_pct=0.0, realized_pnl=0.0,
        )
        assert snap.allocation_weight("AAPL") == 0.0

    def test_summary_string(self):
        snap = PortfolioSnapshot(
            cash=100_000.0, positions={}, total_value=100_000.0,
            total_return_pct=5.0, realized_pnl=0.0,
        )
        s = snap.summary()
        assert "$100,000" in s
        assert "5.00%" in s


class TestWashSaleAdjustment:
    def test_wash_sale_adjusts_cost_basis(self):
        p = Portfolio(initial_cash=100_000.0)
        ts_sell = datetime(2024, 3, 1, tzinfo=UTC)
        ts_rebuy = datetime(2024, 3, 15, tzinfo=UTC)

        p.transaction_date = datetime(2024, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 10, 100.0)

        p.transaction_date = ts_sell
        p.close_position("AAPL", 10, 80.0)
        assert p.realized_pnl == pytest.approx(-200.0)

        p.transaction_date = ts_rebuy
        p.open_position("AAPL", 10, 90.0)

        lots = p.get_tax_lots("AAPL")
        assert len(lots) == 1
        assert lots[0].purchase_price > 90.0

    def test_no_adjustment_when_outside_window(self):
        p = Portfolio(initial_cash=100_000.0)
        ts_sell = datetime(2024, 1, 1, tzinfo=UTC)
        ts_rebuy = datetime(2024, 3, 1, tzinfo=UTC)

        p.transaction_date = datetime(2023, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 10, 100.0)

        p.transaction_date = ts_sell
        p.close_position("AAPL", 10, 80.0)

        p.transaction_date = ts_rebuy
        p.open_position("AAPL", 10, 90.0)

        lots = p.get_tax_lots("AAPL")
        assert lots[0].purchase_price == 90.0

    def test_no_adjustment_when_gain_not_loss(self):
        p = Portfolio(initial_cash=100_000.0)
        ts_sell = datetime(2024, 3, 1, tzinfo=UTC)
        ts_rebuy = datetime(2024, 3, 15, tzinfo=UTC)

        p.transaction_date = datetime(2024, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 10, 100.0)

        p.transaction_date = ts_sell
        p.close_position("AAPL", 10, 120.0)

        p.transaction_date = ts_rebuy
        p.open_position("AAPL", 10, 110.0)

        lots = p.get_tax_lots("AAPL")
        assert lots[0].purchase_price == 110.0


class TestTaxLotInsufficient:
    def test_sell_more_than_position_raises(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 5, 100.0)
        with pytest.raises(ValueError, match="Cannot sell"):
            p.close_position("AAPL", 10, 150.0)

    def test_tax_lots_insufficient_when_position_matches_but_lots_dont(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        p._tax_lots["AAPL"][0].quantity = 5
        p.positions["AAPL"].quantity = 10
        with pytest.raises(ValueError, match="Tax lots insufficient"):
            p.close_position("AAPL", 10, 150.0)


class TestMultipleRounds:
    def test_buy_sell_buy_cycle(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        p.close_position("AAPL", 10, 120.0)
        p.open_position("AAPL", 5, 110.0)
        assert p.positions["AAPL"].quantity == 5
        assert p.positions["AAPL"].avg_cost == 110.0
        assert len(p.trade_history) == 3

    def test_accumulated_realized_pnl(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        p.close_position("AAPL", 10, 120.0)
        first_pnl = p.realized_pnl

        p.open_position("MSFT", 5, 200.0)
        p.close_position("MSFT", 5, 250.0)

        assert p.realized_pnl == pytest.approx(first_pnl + 250.0)

    def test_total_value_after_full_cycle(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        p.close_position("AAPL", 10, 120.0)
        assert p.total_value == pytest.approx(100_000.0 + 200.0)

    def test_full_round_trip_preserves_cash_plus_pnl(self):
        p = Portfolio(initial_cash=100_000.0)
        p.open_position("AAPL", 10, 100.0)
        p.close_position("AAPL", 10, 150.0)
        assert p.cash == pytest.approx(100_000.0 + 500.0)
        assert len(p.positions) == 0


class TestPortfolioStateAlias:
    def test_portfolio_state_is_snapshot(self):
        from engine.core.portfolio import PortfolioState
        assert PortfolioState is PortfolioSnapshot


class TestSellRecord:
    def test_fields(self):
        rec = SellRecord(
            symbol="AAPL",
            sell_date=datetime.now(UTC),
            quantity=10,
            sell_price=150.0,
            cost_basis=1000.0,
            gain=500.0,
        )
        assert rec.symbol == "AAPL"
        assert rec.gain == 500.0


class TestTradeRecord:
    def test_fields(self):
        rec = TradeRecord(
            timestamp=datetime.now(UTC),
            side="buy",
            symbol="AAPL",
            quantity=10,
            price=100.0,
            cost=2.0,
            tax=0.0,
            lot_ids=["lot-1"],
        )
        assert rec.side == "buy"
        assert rec.lot_ids == ["lot-1"]

    def test_default_lot_ids(self):
        rec = TradeRecord(
            timestamp=datetime.now(UTC),
            side="buy",
            symbol="AAPL",
            quantity=10,
            price=100.0,
        )
        assert rec.lot_ids == []
