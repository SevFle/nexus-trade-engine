"""Tests for pluggable commission calculator models."""

from __future__ import annotations

import pytest

from engine.core.execution.commission import (
    CommissionModelType,
    FlatRateCommission,
    PercentageCommission,
    PerShareCommission,
    TieredCommission,
    ZeroCommission,
    create_commission_calculator,
)


class TestPerShareCommission:
    def test_basic_buy(self):
        calc = PerShareCommission(rate_per_share=0.005, min_commission=1.0)
        quote = calc.calculate(quantity=100, price=150.0, side="buy")
        assert quote.estimated_commission == 1.0
        assert quote.total >= 1.0

    def test_large_quantity_exceeds_min(self):
        calc = PerShareCommission(rate_per_share=0.005, min_commission=1.0)
        quote = calc.calculate(quantity=1000, price=150.0, side="buy")
        assert quote.estimated_commission == 5.0

    def test_minimum_commission(self):
        calc = PerShareCommission(rate_per_share=0.005, min_commission=5.0)
        quote = calc.calculate(quantity=10, price=100.0, side="buy")
        assert quote.estimated_commission == 5.0

    def test_no_minimum_needed(self):
        calc = PerShareCommission(rate_per_share=0.01, min_commission=1.0)
        quote = calc.calculate(quantity=1000, price=100.0, side="buy")
        assert quote.estimated_commission == 10.0

    def test_sell_includes_regulatory_fee(self):
        calc = PerShareCommission(rate_per_share=0.005, min_commission=1.0)
        buy_quote = calc.calculate(quantity=100, price=100.0, side="buy")
        sell_quote = calc.calculate(quantity=100, price=100.0, side="sell")
        assert sell_quote.regulatory_fee > 0
        assert buy_quote.regulatory_fee == 0.0

    def test_exchange_fee(self):
        calc = PerShareCommission(rate_per_share=0.005, exchange_fee_per_share=0.002)
        quote = calc.calculate(quantity=100, price=100.0, side="buy")
        assert quote.exchange_fee == 0.2


class TestFlatRateCommission:
    def test_flat_rate(self):
        calc = FlatRateCommission(flat_rate=4.95)
        quote = calc.calculate(quantity=100, price=150.0, side="buy")
        assert quote.estimated_commission == 4.95
        assert quote.total == 4.95

    def test_with_exchange_fee(self):
        calc = FlatRateCommission(flat_rate=4.95, exchange_fee=0.50)
        quote = calc.calculate(quantity=100, price=150.0, side="buy")
        assert quote.total == 5.45


class TestPercentageCommission:
    def test_basic(self):
        calc = PercentageCommission(rate_pct=0.001, min_commission=1.0)
        quote = calc.calculate(quantity=100, price=100.0, side="buy")
        assert quote.estimated_commission == 10.0

    def test_minimum_applied(self):
        calc = PercentageCommission(rate_pct=0.001, min_commission=5.0)
        quote = calc.calculate(quantity=10, price=10.0, side="buy")
        assert quote.estimated_commission == 5.0


class TestTieredCommission:
    def test_low_tier(self):
        calc = TieredCommission()
        quote = calc.calculate(quantity=100, price=100.0, side="buy")
        assert quote.estimated_commission > 0

    def test_high_tier(self):
        calc = TieredCommission()
        quote = calc.calculate(quantity=15000, price=100.0, side="buy")
        assert quote.estimated_commission > 0

    def test_custom_tiers(self):
        tiers = [(0, 0.01), (100, 0.005), (1000, 0.001)]
        calc = TieredCommission(tiers=tiers, min_commission=0.0)
        q1 = calc.calculate(quantity=50, price=100.0, side="buy")
        q2 = calc.calculate(quantity=500, price=100.0, side="buy")
        q3 = calc.calculate(quantity=5000, price=100.0, side="buy")
        assert q1.estimated_commission == 0.5
        assert q2.estimated_commission == 2.5
        assert q3.estimated_commission == 5.0


class TestZeroCommission:
    def test_always_zero(self):
        calc = ZeroCommission()
        quote = calc.calculate(quantity=10000, price=500.0, side="sell")
        assert quote.estimated_commission == 0.0
        assert quote.total == 0.0


class TestCreateCommissionCalculator:
    def test_creates_per_share(self):
        calc = create_commission_calculator("per_share")
        assert isinstance(calc, PerShareCommission)

    def test_creates_flat_rate(self):
        calc = create_commission_calculator("flat_rate")
        assert isinstance(calc, FlatRateCommission)

    def test_creates_percentage(self):
        calc = create_commission_calculator("percentage")
        assert isinstance(calc, PercentageCommission)

    def test_creates_tiered(self):
        calc = create_commission_calculator("tiered")
        assert isinstance(calc, TieredCommission)

    def test_creates_zero(self):
        calc = create_commission_calculator("zero")
        assert isinstance(calc, ZeroCommission)

    def test_invalid_type_raises(self):
        with pytest.raises(ValueError):
            create_commission_calculator("invalid")

    def test_passes_kwargs(self):
        calc = create_commission_calculator(
            CommissionModelType.PER_SHARE,
            rate_per_share=0.01,
            min_commission=2.0,
        )
        assert isinstance(calc, PerShareCommission)
        quote = calc.calculate(quantity=50, price=100.0, side="buy")
        assert quote.estimated_commission == 2.0
