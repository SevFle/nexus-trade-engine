"""Tests for holding-cost helpers (gh#96 follow-up)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from engine.core.holding_costs import (
    DAYS_PER_YEAR,
    dividend_payment,
    hard_to_borrow_cost,
    reinvested_shares,
    reinvestment_residual_cash,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_days_per_year(self):
        assert DAYS_PER_YEAR == 365


# ---------------------------------------------------------------------------
# Hard-to-borrow
# ---------------------------------------------------------------------------


class TestHardToBorrow:
    def test_one_day_at_15_pct(self):
        # $100k short × 15 % / 365 ≈ $41.10 / day.
        assert hard_to_borrow_cost(
            Decimal("100000"), Decimal("0.15")
        ) == Decimal("41.10")

    def test_30_days_scales_linearly(self):
        out = hard_to_borrow_cost(
            Decimal("100000"), Decimal("0.15"), days=30
        )
        assert out == Decimal("1232.88")

    def test_extreme_htb_rate(self):
        # Severely-restricted name, 200 % APR, $50k short, one day.
        # 50k × 2.0 / 365 ≈ $273.97 / day.
        out = hard_to_borrow_cost(
            Decimal("50000"), Decimal("2.0"), days=1
        )
        assert out == Decimal("273.97")

    def test_zero_short_zero_cost(self):
        assert hard_to_borrow_cost(
            Decimal("0"), Decimal("0.10")
        ) == Decimal("0.00")

    def test_zero_rate_zero_cost(self):
        assert hard_to_borrow_cost(
            Decimal("100000"), Decimal("0")
        ) == Decimal("0.00")

    def test_negative_short_value_rejected(self):
        with pytest.raises(ValueError):
            hard_to_borrow_cost(Decimal("-1"), Decimal("0.10"))

    def test_negative_rate_rejected(self):
        with pytest.raises(ValueError):
            hard_to_borrow_cost(Decimal("100"), Decimal("-0.01"))

    def test_negative_days_rejected(self):
        with pytest.raises(ValueError):
            hard_to_borrow_cost(
                Decimal("100"), Decimal("0.10"), days=-1
            )

    def test_zero_days_per_year_rejected(self):
        with pytest.raises(ValueError):
            hard_to_borrow_cost(
                Decimal("100"), Decimal("0.10"), days_per_year=0
            )


# ---------------------------------------------------------------------------
# Dividend payment
# ---------------------------------------------------------------------------


class TestDividendPayment:
    def test_known_value(self):
        # 250 shares × $0.42 = $105.00
        assert dividend_payment(
            Decimal("250"), Decimal("0.42")
        ) == Decimal("105.00")

    def test_fractional_shares(self):
        # 100.5 shares × $1.00 = $100.50
        assert dividend_payment(
            Decimal("100.5"), Decimal("1.00")
        ) == Decimal("100.50")

    def test_zero_shares_zero_dividend(self):
        assert dividend_payment(
            Decimal("0"), Decimal("1.00")
        ) == Decimal("0.00")

    def test_zero_per_share_zero_dividend(self):
        assert dividend_payment(
            Decimal("100"), Decimal("0")
        ) == Decimal("0.00")

    def test_negative_shares_rejected(self):
        with pytest.raises(ValueError):
            dividend_payment(Decimal("-1"), Decimal("1.00"))

    def test_negative_dividend_rejected(self):
        with pytest.raises(ValueError):
            dividend_payment(Decimal("100"), Decimal("-0.01"))


# ---------------------------------------------------------------------------
# DRIP reinvestment
# ---------------------------------------------------------------------------


class TestReinvestedShares:
    def test_fractional_default(self):
        # $105.00 / $50.00 = 2.1 shares.
        assert reinvested_shares(
            Decimal("105"), Decimal("50")
        ) == Decimal("2.1000")

    def test_fractional_quantises_to_four_decimals(self):
        # $100 / $33 = 3.0303030303... → 3.0303 (4dp).
        assert reinvested_shares(
            Decimal("100"), Decimal("33")
        ) == Decimal("3.0303")

    def test_integer_truncation_with_fractional_false(self):
        # $105 / $50 = 2.1 → 2 (integer truncation).
        assert reinvested_shares(
            Decimal("105"), Decimal("50"), fractional=False
        ) == Decimal("2")

    def test_zero_cash_zero_shares(self):
        assert reinvested_shares(
            Decimal("0"), Decimal("50")
        ) == Decimal("0.0000")

    def test_negative_cash_rejected(self):
        with pytest.raises(ValueError):
            reinvested_shares(Decimal("-1"), Decimal("50"))

    def test_zero_price_rejected(self):
        with pytest.raises(ValueError):
            reinvested_shares(Decimal("100"), Decimal("0"))

    def test_negative_price_rejected(self):
        with pytest.raises(ValueError):
            reinvested_shares(Decimal("100"), Decimal("-1"))


class TestReinvestmentResidualCash:
    def test_integer_truncation_residual(self):
        # $105 cash, 2 shares × $50 = $100 spent → $5 residual.
        assert reinvestment_residual_cash(
            Decimal("105"), Decimal("50"), Decimal("2")
        ) == Decimal("5.00")

    def test_full_fractional_purchase_zero_residual(self):
        # $105 cash, 2.1 shares × $50 = $105 spent → 0 residual.
        assert reinvestment_residual_cash(
            Decimal("105"), Decimal("50"), Decimal("2.1")
        ) == Decimal("0.00")

    def test_overspend_rejected(self):
        # 3 × $50 = $150 > $100 cash → residual would be negative.
        with pytest.raises(ValueError):
            reinvestment_residual_cash(
                Decimal("100"), Decimal("50"), Decimal("3")
            )

    def test_negative_inputs_rejected(self):
        with pytest.raises(ValueError):
            reinvestment_residual_cash(
                Decimal("-1"), Decimal("50"), Decimal("0")
            )
        with pytest.raises(ValueError):
            reinvestment_residual_cash(
                Decimal("100"), Decimal("-1"), Decimal("0")
            )
        with pytest.raises(ValueError):
            reinvestment_residual_cash(
                Decimal("100"), Decimal("50"), Decimal("-1")
            )
