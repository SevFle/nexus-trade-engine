"""
Comprehensive tests for CostModel targeting uncovered code paths.

Covers: CostBreakdown.total_without_tax, Money currency assertions, edge cases in
tax calculations (multi-lot, partial consumption), boundary wash sale checks,
estimate_pct with zero price, DefaultCostModel no-arg constructor, and more.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from engine.core.cost_model import (
    CostBreakdown,
    DefaultCostModel,
    ICostModel,
    Money,
    TaxLot,
    TaxMethod,
)

# ── Money edge cases ──


class TestMoneyEdgeCases:
    def test_subtraction_result(self):
        a = Money(amount=10.0)
        b = Money(amount=3.5)
        result = a - b
        assert result.amount == 6.5

    def test_negative_amount(self):
        m = Money(amount=-5.0)
        assert m.is_zero is False

    def test_near_zero_threshold(self):
        m = Money(amount=1e-11)
        assert m.is_zero is True

    def test_just_above_zero_threshold(self):
        m = Money(amount=1e-9)
        assert m.is_zero is False

    def test_as_pct_of_total(self):
        m = Money(amount=25.0)
        assert m.as_pct_of(200.0) == 12.5

    def test_as_pct_of_negative_total(self):
        m = Money(amount=10.0)
        result = m.as_pct_of(-100.0)
        assert result == -10.0

    def test_add_preserves_currency(self):
        a = Money(amount=10.0, currency="EUR")
        b = Money(amount=5.0, currency="EUR")
        result = a + b
        assert result.currency == "EUR"

    def test_sub_preserves_currency(self):
        a = Money(amount=10.0, currency="EUR")
        b = Money(amount=3.0, currency="EUR")
        result = a - b
        assert result.currency == "EUR"

    def test_default_currency_is_usd(self):
        m = Money(amount=100.0)
        assert m.currency == "USD"


# ── CostBreakdown edge cases ──


class TestCostBreakdownEdgeCases:
    def test_total_without_tax(self):
        cb = CostBreakdown(
            commission=Money(amount=1.0),
            spread=Money(amount=0.5),
            slippage=Money(amount=0.3),
            exchange_fee=Money(amount=0.1),
            tax_estimate=Money(amount=0.2),
        )
        total = cb.total.amount
        non_tax = cb.total_without_tax.amount
        assert non_tax == total - 0.2
        assert non_tax == pytest.approx(1.0 + 0.5 + 0.3 + 0.1)

    def test_total_without_tax_zero_tax(self):
        cb = CostBreakdown(
            commission=Money(amount=5.0),
            tax_estimate=Money(amount=0.0),
        )
        assert cb.total_without_tax.amount == cb.total.amount

    def test_total_with_currency_conversion(self):
        cb = CostBreakdown(
            commission=Money(amount=1.0),
            currency_conversion=Money(amount=2.5),
        )
        assert cb.total.amount == 3.5

    def test_all_zero_costs(self):
        cb = CostBreakdown()
        assert cb.total.amount == 0.0
        assert cb.total.is_zero is True
        assert cb.total_without_tax.amount == 0.0

    def test_as_dict_keys(self):
        cb = CostBreakdown(
            commission=Money(amount=1.0),
            spread=Money(amount=0.5),
        )
        d = cb.as_dict()
        assert set(d.keys()) == {
            "commission", "spread", "slippage", "exchange_fee",
            "tax_estimate", "currency_conversion", "total",
        }

    def test_as_dict_values(self):
        cb = CostBreakdown(
            commission=Money(amount=1.5),
            spread=Money(amount=0.75),
            slippage=Money(amount=0.25),
            exchange_fee=Money(amount=0.03),
            tax_estimate=Money(amount=0.0),
            currency_conversion=Money(amount=0.0),
        )
        d = cb.as_dict()
        assert d["commission"] == 1.5
        assert d["spread"] == 0.75
        assert d["slippage"] == 0.25
        assert d["exchange_fee"] == 0.03


# ── DefaultCostModel constructor ──


class TestDefaultCostModelConstructor:
    def test_default_parameters(self):
        model = DefaultCostModel()
        assert model.commission_per_trade == 0.0
        assert model.spread_bps == 5.0
        assert model.slippage_bps == 10.0
        assert model.exchange_fee_per_share == 0.0003
        assert model.short_term_tax_rate == 0.37
        assert model.long_term_tax_rate == 0.20
        assert model.qualified_dividend_rate == 0.15
        assert model.ordinary_dividend_rate == 0.37
        assert model.wash_sale_window_days == 30

    def test_custom_parameters(self):
        model = DefaultCostModel(
            commission_per_trade=5.0,
            spread_bps=3.0,
            slippage_bps=7.0,
            exchange_fee_per_share=0.005,
            short_term_tax_rate=0.40,
            long_term_tax_rate=0.15,
            qualified_dividend_rate=0.10,
            ordinary_dividend_rate=0.40,
            wash_sale_window_days=61,
        )
        assert model.commission_per_trade == 5.0
        assert model.wash_sale_window_days == 61


# ── Spread and slippage edge cases ──


class TestSpreadEdgeCases:
    def test_spread_zero_bps(self):
        model = DefaultCostModel(spread_bps=0.0)
        result = model.estimate_spread("AAPL", 100.0, "buy")
        assert result.amount == 0.0

    def test_spread_high_price(self):
        model = DefaultCostModel(spread_bps=5.0)
        result = model.estimate_spread("BRK.A", 500_000.0, "buy")
        expected = 500_000.0 * (5.0 / 10_000)
        assert result.amount == pytest.approx(expected)

    def test_spread_side_does_not_affect_result(self):
        model = DefaultCostModel(spread_bps=5.0)
        buy_spread = model.estimate_spread("AAPL", 100.0, "buy")
        sell_spread = model.estimate_spread("AAPL", 100.0, "sell")
        assert buy_spread.amount == sell_spread.amount


class TestSlippageEdgeCases:
    def test_slippage_zero_quantity(self):
        model = DefaultCostModel(slippage_bps=10.0)
        result = model.estimate_slippage("AAPL", 0, 100.0, avg_volume=0)
        assert result.amount == 0.0

    def test_slippage_zero_price(self):
        model = DefaultCostModel(slippage_bps=10.0)
        result = model.estimate_slippage("AAPL", 100, 0.0, avg_volume=0)
        assert result.amount == 0.0

    def test_slippage_with_volume_multiplier(self):
        model = DefaultCostModel(slippage_bps=10.0)
        # 100 shares at $100 with avg_volume 1000
        result = model.estimate_slippage("AAPL", 100, 100.0, avg_volume=1000)
        base = 100.0 * (10.0 / 10_000) * 100
        participation = 100 / 1000
        multiplier = 1.0 + participation * 10
        expected = base * multiplier
        assert result.amount == pytest.approx(expected)

    def test_slippage_small_order_vs_volume(self):
        model = DefaultCostModel(slippage_bps=10.0)
        small = model.estimate_slippage("AAPL", 10, 100.0, avg_volume=10_000_000)
        base = model.estimate_slippage("AAPL", 10, 100.0, avg_volume=0)
        assert small.amount > base.amount


# ── estimate_pct edge cases ──


class TestEstimatePct:
    def test_zero_price_commission_only_returns_zero(self):
        model = DefaultCostModel(commission_per_trade=1.0, spread_bps=0.0, slippage_bps=0.0)
        result = model.estimate_pct("AAPL", 0.0, "buy")
        assert result == 0.0

    def test_estimate_pct_positive_price(self):
        model = DefaultCostModel(commission_per_trade=0.0, spread_bps=5.0, slippage_bps=5.0)
        result = model.estimate_pct("AAPL", 100.0, "buy")
        one_side_bps = 5.0 + 5.0
        round_trip_bps = one_side_bps * 2
        expected = round_trip_bps / 10_000
        assert result == pytest.approx(expected)

    def test_estimate_pct_with_commission(self):
        model = DefaultCostModel(commission_per_trade=1.0, spread_bps=0.0, slippage_bps=0.0)
        result = model.estimate_pct("AAPL", 100.0, "buy")
        commission_bps = (1.0 / 100.0) * 10_000
        expected = commission_bps / 10_000
        assert result == pytest.approx(expected)


# ── Tax: multi-lot, partial consumption ──


class TestTaxMultiLot:
    def test_partial_consumption_from_first_lot(self):
        sell_date = datetime(2026, 1, 1, tzinfo=UTC)
        model = DefaultCostModel(short_term_tax_rate=0.37)
        lots = [
            TaxLot(symbol="AAPL", quantity=100, purchase_price=100.0,
                   purchase_date=sell_date - timedelta(days=30)),
        ]
        tax = model.estimate_tax("AAPL", 150.0, 50, lots, TaxMethod.FIFO, sell_date=sell_date)
        expected = (150.0 - 100.0) * 50 * 0.37
        assert tax.amount == pytest.approx(expected)

    def test_consume_from_multiple_lots(self):
        sell_date = datetime(2026, 1, 1, tzinfo=UTC)
        model = DefaultCostModel(
            short_term_tax_rate=0.37,
            long_term_tax_rate=0.20,
        )
        lots = [
            TaxLot(symbol="AAPL", quantity=50, purchase_price=100.0,
                   purchase_date=sell_date - timedelta(days=400)),
            TaxLot(symbol="AAPL", quantity=50, purchase_price=120.0,
                   purchase_date=sell_date - timedelta(days=30)),
        ]
        tax = model.estimate_tax("AAPL", 150.0, 100, lots, TaxMethod.FIFO, sell_date=sell_date)
        long_term_gain = (150.0 - 100.0) * 50
        short_term_gain = (150.0 - 120.0) * 50
        expected = long_term_gain * 0.20 + short_term_gain * 0.37
        assert tax.amount == pytest.approx(expected)

    def test_lifo_consumes_newest_first(self):
        sell_date = datetime(2026, 1, 1, tzinfo=UTC)
        model = DefaultCostModel(short_term_tax_rate=0.37, long_term_tax_rate=0.20)
        lots = [
            TaxLot(symbol="AAPL", quantity=50, purchase_price=80.0,
                   purchase_date=sell_date - timedelta(days=400)),
            TaxLot(symbol="AAPL", quantity=50, purchase_price=140.0,
                   purchase_date=sell_date - timedelta(days=30)),
        ]
        tax = model.estimate_tax("AAPL", 150.0, 50, lots, TaxMethod.LIFO, sell_date=sell_date)
        expected = (150.0 - 140.0) * 50 * 0.37
        assert tax.amount == pytest.approx(expected)

    def test_tax_with_no_gain_returns_zero(self):
        sell_date = datetime(2026, 1, 1, tzinfo=UTC)
        model = DefaultCostModel()
        lots = [
            TaxLot(symbol="AAPL", quantity=100, purchase_price=200.0,
                   purchase_date=sell_date - timedelta(days=30)),
        ]
        tax = model.estimate_tax("AAPL", 150.0, 100, lots, TaxMethod.FIFO, sell_date=sell_date)
        assert tax.amount == 0.0

    def test_tax_quantity_less_than_single_lot(self):
        sell_date = datetime(2026, 1, 1, tzinfo=UTC)
        model = DefaultCostModel(short_term_tax_rate=0.37)
        lots = [
            TaxLot(symbol="AAPL", quantity=1000, purchase_price=100.0,
                   purchase_date=sell_date - timedelta(days=10)),
        ]
        tax = model.estimate_tax("AAPL", 110.0, 1, lots, TaxMethod.FIFO, sell_date=sell_date)
        expected = (110.0 - 100.0) * 1 * 0.37
        assert tax.amount == pytest.approx(expected)


# ── TaxLot edge cases ──


class TestTaxLotEdgeCases:
    def test_is_long_term_exactly_365_days(self):
        now = datetime(2026, 1, 1, tzinfo=UTC)
        lot = TaxLot(
            symbol="AAPL", quantity=100, purchase_price=100.0,
            purchase_date=now - timedelta(days=365),
        )
        assert lot.is_long_term(as_of=now) is True

    def test_is_long_term_364_days(self):
        now = datetime(2026, 1, 1, tzinfo=UTC)
        lot = TaxLot(
            symbol="AAPL", quantity=100, purchase_price=100.0,
            purchase_date=now - timedelta(days=364),
        )
        assert lot.is_long_term(as_of=now) is False

    def test_is_long_term_default_as_of(self):
        lot = TaxLot(
            symbol="AAPL", quantity=100, purchase_price=100.0,
            purchase_date=datetime.now(UTC) - timedelta(days=400),
        )
        assert lot.is_long_term() is True

    def test_cost_basis_with_large_numbers(self):
        lot = TaxLot(
            symbol="AAPL", quantity=1_000_000, purchase_price=500.0,
            purchase_date=datetime.now(UTC),
        )
        assert lot.cost_basis == 500_000_000.0


# ── Wash sale boundary checks ──


class TestWashSaleBoundary:
    def test_buy_exactly_30_days_before(self):
        model = DefaultCostModel(wash_sale_window_days=30)
        sell_date = datetime(2026, 1, 31, tzinfo=UTC)
        buy_history = [
            {"symbol": "AAPL", "date": datetime(2026, 1, 1, tzinfo=UTC)},
        ]
        assert model.check_wash_sale("AAPL", sell_date, buy_history) is True

    def test_buy_exactly_31_days_before(self):
        model = DefaultCostModel(wash_sale_window_days=30)
        sell_date = datetime(2026, 2, 1, tzinfo=UTC)
        buy_history = [
            {"symbol": "AAPL", "date": datetime(2026, 1, 1, tzinfo=UTC)},
        ]
        assert model.check_wash_sale("AAPL", sell_date, buy_history) is False

    def test_buy_exactly_30_days_after(self):
        model = DefaultCostModel(wash_sale_window_days=30)
        sell_date = datetime(2026, 1, 1, tzinfo=UTC)
        buy_history = [
            {"symbol": "AAPL", "date": datetime(2026, 1, 31, tzinfo=UTC)},
        ]
        assert model.check_wash_sale("AAPL", sell_date, buy_history) is True

    def test_empty_buy_history(self):
        model = DefaultCostModel()
        sell_date = datetime.now(UTC)
        assert model.check_wash_sale("AAPL", sell_date, []) is False

    def test_buy_missing_date_key_raises(self):
        model = DefaultCostModel()
        sell_date = datetime.now(UTC)
        buy_history = [{"symbol": "AAPL"}]
        with pytest.raises(TypeError):
            model.check_wash_sale("AAPL", sell_date, buy_history)

    def test_buy_missing_symbol_defaults_empty(self):
        model = DefaultCostModel()
        sell_date = datetime.now(UTC)
        buy_history = [{"date": sell_date - timedelta(days=10)}]
        assert model.check_wash_sale("AAPL", sell_date, buy_history) is False


# ── Wash sale adjustment edge cases ──


class TestWashSaleAdjustmentEdgeCases:
    def test_no_replacement_lots_with_loss(self):
        model = DefaultCostModel()
        sell_date = datetime.now(UTC)
        result = model.calculate_wash_sale_adjustment(
            "AAPL", sell_date, -500.0, [],
        )
        assert result["is_wash_sale"] is False
        assert result["adjustment"] == 0.0
        assert result["replacement_lots"] == []

    def test_positive_gain_returns_empty(self):
        model = DefaultCostModel()
        sell_date = datetime.now(UTC)
        buy_history = [
            {"symbol": "AAPL", "date": sell_date - timedelta(days=10), "price": 100.0, "quantity": 50},
        ]
        result = model.calculate_wash_sale_adjustment("AAPL", sell_date, 500.0, buy_history)
        assert result["is_wash_sale"] is False

    def test_zero_loss_returns_empty(self):
        model = DefaultCostModel()
        sell_date = datetime.now(UTC)
        buy_history = [
            {"symbol": "AAPL", "date": sell_date - timedelta(days=10), "price": 100.0, "quantity": 50},
        ]
        result = model.calculate_wash_sale_adjustment("AAPL", sell_date, 0.0, buy_history)
        assert result["is_wash_sale"] is False

    def test_multiple_replacement_lots(self):
        model = DefaultCostModel()
        sell_date = datetime.now(UTC)
        loss = -600.0
        buy_history = [
            {"symbol": "AAPL", "date": sell_date - timedelta(days=5), "price": 140.0, "quantity": 100},
            {"symbol": "AAPL", "date": sell_date - timedelta(days=15), "price": 145.0, "quantity": 200},
        ]
        result = model.calculate_wash_sale_adjustment("AAPL", sell_date, loss, buy_history)
        assert result["is_wash_sale"] is True
        assert result["adjustment"] == 600.0
        assert result["adjustment_per_share"] == pytest.approx(600.0 / 300.0)
        assert len(result["replacement_lots"]) == 2

    def test_replacement_lot_fields(self):
        model = DefaultCostModel()
        sell_date = datetime.now(UTC)
        buy_date = sell_date - timedelta(days=10)
        buy_history = [
            {"symbol": "AAPL", "date": buy_date, "price": 145.0, "quantity": 100},
        ]
        result = model.calculate_wash_sale_adjustment("AAPL", sell_date, -500.0, buy_history)
        lot = result["replacement_lots"][0]
        assert lot["date"] == buy_date
        assert lot["price"] == 145.0
        assert lot["quantity"] == 100


# ── Dividend tax ──


class TestDividendTaxEdgeCases:
    def test_zero_dividend(self):
        model = DefaultCostModel()
        tax = model.estimate_dividend_tax(0.0, is_qualified=True)
        assert tax.amount == 0.0

    def test_large_dividend_qualified(self):
        model = DefaultCostModel(qualified_dividend_rate=0.15)
        tax = model.estimate_dividend_tax(100_000.0, is_qualified=True)
        assert tax.amount == 15_000.0

    def test_large_dividend_ordinary(self):
        model = DefaultCostModel(ordinary_dividend_rate=0.37)
        tax = model.estimate_dividend_tax(100_000.0, is_qualified=False)
        assert tax.amount == 37_000.0

    def test_custom_rates(self):
        model = DefaultCostModel(qualified_dividend_rate=0.20, ordinary_dividend_rate=0.30)
        q = model.estimate_dividend_tax(1000.0, is_qualified=True)
        o = model.estimate_dividend_tax(1000.0, is_qualified=False)
        assert q.amount == 200.0
        assert o.amount == 300.0


# ── ICostModel interface ──


class TestICostModelInterface:
    def test_icostmodel_is_abstract(self):
        with pytest.raises(TypeError):
            ICostModel()

    def test_default_cost_model_is_icostmodel(self):
        model = DefaultCostModel()
        assert isinstance(model, ICostModel)


# ── TaxMethod enum ──


class TestTaxMethodEnum:
    def test_all_methods(self):
        assert TaxMethod.FIFO.value == "fifo"
        assert TaxMethod.LIFO.value == "lifo"
        assert TaxMethod.SPECIFIC_LOT.value == "specific_lot"

    def test_from_value(self):
        assert TaxMethod("fifo") == TaxMethod.FIFO
        assert TaxMethod("lifo") == TaxMethod.LIFO
