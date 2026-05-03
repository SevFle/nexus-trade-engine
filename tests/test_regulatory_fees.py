"""Tests for US regulatory & holding-cost helpers (gh#96 follow-up)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from engine.core.regulatory_fees import (
    DAYS_PER_YEAR,
    FINRA_TAF_MAX_PER_TRADE_2025,
    FINRA_TAF_PER_SHARE_2025,
    OCC_CLEARING_FEE_PER_CONTRACT,
    ORF_PER_CONTRACT_2025,
    SEC_SECTION_31_RATE_PER_MILLION_2026,
    daily_margin_interest,
    finra_taf,
    occ_clearing_fee,
    options_regulatory_fee,
    sec_section_31_fee,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_default_rates_pinned(self):
        assert Decimal("20.60") == SEC_SECTION_31_RATE_PER_MILLION_2026
        assert Decimal("0.000166") == FINRA_TAF_PER_SHARE_2025
        assert Decimal("8.30") == FINRA_TAF_MAX_PER_TRADE_2025
        assert Decimal("0.02905") == ORF_PER_CONTRACT_2025
        assert Decimal("0.055") == OCC_CLEARING_FEE_PER_CONTRACT
        assert DAYS_PER_YEAR == 365


# ---------------------------------------------------------------------------
# SEC Section 31
# ---------------------------------------------------------------------------


class TestSecSection31:
    def test_buy_returns_zero(self):
        assert (
            sec_section_31_fee(Decimal("100000"), side="buy") == Decimal("0.00")
        )

    def test_known_sell_at_default_rate(self):
        # $1,000,000 sell * $20.60 / $1,000,000 = $20.60.
        out = sec_section_31_fee(Decimal("1000000"), side="sell")
        assert out == Decimal("20.60")

    def test_small_sell_quantises_to_cent(self):
        # $10,000 * $20.60 / $1,000,000 = $0.206 → rounds to $0.21.
        out = sec_section_31_fee(Decimal("10000"), side="sell")
        assert out == Decimal("0.21")

    def test_zero_proceeds_zero_fee(self):
        assert sec_section_31_fee(Decimal("0"), side="sell") == Decimal("0.00")

    def test_negative_proceeds_rejected(self):
        with pytest.raises(ValueError):
            sec_section_31_fee(Decimal("-1"), side="sell")

    def test_custom_rate_override(self):
        # Operator pinning to historical FY2024 rate of $8.00/M.
        out = sec_section_31_fee(
            Decimal("1000000"),
            side="sell",
            rate_per_million=Decimal("8.00"),
        )
        assert out == Decimal("8.00")


# ---------------------------------------------------------------------------
# FINRA TAF
# ---------------------------------------------------------------------------


class TestFinraTaf:
    def test_buy_returns_zero(self):
        assert finra_taf(10_000, side="buy") == Decimal("0.00")

    def test_known_sell_at_default_rate(self):
        # 1,000 shares * $0.000166 = $0.166 → rounds to $0.17.
        assert finra_taf(1_000, side="sell") == Decimal("0.17")

    def test_cap_kicks_in_for_huge_order(self):
        # 1,000,000 shares * $0.000166 = $166 → capped at $8.30.
        assert finra_taf(1_000_000, side="sell") == Decimal("8.30")

    def test_zero_quantity_zero_fee(self):
        assert finra_taf(0, side="sell") == Decimal("0.00")

    def test_negative_quantity_rejected(self):
        with pytest.raises(ValueError):
            finra_taf(-1, side="sell")


# ---------------------------------------------------------------------------
# Options Regulatory Fee
# ---------------------------------------------------------------------------


class TestOrf:
    def test_known_value(self):
        # 100 contracts * $0.02905 = $2.905. Decimal default rounding
        # is HALF_EVEN (banker's rounding), so 2.905 → 2.90 (rounds to
        # the nearest even cent). The amount that hits the broker is
        # the integer-cent quantisation regardless of mode.
        assert options_regulatory_fee(100) == Decimal("2.90")

    def test_zero_contracts_zero_fee(self):
        assert options_regulatory_fee(0) == Decimal("0.00")

    def test_negative_contracts_rejected(self):
        with pytest.raises(ValueError):
            options_regulatory_fee(-1)


# ---------------------------------------------------------------------------
# OCC clearing fee
# ---------------------------------------------------------------------------


class TestOccClearingFee:
    def test_known_value(self):
        # 100 contracts * $0.055 = $5.50.
        assert occ_clearing_fee(100) == Decimal("5.50")

    def test_zero_contracts_zero_fee(self):
        assert occ_clearing_fee(0) == Decimal("0.00")

    def test_negative_contracts_rejected(self):
        with pytest.raises(ValueError):
            occ_clearing_fee(-1)


# ---------------------------------------------------------------------------
# Daily margin interest
# ---------------------------------------------------------------------------


class TestMarginInterest:
    def test_one_day_accrual_known_value(self):
        # $100,000 borrowed * 8.5 % / 365 ≈ $23.29 / day.
        out = daily_margin_interest(
            Decimal("100000"), Decimal("0.085")
        )
        assert out == Decimal("23.29")

    def test_multi_day_accrual_scales_linearly(self):
        # Same loan over 30 days ≈ $698.63.
        out = daily_margin_interest(
            Decimal("100000"), Decimal("0.085"), days=30
        )
        assert out == Decimal("698.63")

    def test_zero_borrowed_zero_interest(self):
        assert (
            daily_margin_interest(Decimal("0"), Decimal("0.05"))
            == Decimal("0.00")
        )

    def test_zero_rate_zero_interest(self):
        assert (
            daily_margin_interest(Decimal("100000"), Decimal("0"))
            == Decimal("0.00")
        )

    def test_negative_amount_rejected(self):
        with pytest.raises(ValueError):
            daily_margin_interest(Decimal("-1"), Decimal("0.05"))

    def test_negative_rate_rejected(self):
        with pytest.raises(ValueError):
            daily_margin_interest(Decimal("100"), Decimal("-0.01"))

    def test_zero_days_per_year_rejected(self):
        with pytest.raises(ValueError):
            daily_margin_interest(
                Decimal("100"), Decimal("0.05"), days_per_year=0
            )
