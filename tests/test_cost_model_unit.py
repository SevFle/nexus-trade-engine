"""
Comprehensive unit tests for cost_model.py — Money, CostBreakdown, TaxLot,
and DefaultCostModel edge cases not covered by test_cost_model.py.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from engine.core.cost_model import (
    CostBreakdown,
    DefaultCostModel,
    ICostModel,
    Money,
    TaxLot,
    TaxMethod,
)


class TestMoneyArithmetic:
    def test_add_same_currency(self):
        a = Money(amount=10.0, currency="USD")
        b = Money(amount=5.0, currency="USD")
        result = a + b
        assert result.amount == 15.0
        assert result.currency == "USD"

    def test_sub_same_currency(self):
        a = Money(amount=10.0, currency="USD")
        b = Money(amount=3.0, currency="USD")
        result = a - b
        assert result.amount == 7.0
        assert result.currency == "USD"

    def test_is_zero_with_zero_amount(self):
        m = Money(amount=0.0)
        assert m.is_zero is True

    def test_is_zero_with_tiny_amount(self):
        m = Money(amount=1e-11)
        assert m.is_zero is True

    def test_is_zero_with_significant_amount(self):
        m = Money(amount=0.01)
        assert m.is_zero is False

    def test_is_zero_with_negative(self):
        m = Money(amount=-1e-11)
        assert m.is_zero is True

    def test_as_pct_of_nonzero_total(self):
        m = Money(amount=25.0)
        assert m.as_pct_of(100.0) == 25.0

    def test_as_pct_of_zero_total(self):
        m = Money(amount=50.0)
        assert m.as_pct_of(0.0) == 0.0

    def test_add_mismatched_currency_raises(self):
        a = Money(amount=10.0, currency="USD")
        b = Money(amount=5.0, currency="EUR")
        with pytest.raises(AssertionError):
            a + b

    def test_sub_mismatched_currency_raises(self):
        a = Money(amount=10.0, currency="USD")
        b = Money(amount=5.0, currency="EUR")
        with pytest.raises(AssertionError):
            a - b

    def test_negative_amount(self):
        m = Money(amount=-5.0)
        assert m.is_zero is False
        assert m.amount == -5.0


class TestCostBreakdown:
    def test_total_sums_all_components(self):
        bd = CostBreakdown(
            commission=Money(1.0),
            spread=Money(2.0),
            slippage=Money(3.0),
            exchange_fee=Money(0.5),
            tax_estimate=Money(4.0),
            currency_conversion=Money(1.5),
        )
        assert bd.total.amount == pytest.approx(12.0)

    def test_total_defaults_to_zero(self):
        bd = CostBreakdown()
        assert bd.total.amount == 0.0
        assert bd.total.is_zero is True

    def test_total_without_tax(self):
        bd = CostBreakdown(
            commission=Money(1.0),
            spread=Money(2.0),
            slippage=Money(3.0),
            exchange_fee=Money(0.5),
            tax_estimate=Money(4.0),
            currency_conversion=Money(1.5),
        )
        assert bd.total_without_tax.amount == pytest.approx(8.0)

    def test_total_without_tax_zero_tax(self):
        bd = CostBreakdown(commission=Money(5.0), tax_estimate=Money(0.0))
        assert bd.total_without_tax.amount == pytest.approx(5.0)

    def test_as_dict_keys(self):
        bd = CostBreakdown(commission=Money(1.0))
        d = bd.as_dict()
        expected_keys = {
            "commission", "spread", "slippage", "exchange_fee",
            "tax_estimate", "currency_conversion", "total",
        }
        assert set(d.keys()) == expected_keys

    def test_as_dict_values(self):
        bd = CostBreakdown(
            commission=Money(1.5),
            spread=Money(2.5),
        )
        d = bd.as_dict()
        assert d["commission"] == 1.5
        assert d["spread"] == 2.5
        assert d["total"] == pytest.approx(4.0)


class TestTaxLot:
    def test_cost_basis(self):
        lot = TaxLot(
            symbol="AAPL",
            quantity=100,
            purchase_price=150.0,
            purchase_date=datetime(2025, 1, 1, tzinfo=UTC),
        )
        assert lot.cost_basis == 15_000.0

    def test_is_long_term_exactly_365_days(self):
        purchase = datetime(2025, 1, 1, tzinfo=UTC)
        lot = TaxLot(
            symbol="AAPL",
            quantity=10,
            purchase_price=100.0,
            purchase_date=purchase,
        )
        as_of = purchase + timedelta(days=365)
        assert lot.is_long_term(as_of=as_of) is True

    def test_is_short_term_364_days(self):
        purchase = datetime(2025, 1, 1, tzinfo=UTC)
        lot = TaxLot(
            symbol="AAPL",
            quantity=10,
            purchase_price=100.0,
            purchase_date=purchase,
        )
        as_of = purchase + timedelta(days=364)
        assert lot.is_long_term(as_of=as_of) is False

    def test_is_long_term_uses_current_time_default(self):
        lot = TaxLot(
            symbol="AAPL",
            quantity=10,
            purchase_price=100.0,
            purchase_date=datetime(2000, 1, 1, tzinfo=UTC),
        )
        assert lot.is_long_term() is True

    def test_default_lot_id_empty_string(self):
        lot = TaxLot(
            symbol="AAPL",
            quantity=10,
            purchase_price=100.0,
            purchase_date=datetime(2025, 1, 1, tzinfo=UTC),
        )
        assert lot.lot_id == ""

    def test_custom_lot_id(self):
        lot = TaxLot(
            symbol="AAPL",
            quantity=10,
            purchase_price=100.0,
            purchase_date=datetime(2025, 1, 1, tzinfo=UTC),
            lot_id="custom-123",
        )
        assert lot.lot_id == "custom-123"

    def test_cost_basis_zero_quantity(self):
        lot = TaxLot(
            symbol="AAPL",
            quantity=0,
            purchase_price=100.0,
            purchase_date=datetime(2025, 1, 1, tzinfo=UTC),
        )
        assert lot.cost_basis == 0.0


class TestDefaultCostModelEstimatePct:
    def test_zero_price_returns_spread_slippage_only(self):
        model = DefaultCostModel(commission_per_trade=1.0, spread_bps=5.0, slippage_bps=10.0)
        pct = model.estimate_pct("AAPL", price=0.0)
        expected = (5.0 + 10.0) * 2 / 10_000
        assert pct == pytest.approx(expected)

    def test_positive_price_returns_nonzero(self):
        model = DefaultCostModel(commission_per_trade=1.0, spread_bps=5.0, slippage_bps=10.0)
        pct = model.estimate_pct("AAPL", price=100.0)
        assert pct > 0

    def test_round_trip_is_double_one_side(self):
        model = DefaultCostModel(commission_per_trade=0.0, spread_bps=5.0, slippage_bps=10.0)
        pct = model.estimate_pct("AAPL", price=100.0)
        one_side_bps = 5.0 + 10.0
        expected = (one_side_bps * 2) / 10_000
        assert pct == pytest.approx(expected)

    def test_high_commission_contributes(self):
        model = DefaultCostModel(commission_per_trade=10.0, spread_bps=0.0, slippage_bps=0.0)
        pct = model.estimate_pct("AAPL", price=100.0)
        commission_bps = (10.0 / 100.0) * 10_000
        expected = commission_bps / 10_000
        assert pct == pytest.approx(expected)


class TestDefaultCostModelTaxEdgeCases:
    def test_specific_lot_method_passes_through(self):
        now = datetime.now(UTC)
        lots = [
            TaxLot(
                symbol="AAPL",
                quantity=50,
                purchase_price=80.0,
                purchase_date=now - timedelta(days=30),
            ),
            TaxLot(
                symbol="AAPL",
                quantity=50,
                purchase_price=120.0,
                purchase_date=now - timedelta(days=10),
            ),
        ]
        model = DefaultCostModel(short_term_tax_rate=0.37)
        tax = model.estimate_tax("AAPL", 150.0, 100, lots, TaxMethod.SPECIFIC_LOT, sell_date=now)
        gain = (150.0 - 80.0) * 50 + (150.0 - 120.0) * 50
        expected = gain * 0.37
        assert tax.amount == pytest.approx(expected)

    def test_sell_more_than_lot_quantity_consumes_partial(self):
        now = datetime.now(UTC)
        lots = [
            TaxLot(
                symbol="AAPL",
                quantity=50,
                purchase_price=100.0,
                purchase_date=now - timedelta(days=30),
            ),
        ]
        model = DefaultCostModel(short_term_tax_rate=0.37)
        tax = model.estimate_tax("AAPL", 150.0, 30, lots, TaxMethod.FIFO, sell_date=now)
        expected = (150.0 - 100.0) * 30 * 0.37
        assert tax.amount == pytest.approx(expected)

    def test_no_lots_zero_tax(self):
        model = DefaultCostModel()
        tax = model.estimate_tax("AAPL", 150.0, 100, [], TaxMethod.FIFO)
        assert tax.amount == 0.0

    def test_mixed_long_short_term_lots(self):
        now = datetime.now(UTC)
        lots = [
            TaxLot(
                symbol="AAPL",
                quantity=50,
                purchase_price=100.0,
                purchase_date=now - timedelta(days=400),
            ),
            TaxLot(
                symbol="AAPL",
                quantity=50,
                purchase_price=120.0,
                purchase_date=now - timedelta(days=30),
            ),
        ]
        model = DefaultCostModel(
            short_term_tax_rate=0.37,
            long_term_tax_rate=0.20,
        )
        tax = model.estimate_tax("AAPL", 150.0, 100, lots, TaxMethod.FIFO, sell_date=now)
        lt_gain = (150.0 - 100.0) * 50
        st_gain = (150.0 - 120.0) * 50
        expected = lt_gain * 0.20 + st_gain * 0.37
        assert tax.amount == pytest.approx(expected)


class TestDefaultCostModelCustomParams:
    def test_custom_commission(self):
        model = DefaultCostModel(commission_per_trade=5.0)
        result = model.estimate_commission("AAPL", 100, 150.0)
        assert result.amount == 5.0

    def test_custom_spread_bps(self):
        model = DefaultCostModel(spread_bps=10.0)
        result = model.estimate_spread("AAPL", 100.0, "buy")
        expected = 100.0 * (10.0 / 10_000)
        assert result.amount == pytest.approx(expected)

    def test_custom_exchange_fee(self):
        model = DefaultCostModel(exchange_fee_per_share=0.005)
        bd = model.estimate_total("AAPL", 100, 100.0, "buy")
        assert bd.exchange_fee.amount == pytest.approx(0.5)

    def test_custom_wash_sale_window(self):
        model = DefaultCostModel(wash_sale_window_days=60)
        sell_date = datetime.now(UTC)
        buy_history = [
            {"symbol": "AAPL", "date": sell_date - timedelta(days=45)},
        ]
        assert model.check_wash_sale("AAPL", sell_date, buy_history) is True
        buy_history_far = [
            {"symbol": "AAPL", "date": sell_date - timedelta(days=61)},
        ]
        assert model.check_wash_sale("AAPL", sell_date, buy_history_far) is False

    def test_estimate_total_no_tax_by_default(self):
        model = DefaultCostModel()
        bd = model.estimate_total("AAPL", 100, 150.0, "buy")
        assert bd.tax_estimate.amount == 0.0

    def test_estimate_slippage_no_volume(self):
        model = DefaultCostModel(slippage_bps=10.0)
        result = model.estimate_slippage("AAPL", 100, 150.0, avg_volume=0)
        expected = 150.0 * (10.0 / 10_000) * 100
        assert result.amount == pytest.approx(expected)

    def test_estimate_slippage_with_volume_multiplier(self):
        model = DefaultCostModel(slippage_bps=10.0)
        low_qty = model.estimate_slippage("AAPL", 100, 150.0, avg_volume=1_000_000)
        high_qty = model.estimate_slippage("AAPL", 100_000, 150.0, avg_volume=1_000_000)
        assert high_qty.amount > low_qty.amount


class TestICostModelIsAbstract:
    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            ICostModel()


class TestTaxMethodEnum:
    def test_fifo_value(self):
        assert TaxMethod.FIFO == "fifo"

    def test_lifo_value(self):
        assert TaxMethod.LIFO == "lifo"

    def test_specific_lot_value(self):
        assert TaxMethod.SPECIFIC_LOT == "specific_lot"
