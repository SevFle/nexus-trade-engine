"""
Tests for the cost model — the most critical component to get right.
"""

from datetime import UTC, datetime, timedelta

import pytest

from engine.core.cost_model import DefaultCostModel, TaxLot, TaxMethod


@pytest.fixture
def cost_model():
    return DefaultCostModel(
        commission_per_trade=1.0,
        spread_bps=5.0,
        slippage_bps=10.0,
        exchange_fee_per_share=0.0003,
        short_term_tax_rate=0.37,
        long_term_tax_rate=0.20,
    )


class TestCommission:
    def test_flat_commission(self, cost_model):
        result = cost_model.estimate_commission("AAPL", 100, 150.0)
        assert result.amount == 1.0

    def test_zero_commission(self):
        model = DefaultCostModel(commission_per_trade=0.0)
        result = model.estimate_commission("AAPL", 100, 150.0)
        assert result.amount == 0.0


class TestSpread:
    def test_spread_calculation(self, cost_model):
        result = cost_model.estimate_spread("AAPL", 150.0, "buy")
        expected = 150.0 * (5.0 / 10_000)  # 5 bps
        assert abs(result.amount - expected) < 1e-10

    def test_spread_scales_with_price(self, cost_model):
        cheap = cost_model.estimate_spread("X", 10.0, "buy")
        expensive = cost_model.estimate_spread("Y", 1000.0, "buy")
        assert expensive.amount > cheap.amount


class TestSlippage:
    def test_base_slippage(self, cost_model):
        result = cost_model.estimate_slippage("AAPL", 100, 150.0, avg_volume=0)
        expected = 150.0 * (10.0 / 10_000) * 100
        assert abs(result.amount - expected) < 1e-6

    def test_slippage_increases_with_participation(self, cost_model):
        low_impact = cost_model.estimate_slippage("AAPL", 100, 150.0, avg_volume=1_000_000)
        high_impact = cost_model.estimate_slippage("AAPL", 100_000, 150.0, avg_volume=1_000_000)
        assert high_impact.amount > low_impact.amount


class TestTotalCost:
    def test_total_cost_breakdown(self, cost_model):
        breakdown = cost_model.estimate_total("AAPL", 100, 150.0, "buy", avg_volume=1_000_000)
        assert breakdown.commission.amount == 1.0
        assert breakdown.spread.amount > 0
        assert breakdown.slippage.amount > 0
        assert breakdown.exchange_fee.amount == 0.0003 * 100
        assert breakdown.total.amount > 0

    def test_cost_as_percentage(self, cost_model):
        pct = cost_model.estimate_pct("AAPL", 150.0, "buy")
        assert 0 < pct < 0.01  # Should be less than 1%


class TestTaxEngine:
    def test_short_term_gains_tax(self, cost_model):
        sell_date = datetime(2026, 4, 16, tzinfo=UTC)
        lots = [
            TaxLot(
                symbol="AAPL",
                quantity=100,
                purchase_price=100.0,
                purchase_date=sell_date - timedelta(days=30),
            )
        ]
        tax = cost_model.estimate_tax(
            "AAPL", 150.0, 100, lots, TaxMethod.FIFO, sell_date=sell_date
        )
        expected = (150.0 - 100.0) * 100 * 0.37  # Short-term rate
        assert abs(tax.amount - expected) < 1e-6

    def test_long_term_gains_tax(self, cost_model):
        sell_date = datetime(2026, 4, 16, tzinfo=UTC)
        lots = [
            TaxLot(
                symbol="AAPL",
                quantity=100,
                purchase_price=100.0,
                purchase_date=sell_date - timedelta(days=400),
            )
        ]
        tax = cost_model.estimate_tax(
            "AAPL", 150.0, 100, lots, TaxMethod.FIFO, sell_date=sell_date
        )
        expected = (150.0 - 100.0) * 100 * 0.20  # Long-term rate
        assert abs(tax.amount - expected) < 1e-6

    def test_no_tax_on_loss(self, cost_model):
        sell_date = datetime.now(UTC)
        lots = [
            TaxLot(
                symbol="AAPL",
                quantity=100,
                purchase_price=200.0,
                purchase_date=sell_date - timedelta(days=30),
            )
        ]
        tax = cost_model.estimate_tax(
            "AAPL", 150.0, 100, lots, TaxMethod.FIFO, sell_date=sell_date
        )
        assert tax.amount == 0.0

    def test_fifo_vs_lifo(self, cost_model):
        now = datetime.now(UTC)
        lots = [
            TaxLot(
                symbol="AAPL",
                quantity=50,
                purchase_price=80.0,
                purchase_date=now - timedelta(days=400),
            ),
            TaxLot(
                symbol="AAPL",
                quantity=50,
                purchase_price=140.0,
                purchase_date=now - timedelta(days=30),
            ),
        ]
        fifo_tax = cost_model.estimate_tax("AAPL", 150.0, 50, lots, TaxMethod.FIFO, sell_date=now)
        lifo_tax = cost_model.estimate_tax("AAPL", 150.0, 50, lots, TaxMethod.LIFO, sell_date=now)
        assert fifo_tax.amount != lifo_tax.amount


class TestWashSale:
    def test_wash_sale_detected(self, cost_model):
        sell_date = datetime.now(UTC)
        buy_history = [
            {"symbol": "AAPL", "date": sell_date - timedelta(days=10)},
        ]
        assert cost_model.check_wash_sale("AAPL", sell_date, buy_history) is True

    def test_no_wash_sale_outside_window(self, cost_model):
        sell_date = datetime.now(UTC)
        buy_history = [
            {"symbol": "AAPL", "date": sell_date - timedelta(days=60)},
        ]
        assert cost_model.check_wash_sale("AAPL", sell_date, buy_history) is False

    def test_no_wash_sale_different_symbol(self, cost_model):
        sell_date = datetime.now(UTC)
        buy_history = [
            {"symbol": "MSFT", "date": sell_date - timedelta(days=10)},
        ]
        assert cost_model.check_wash_sale("AAPL", sell_date, buy_history) is False

    def test_wash_sale_adjustment_calculates_disallowed_loss(self, cost_model):
        sell_date = datetime.now(UTC)
        loss = -500.0
        buy_history = [
            {
                "symbol": "AAPL",
                "date": sell_date - timedelta(days=10),
                "price": 145.0,
                "quantity": 100,
            },
        ]
        result = cost_model.calculate_wash_sale_adjustment("AAPL", sell_date, loss, buy_history)
        assert result["is_wash_sale"] is True
        assert result["adjustment"] == 500.0

    def test_wash_sale_adjustment_no_loss(self, cost_model):
        sell_date = datetime.now(UTC)
        gain = 500.0
        buy_history = [
            {
                "symbol": "AAPL",
                "date": sell_date - timedelta(days=10),
                "price": 145.0,
                "quantity": 100,
            },
        ]
        result = cost_model.calculate_wash_sale_adjustment("AAPL", sell_date, gain, buy_history)
        assert result["is_wash_sale"] is False
        assert result["replacement_lots"] == []

    def test_wash_sale_adjustment_per_share(self, cost_model):
        sell_date = datetime.now(UTC)
        loss = -1000.0
        buy_history = [
            {
                "symbol": "AAPL",
                "date": sell_date - timedelta(days=10),
                "price": 145.0,
                "quantity": 100,
            },
        ]
        result = cost_model.calculate_wash_sale_adjustment("AAPL", sell_date, loss, buy_history)
        assert result["is_wash_sale"] is True
        assert result["adjustment_per_share"] == 10.0

    def test_wash_sale_30_day_window_after_sale(self, cost_model):
        sell_date = datetime.now(UTC)
        loss = -500.0
        buy_history = [
            {
                "symbol": "AAPL",
                "date": sell_date + timedelta(days=10),
                "price": 145.0,
                "quantity": 100,
            },
        ]
        result = cost_model.calculate_wash_sale_adjustment("AAPL", sell_date, loss, buy_history)
        assert result["is_wash_sale"] is True


class TestDividendTax:
    def test_qualified_dividend(self, cost_model):
        tax = cost_model.estimate_dividend_tax(1000.0, is_qualified=True)
        assert tax.amount == 1000.0 * 0.15

    def test_ordinary_dividend(self, cost_model):
        tax = cost_model.estimate_dividend_tax(1000.0, is_qualified=False)
        assert tax.amount == 1000.0 * 0.37
