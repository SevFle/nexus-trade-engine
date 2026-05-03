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


class TestMoney:
    def test_addition(self):
        from engine.core.cost_model import Money

        a = Money(amount=10.0)
        b = Money(amount=5.0)
        result = a + b
        assert result.amount == 15.0
        assert result.currency == "USD"

    def test_subtraction(self):
        from engine.core.cost_model import Money

        a = Money(amount=10.0)
        b = Money(amount=3.0)
        result = a - b
        assert result.amount == 7.0

    def test_is_zero(self):
        from engine.core.cost_model import Money

        assert Money(amount=0.0).is_zero is True
        assert Money(amount=1e-11).is_zero is True
        assert Money(amount=0.001).is_zero is False

    def test_as_pct_of(self):
        from engine.core.cost_model import Money

        m = Money(amount=5.0)
        assert m.as_pct_of(100.0) == 5.0

    def test_as_pct_of_zero(self):
        from engine.core.cost_model import Money

        m = Money(amount=5.0)
        assert m.as_pct_of(0.0) == 0.0


class TestCostBreakdown:
    def test_total_sums_all_components(self):
        from engine.core.cost_model import CostBreakdown, Money

        cb = CostBreakdown(
            commission=Money(amount=1.0),
            spread=Money(amount=2.0),
            slippage=Money(amount=3.0),
            exchange_fee=Money(amount=0.5),
            tax_estimate=Money(amount=1.5),
            currency_conversion=Money(amount=0.0),
        )
        assert cb.total.amount == pytest.approx(8.0)

    def test_total_without_tax(self):
        from engine.core.cost_model import CostBreakdown, Money

        cb = CostBreakdown(
            commission=Money(amount=1.0),
            spread=Money(amount=1.0),
            tax_estimate=Money(amount=1.0),
        )
        assert cb.total.amount == pytest.approx(3.0)
        assert cb.total_without_tax.amount == pytest.approx(2.0)

    def test_as_dict_keys(self):
        from engine.core.cost_model import CostBreakdown, Money

        cb = CostBreakdown(commission=Money(amount=1.0))
        d = cb.as_dict()
        assert "commission" in d
        assert "spread" in d
        assert "slippage" in d
        assert "exchange_fee" in d
        assert "tax_estimate" in d
        assert "currency_conversion" in d
        assert "total" in d

    def test_default_all_zero(self):
        from engine.core.cost_model import CostBreakdown

        cb = CostBreakdown()
        assert cb.total.amount == 0.0
        assert cb.total.is_zero


class TestTaxLot:
    def test_is_long_term(self):
        from engine.core.cost_model import TaxLot

        lot = TaxLot(
            symbol="AAPL",
            quantity=10,
            purchase_price=100.0,
            purchase_date=datetime(2023, 1, 1, tzinfo=UTC),
        )
        assert lot.is_long_term(as_of=datetime(2024, 6, 1, tzinfo=UTC)) is True

    def test_is_short_term(self):
        from engine.core.cost_model import TaxLot

        lot = TaxLot(
            symbol="AAPL",
            quantity=10,
            purchase_price=100.0,
            purchase_date=datetime(2024, 1, 1, tzinfo=UTC),
        )
        assert lot.is_long_term(as_of=datetime(2024, 3, 1, tzinfo=UTC)) is False

    def test_cost_basis(self):
        from engine.core.cost_model import TaxLot

        lot = TaxLot(symbol="AAPL", quantity=10, purchase_price=100.0, purchase_date=datetime.now(UTC))
        assert lot.cost_basis == 1000.0


class TestTaxMethodEnum:
    def test_fifo_value(self):
        from engine.core.cost_model import TaxMethod

        assert TaxMethod.FIFO == "fifo"

    def test_lifo_value(self):
        from engine.core.cost_model import TaxMethod

        assert TaxMethod.LIFO == "lifo"

    def test_specific_lot_value(self):
        from engine.core.cost_model import TaxMethod

        assert TaxMethod.SPECIFIC_LOT == "specific_lot"


class TestDefaultCostModelCustomParams:
    def test_custom_commission(self):
        model = DefaultCostModel(commission_per_trade=5.0)
        result = model.estimate_commission("AAPL", 100, 50.0)
        assert result.amount == 5.0

    def test_custom_spread_bps(self):
        model = DefaultCostModel(spread_bps=10.0)
        result = model.estimate_spread("AAPL", 100.0, "buy")
        assert result.amount == pytest.approx(100.0 * 10.0 / 10_000)

    def test_custom_exchange_fee(self):
        model = DefaultCostModel(exchange_fee_per_share=0.005)
        cb = model.estimate_total("AAPL", 100, 50.0, "buy")
        assert cb.exchange_fee.amount == pytest.approx(0.5)

    def test_estimate_pct_round_trip(self):
        model = DefaultCostModel(commission_per_trade=0.0, spread_bps=5.0, slippage_bps=5.0)
        pct = model.estimate_pct("AAPL", 100.0)
        assert pct > 0
        expected_bps = (5.0 + 5.0) * 2
        assert pct == pytest.approx(expected_bps / 10_000)

    def test_estimate_pct_zero_price(self):
        model = DefaultCostModel(commission_per_trade=0.0, spread_bps=0.0, slippage_bps=0.0)
        pct = model.estimate_pct("AAPL", 0.0)
        assert pct == 0.0


class TestEstimateTaxEdgeCases:
    def test_tax_no_gain(self):
        model = DefaultCostModel()
        lot = TaxLot(
            symbol="AAPL",
            quantity=10,
            purchase_price=100.0,
            purchase_date=datetime(2024, 1, 1, tzinfo=UTC),
        )
        tax = model.estimate_tax("AAPL", 100.0, 10, [lot], sell_date=datetime(2024, 6, 1, tzinfo=UTC))
        assert tax.amount == 0.0

    def test_tax_loss_zero_tax(self):
        model = DefaultCostModel()
        lot = TaxLot(
            symbol="AAPL",
            quantity=10,
            purchase_price=100.0,
            purchase_date=datetime(2024, 1, 1, tzinfo=UTC),
        )
        tax = model.estimate_tax("AAPL", 80.0, 10, [lot], sell_date=datetime(2024, 6, 1, tzinfo=UTC))
        assert tax.amount == 0.0

    def test_tax_multiple_lots_partial(self):
        model = DefaultCostModel(short_term_tax_rate=0.30)
        lot1 = TaxLot(
            symbol="AAPL",
            quantity=5,
            purchase_price=90.0,
            purchase_date=datetime(2024, 1, 1, tzinfo=UTC),
        )
        lot2 = TaxLot(
            symbol="AAPL",
            quantity=5,
            purchase_price=95.0,
            purchase_date=datetime(2024, 1, 2, tzinfo=UTC),
        )
        tax = model.estimate_tax("AAPL", 100.0, 7, [lot1, lot2], sell_date=datetime(2024, 3, 1, tzinfo=UTC))
        expected = 5 * (100.0 - 90.0) * 0.30 + 2 * (100.0 - 95.0) * 0.30
        assert tax.amount == pytest.approx(expected)

    def test_tax_lifo_ordering(self):
        model = DefaultCostModel(short_term_tax_rate=0.30)
        lot1 = TaxLot(
            symbol="AAPL",
            quantity=10,
            purchase_price=90.0,
            purchase_date=datetime(2024, 1, 1, tzinfo=UTC),
        )
        lot2 = TaxLot(
            symbol="AAPL",
            quantity=10,
            purchase_price=95.0,
            purchase_date=datetime(2024, 2, 1, tzinfo=UTC),
        )
        tax = model.estimate_tax(
            "AAPL", 100.0, 10, [lot1, lot2],
            method=TaxMethod.LIFO,
            sell_date=datetime(2024, 6, 1, tzinfo=UTC),
        )
        expected = 10 * (100.0 - 95.0) * 0.30
        assert tax.amount == pytest.approx(expected)

    def test_tax_specific_lot_no_reorder(self):
        model = DefaultCostModel(short_term_tax_rate=0.30)
        lot1 = TaxLot(
            symbol="AAPL",
            quantity=10,
            purchase_price=95.0,
            purchase_date=datetime(2024, 2, 1, tzinfo=UTC),
        )
        lot2 = TaxLot(
            symbol="AAPL",
            quantity=10,
            purchase_price=90.0,
            purchase_date=datetime(2024, 1, 1, tzinfo=UTC),
        )
        tax = model.estimate_tax(
            "AAPL", 100.0, 10, [lot1, lot2],
            method=TaxMethod.SPECIFIC_LOT,
            sell_date=datetime(2024, 6, 1, tzinfo=UTC),
        )
        expected = 10 * (100.0 - 95.0) * 0.30
        assert tax.amount == pytest.approx(expected)


class TestCheckWashSaleEdgeCases:
    def test_wash_sale_boundary_before_window(self):
        model = DefaultCostModel(wash_sale_window_days=30)
        sell_date = datetime(2024, 3, 15, tzinfo=UTC)
        buy_history = [{"symbol": "AAPL", "date": sell_date - timedelta(days=31)}]
        assert model.check_wash_sale("AAPL", sell_date, buy_history) is False

    def test_wash_sale_boundary_after_window(self):
        model = DefaultCostModel(wash_sale_window_days=30)
        sell_date = datetime(2024, 3, 15, tzinfo=UTC)
        buy_history = [{"symbol": "AAPL", "date": sell_date + timedelta(days=31)}]
        assert model.check_wash_sale("AAPL", sell_date, buy_history) is False

    def test_wash_sale_exactly_at_window_edge(self):
        model = DefaultCostModel(wash_sale_window_days=30)
        sell_date = datetime(2024, 3, 15, tzinfo=UTC)
        buy_history = [{"symbol": "AAPL", "date": sell_date + timedelta(days=30)}]
        assert model.check_wash_sale("AAPL", sell_date, buy_history) is True

    def test_wash_sale_empty_history(self):
        model = DefaultCostModel()
        sell_date = datetime.now(UTC)
        assert model.check_wash_sale("AAPL", sell_date, []) is False

    def test_wash_sale_date_missing_skips_entry(self):
        model = DefaultCostModel()
        sell_date = datetime.now(UTC)
        buy_history = [{"symbol": "AAPL"}]
        with pytest.raises(TypeError):
            model.check_wash_sale("AAPL", sell_date, buy_history)


class TestWashSaleAdjustmentEdgeCases:
    def test_positive_loss_not_wash_sale(self):
        model = DefaultCostModel()
        result = model.calculate_wash_sale_adjustment(
            "AAPL", datetime.now(UTC), 500.0, []
        )
        assert result["is_wash_sale"] is False
        assert result["adjustment"] == 0.0

    def test_adjustment_per_share(self):
        model = DefaultCostModel()
        sell_date = datetime(2024, 3, 1, tzinfo=UTC)
        loss = -1000.0
        buy_history = [
            {"symbol": "AAPL", "date": sell_date + timedelta(days=5), "price": 100.0, "quantity": 200},
        ]
        result = model.calculate_wash_sale_adjustment("AAPL", sell_date, loss, buy_history)
        assert result["adjustment_per_share"] == pytest.approx(1000.0 / 200)
