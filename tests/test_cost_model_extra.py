"""Additional coverage for engine.core.cost_model uncovered lines.

Existing tests cover commission, spread, slippage, total, tax, wash sale,
and dividend. This file targets:
  - Money.__add__ / __sub__ (lines 30-31, 34-35)
  - Money.is_zero (line 39)
  - Money.as_pct_of with total==0 (lines 42-44)
  - TaxLot.cost_basis (line 104)
  - TaxMethod.SPECIFIC_LOT path in estimate_tax (line 263)
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


class TestMoney:
    def test_add_same_currency(self):
        a = Money(amount=10.0)
        b = Money(amount=5.0)
        result = a + b
        assert abs(result.amount - 15.0) < 1e-10
        assert result.currency == "USD"

    def test_sub_same_currency(self):
        a = Money(amount=10.0)
        b = Money(amount=3.0)
        result = a - b
        assert abs(result.amount - 7.0) < 1e-10

    def test_is_zero_true(self):
        m = Money(amount=0.0)
        assert m.is_zero is True

    def test_is_zero_near_zero(self):
        m = Money(amount=1e-11)
        assert m.is_zero is True

    def test_is_zero_false(self):
        m = Money(amount=0.01)
        assert m.is_zero is False

    def test_as_pct_of_normal(self):
        m = Money(amount=25.0)
        assert abs(m.as_pct_of(100.0) - 25.0) < 1e-10

    def test_as_pct_of_zero_total(self):
        m = Money(amount=10.0)
        assert m.as_pct_of(0.0) == 0.0

    def test_add_cross_currency_asserts(self):
        a = Money(amount=1.0, currency="USD")
        b = Money(amount=1.0, currency="EUR")
        with pytest.raises(AssertionError):
            a + b

    def test_sub_cross_currency_asserts(self):
        a = Money(amount=1.0, currency="USD")
        b = Money(amount=1.0, currency="EUR")
        with pytest.raises(AssertionError):
            a - b


class TestCostBreakdown:
    def test_total_without_tax(self):
        cb = CostBreakdown(
            commission=Money(amount=1.0),
            spread=Money(amount=2.0),
            slippage=Money(amount=3.0),
            exchange_fee=Money(amount=0.5),
            tax_estimate=Money(amount=5.0),
        )
        total = cb.total
        assert abs(total.amount - 11.5) < 1e-10
        without_tax = cb.total_without_tax
        assert abs(without_tax.amount - 6.5) < 1e-10

    def test_as_dict_keys(self):
        cb = CostBreakdown(
            commission=Money(amount=1.0),
            spread=Money(amount=2.0),
        )
        d = cb.as_dict()
        assert "commission" in d
        assert "spread" in d
        assert "slippage" in d
        assert "exchange_fee" in d
        assert "tax_estimate" in d
        assert "currency_conversion" in d
        assert "total" in d
        assert d["commission"] == 1.0
        assert d["spread"] == 2.0


class TestTaxLot:
    def test_cost_basis(self):
        lot = TaxLot(
            symbol="AAPL",
            quantity=100,
            purchase_price=150.0,
            purchase_date=datetime.now(UTC),
        )
        assert lot.cost_basis == 15_000.0

    def test_cost_basis_zero_quantity(self):
        lot = TaxLot(
            symbol="AAPL",
            quantity=0,
            purchase_price=150.0,
            purchase_date=datetime.now(UTC),
        )
        assert lot.cost_basis == 0.0

    def test_is_long_term_true(self):
        lot = TaxLot(
            symbol="AAPL",
            quantity=10,
            purchase_price=100.0,
            purchase_date=datetime.now(UTC) - timedelta(days=400),
        )
        assert lot.is_long_term() is True

    def test_is_long_term_false(self):
        lot = TaxLot(
            symbol="AAPL",
            quantity=10,
            purchase_price=100.0,
            purchase_date=datetime.now(UTC) - timedelta(days=100),
        )
        assert lot.is_long_term() is False

    def test_is_long_term_with_as_of(self):
        purchase = datetime(2023, 1, 1, tzinfo=UTC)
        lot = TaxLot(
            symbol="AAPL",
            quantity=10,
            purchase_price=100.0,
            purchase_date=purchase,
        )
        as_of = datetime(2024, 1, 2, tzinfo=UTC)
        assert lot.is_long_term(as_of=as_of) is True
        as_of_short = datetime(2023, 6, 1, tzinfo=UTC)
        assert lot.is_long_term(as_of=as_of_short) is False

    def test_lot_id_default(self):
        lot = TaxLot(
            symbol="AAPL",
            quantity=10,
            purchase_price=100.0,
            purchase_date=datetime.now(UTC),
        )
        assert lot.lot_id == ""

    def test_lot_id_custom(self):
        lot = TaxLot(
            symbol="AAPL",
            quantity=10,
            purchase_price=100.0,
            purchase_date=datetime.now(UTC),
            lot_id="custom-id",
        )
        assert lot.lot_id == "custom-id"


class TestDefaultCostModelSpecificLot:
    def test_specific_lot_preserves_order(self):
        cm = DefaultCostModel(short_term_tax_rate=0.37, long_term_tax_rate=0.20)
        now = datetime.now(UTC)
        lots = [
            TaxLot(symbol="AAPL", quantity=10, purchase_price=100.0, purchase_date=now),
            TaxLot(symbol="AAPL", quantity=10, purchase_price=120.0, purchase_date=now),
        ]
        tax = cm.estimate_tax(
            symbol="AAPL",
            sell_price=130.0,
            quantity=20,
            lots=lots,
            method=TaxMethod.SPECIFIC_LOT,
        )
        expected_tax = (30.0 * 10 * 0.37) + (10.0 * 10 * 0.37)
        assert abs(tax.amount - expected_tax) < 1e-6

    def test_specific_lot_with_sell_date(self):
        cm = DefaultCostModel(long_term_tax_rate=0.20)
        now = datetime.now(UTC)
        old = now - timedelta(days=400)
        lots = [
            TaxLot(
                symbol="AAPL",
                quantity=10,
                purchase_price=100.0,
                purchase_date=old,
            ),
        ]
        tax = cm.estimate_tax(
            symbol="AAPL",
            sell_price=130.0,
            quantity=10,
            lots=lots,
            method=TaxMethod.SPECIFIC_LOT,
            sell_date=now,
        )
        assert abs(tax.amount - (30.0 * 10 * 0.20)) < 1e-6


class TestDefaultCostModelEdgeCases:
    def test_estimate_pct_zero_price(self):
        cm = DefaultCostModel(commission_per_trade=5.0)
        pct = cm.estimate_pct("AAPL", price=0.0)
        assert pct == 0.0

    def test_estimate_pct_positive_price(self):
        cm = DefaultCostModel(
            commission_per_trade=1.0,
            spread_bps=5.0,
            slippage_bps=10.0,
        )
        pct = cm.estimate_pct("AAPL", price=100.0)
        assert pct > 0

    def test_check_wash_sale_no_matching_symbol(self):
        cm = DefaultCostModel()
        sell_date = datetime.now(UTC)
        buy_history = [{"date": sell_date - timedelta(days=5), "symbol": "MSFT"}]
        assert cm.check_wash_sale("AAPL", sell_date, buy_history) is False

    def test_check_wash_sale_empty_history(self):
        cm = DefaultCostModel()
        assert cm.check_wash_sale("AAPL", datetime.now(UTC), []) is False

    def test_calculate_wash_sale_positive_loss_returns_empty(self):
        cm = DefaultCostModel()
        result = cm.calculate_wash_sale_adjustment(
            "AAPL", datetime.now(UTC), loss_amount=100.0, buy_history=[]
        )
        assert result["is_wash_sale"] is False
        assert result["adjustment"] == 0.0

    def test_calculate_wash_sale_zero_loss_returns_empty(self):
        cm = DefaultCostModel()
        result = cm.calculate_wash_sale_adjustment(
            "AAPL", datetime.now(UTC), loss_amount=0.0, buy_history=[]
        )
        assert result["is_wash_sale"] is False

    def test_calculate_wash_sale_no_replacement_lots(self):
        cm = DefaultCostModel()
        sell_date = datetime.now(UTC)
        result = cm.calculate_wash_sale_adjustment(
            "AAPL",
            sell_date,
            loss_amount=-500.0,
            buy_history=[{"date": sell_date - timedelta(days=60), "symbol": "AAPL"}],
        )
        assert result["is_wash_sale"] is False

    def test_estimate_tax_no_gain(self):
        cm = DefaultCostModel()
        now = datetime.now(UTC)
        lots = [
            TaxLot(symbol="AAPL", quantity=10, purchase_price=150.0, purchase_date=now),
        ]
        tax = cm.estimate_tax("AAPL", sell_price=100.0, quantity=10, lots=lots)
        assert tax.amount == 0.0

    def test_estimate_dividend_tax_ordinary(self):
        cm = DefaultCostModel(ordinary_dividend_rate=0.37)
        tax = cm.estimate_dividend_tax(100.0, is_qualified=False)
        assert abs(tax.amount - 37.0) < 1e-10

    def test_estimate_dividend_tax_qualified(self):
        cm = DefaultCostModel(qualified_dividend_rate=0.15)
        tax = cm.estimate_dividend_tax(100.0, is_qualified=True)
        assert abs(tax.amount - 15.0) < 1e-10

    def test_icostmodel_is_abstract(self):
        with pytest.raises(TypeError):
            ICostModel()
