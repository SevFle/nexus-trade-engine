"""
Tests for tax lot lifecycle tracking.
"""

from datetime import UTC, datetime, timedelta

import pytest
from core.cost_model import DefaultCostModel, TaxLot, TaxMethod
from core.portfolio import Portfolio


@pytest.fixture
def cost_model():
    return DefaultCostModel(
        short_term_tax_rate=0.37,
        long_term_tax_rate=0.20,
    )


class TestTaxLotLifecycle:
    def test_buy_creates_tax_lot(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 100, 150.0, cost=5.0)
        assert "AAPL" in p.positions
        pos = p.positions["AAPL"]
        assert pos.quantity == 100
        assert len(pos.tax_lots) == 1
        lot = pos.tax_lots[0]
        assert lot.lot_id != ""
        assert lot.symbol == "AAPL"
        assert lot.quantity == 100

    def test_buy_100_sell_50_partial_lot(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 100, 150.0, cost=5.0)
        result = p.close_position("AAPL", 50, 170.0, cost=5.0)
        assert "AAPL" in p.positions
        assert p.positions["AAPL"].quantity == 50
        pos = p.positions["AAPL"]
        assert len(pos.tax_lots) == 1
        assert pos.tax_lots[0].quantity == 50

    def test_buy_two_lots_sell_complete_first(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 100, 100.0)
        p.open_position("AAPL", 100, 200.0)
        result = p.close_position("AAPL", 100, 150.0, tax_method=TaxMethod.FIFO)
        pos = p.positions["AAPL"]
        assert pos.quantity == 100
        assert len(pos.tax_lots) == 1
        assert pos.tax_lots[0].quantity == 100


class TestFIFO:
    def test_fifo_sells_oldest_first(self):
        p = Portfolio(initial_cash=100_000)
        now = datetime.now(UTC)
        p.open_position("AAPL", 50, 80.0)
        p.open_position("AAPL", 50, 140.0)
        result = p.close_position("AAPL", 50, 150.0, tax_method=TaxMethod.FIFO)
        gains = result["realized_gains"]
        assert len(gains) == 1
        assert gains[0]["purchase_price"] == 80.0


class TestLIFO:
    def test_lifo_sells_newest_first(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 50, 80.0)
        p.open_position("AAPL", 50, 140.0)
        result = p.close_position("AAPL", 50, 150.0, tax_method=TaxMethod.LIFO)
        gains = result["realized_gains"]
        assert len(gains) == 1
        assert gains[0]["purchase_price"] == 140.0


class TestShortTermLoss:
    def test_short_term_loss_no_tax(self, cost_model):
        lots = [
            TaxLot(
                symbol="AAPL",
                quantity=100,
                purchase_price=200.0,
                purchase_date=datetime.now(UTC) - timedelta(days=30),
            )
        ]
        tax = cost_model.estimate_tax("AAPL", 150.0, 100, lots, TaxMethod.FIFO)
        assert tax.amount == 0.0


class TestLongTermGain:
    def test_long_term_gain_20_percent(self, cost_model):
        lots = [
            TaxLot(
                symbol="AAPL",
                quantity=100,
                purchase_price=100.0,
                purchase_date=datetime.now(UTC) - timedelta(days=400),
            )
        ]
        tax = cost_model.estimate_tax("AAPL", 200.0, 100, lots, TaxMethod.FIFO)
        expected = (200.0 - 100.0) * 100 * 0.20
        assert abs(tax.amount - expected) < 1e-6


class TestWashSale:
    def test_wash_sale_adjusts_loss(self, cost_model):
        sell_date = datetime.now(UTC)
        buy_history = [
            {"symbol": "AAPL", "date": sell_date - timedelta(days=10)},
        ]
        loss = -1000.0
        adjustment = cost_model.calculate_wash_sale_adjustment(
            "AAPL", sell_date, loss, buy_history
        )
        assert adjustment == 1000.0

    def test_no_wash_sale_for_gain(self, cost_model):
        sell_date = datetime.now(UTC)
        buy_history = [
            {"symbol": "AAPL", "date": sell_date - timedelta(days=10)},
        ]
        gain = 1000.0
        adjustment = cost_model.calculate_wash_sale_adjustment(
            "AAPL", sell_date, gain, buy_history
        )
        assert adjustment == 0.0


class TestSpecificLot:
    def test_specific_lot_requires_metadata(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 100, 100.0)
        with pytest.raises(ValueError, match="lot_id"):
            p.close_position("AAPL", 50, 150.0, tax_method=TaxMethod.SPECIFIC_LOT)

    def test_specific_lot_not_found_raises(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 100, 100.0)
        with pytest.raises(ValueError, match="not found"):
            p.close_position(
                "AAPL",
                50,
                150.0,
                tax_method=TaxMethod.SPECIFIC_LOT,
                metadata={"lot_id": "nonexistent"},
            )
