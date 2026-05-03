"""Tests for crypto cost helpers (gh#96 follow-up)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from engine.core.crypto_costs import (
    DEFAULT_FUNDING_INTERVAL_HOURS,
    constant_product_impermanent_loss,
    fx_conversion,
    perpetual_funding_payment,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_default_funding_interval(self):
        assert DEFAULT_FUNDING_INTERVAL_HOURS == 8


# ---------------------------------------------------------------------------
# Perpetual funding
# ---------------------------------------------------------------------------


class TestPerpetualFunding:
    def test_long_pays_positive_rate(self):
        # $100k notional, +1 bp funding rate, long pays $10.
        out = perpetual_funding_payment(
            Decimal("100000"), Decimal("0.0001"), side="long"
        )
        assert out == Decimal("-10.00")

    def test_short_receives_positive_rate(self):
        out = perpetual_funding_payment(
            Decimal("100000"), Decimal("0.0001"), side="short"
        )
        assert out == Decimal("10.00")

    def test_long_receives_negative_rate(self):
        # Negative funding flips the direction: long receives.
        out = perpetual_funding_payment(
            Decimal("100000"), Decimal("-0.0001"), side="long"
        )
        assert out == Decimal("10.00")

    def test_short_pays_negative_rate(self):
        out = perpetual_funding_payment(
            Decimal("100000"), Decimal("-0.0001"), side="short"
        )
        assert out == Decimal("-10.00")

    def test_zero_notional_zero_payment(self):
        assert (
            perpetual_funding_payment(
                Decimal("0"), Decimal("0.0001"), side="long"
            )
            == Decimal("0.00")
        )

    def test_zero_rate_zero_payment(self):
        assert (
            perpetual_funding_payment(
                Decimal("100000"), Decimal("0"), side="long"
            )
            == Decimal("0.00")
        )

    def test_negative_notional_rejected(self):
        with pytest.raises(ValueError):
            perpetual_funding_payment(
                Decimal("-1"), Decimal("0.0001"), side="long"
            )

    def test_invalid_side_rejected(self):
        with pytest.raises(ValueError):
            perpetual_funding_payment(
                Decimal("100"), Decimal("0.0001"), side="bothways"
            )

    def test_zero_hours_rejected(self):
        with pytest.raises(ValueError):
            perpetual_funding_payment(
                Decimal("100"), Decimal("0.0001"), side="long", hours=0
            )


# ---------------------------------------------------------------------------
# FX conversion
# ---------------------------------------------------------------------------


class TestFxConversion:
    def test_known_value_default_fee(self):
        # $1000 USD * 0.92 EUR/USD * (1 - 10/10000) = 920 - 0.92 = 919.08
        net, fee = fx_conversion(Decimal("1000"), Decimal("0.92"))
        assert net == Decimal("919.08")
        assert fee == Decimal("0.92")

    def test_zero_fee_returns_full_conversion(self):
        net, fee = fx_conversion(
            Decimal("1000"), Decimal("0.92"), fee_bps=Decimal("0")
        )
        assert net == Decimal("920.00")
        assert fee == Decimal("0.00")

    def test_high_fee_for_exotic_pair(self):
        # 100 bps fee — typical for exotic forex pairs.
        net, fee = fx_conversion(
            Decimal("1000"), Decimal("0.92"), fee_bps=Decimal("100")
        )
        assert fee == Decimal("9.20")
        assert net == Decimal("910.80")

    def test_zero_amount_zero_conversion(self):
        net, fee = fx_conversion(Decimal("0"), Decimal("0.92"))
        assert net == Decimal("0.00")
        assert fee == Decimal("0.00")

    def test_negative_amount_rejected(self):
        with pytest.raises(ValueError):
            fx_conversion(Decimal("-1"), Decimal("0.92"))

    def test_negative_rate_rejected(self):
        with pytest.raises(ValueError):
            fx_conversion(Decimal("100"), Decimal("-0.01"))

    def test_negative_fee_rejected(self):
        with pytest.raises(ValueError):
            fx_conversion(
                Decimal("100"), Decimal("0.92"), fee_bps=Decimal("-1")
            )


# ---------------------------------------------------------------------------
# Impermanent loss
# ---------------------------------------------------------------------------


class TestImpermanentLoss:
    def test_no_price_change_no_loss(self):
        assert constant_product_impermanent_loss(1.0) == 0.0

    def test_price_doubled_known_value(self):
        # Standard textbook: at p=2 IL ≈ 5.72 %.
        out = constant_product_impermanent_loss(2.0)
        assert 0.057 < out < 0.058

    def test_price_halved_symmetric(self):
        # IL is symmetric: p=0.5 should match p=2.0.
        a = constant_product_impermanent_loss(0.5)
        b = constant_product_impermanent_loss(2.0)
        assert abs(a - b) < 1e-9

    def test_4x_move_known_value(self):
        # At p=4 IL ≈ 20 %.
        out = constant_product_impermanent_loss(4.0)
        assert 0.19 < out < 0.21

    def test_extreme_move_grows_il(self):
        # Larger price moves produce larger IL up to a limit.
        small = constant_product_impermanent_loss(1.5)
        large = constant_product_impermanent_loss(10.0)
        assert large > small > 0

    def test_zero_price_max_loss(self):
        # Asset went to zero — LP keeps half value.
        assert constant_product_impermanent_loss(0.0) == 0.5

    def test_negative_price_rejected(self):
        with pytest.raises(ValueError):
            constant_product_impermanent_loss(-1.0)

    def test_il_always_non_negative(self):
        for p in (0.1, 0.5, 1.5, 2.0, 5.0, 100.0):
            assert constant_product_impermanent_loss(p) >= 0.0
