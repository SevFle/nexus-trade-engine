"""Tests for execution cost helpers (gh#96 follow-up)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from engine.core.execution_costs import (
    DEFAULT_MAKER_REBATE_PER_SHARE,
    DEFAULT_TAKER_FEE_PER_SHARE,
    NSCC_FEE_PER_SIDE_2025,
    exchange_maker_rebate,
    exchange_taker_fee,
    half_spread_cost,
    nscc_clearing_fee,
    opportunity_cost,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_defaults_pinned(self):
        assert NSCC_FEE_PER_SIDE_2025 == Decimal("0.0002")
        assert DEFAULT_TAKER_FEE_PER_SHARE == Decimal("0.0030")
        assert DEFAULT_MAKER_REBATE_PER_SHARE == Decimal("0.0020")


# ---------------------------------------------------------------------------
# half_spread_cost
# ---------------------------------------------------------------------------


class TestHalfSpread:
    def test_known_value(self):
        # 1-cent spread × 1000 shares × 0.5 = $5.00
        assert half_spread_cost(Decimal("0.01"), 1000) == Decimal("5.00")

    def test_wide_illiquid_spread(self):
        # 50 bps spread on $100 stock → $0.50 spread, half = $0.25
        # × 200 shares = $50.00
        assert half_spread_cost(Decimal("0.50"), 200) == Decimal("50.00")

    def test_zero_quantity_zero_cost(self):
        assert half_spread_cost(Decimal("0.05"), 0) == Decimal("0.00")

    def test_zero_spread_zero_cost(self):
        assert half_spread_cost(Decimal("0"), 1000) == Decimal("0.00")

    def test_negative_spread_rejected(self):
        with pytest.raises(ValueError):
            half_spread_cost(Decimal("-0.01"), 100)

    def test_negative_quantity_rejected(self):
        with pytest.raises(ValueError):
            half_spread_cost(Decimal("0.01"), -1)


# ---------------------------------------------------------------------------
# nscc_clearing_fee
# ---------------------------------------------------------------------------


class TestNsccFee:
    def test_known_value(self):
        # 100,000 shares × $0.0002 = $20.00
        assert nscc_clearing_fee(100_000) == Decimal("20.00")

    def test_zero_quantity_zero_fee(self):
        assert nscc_clearing_fee(0) == Decimal("0.00")

    def test_negative_quantity_rejected(self):
        with pytest.raises(ValueError):
            nscc_clearing_fee(-1)

    def test_per_side_override(self):
        # Operator pin to a different rate.
        assert (
            nscc_clearing_fee(100_000, per_side=Decimal("0.0005"))
            == Decimal("50.00")
        )


# ---------------------------------------------------------------------------
# exchange_taker_fee
# ---------------------------------------------------------------------------


class TestTakerFee:
    def test_known_value(self):
        # 1000 shares × $0.0030 = $3.00
        assert exchange_taker_fee(1000) == Decimal("3.00")

    def test_custom_rate(self):
        # Some venues charge $0.0029.
        assert (
            exchange_taker_fee(1000, rate_per_share=Decimal("0.0029"))
            == Decimal("2.90")
        )

    def test_zero_quantity_zero_fee(self):
        assert exchange_taker_fee(0) == Decimal("0.00")

    def test_negative_quantity_rejected(self):
        with pytest.raises(ValueError):
            exchange_taker_fee(-1)


# ---------------------------------------------------------------------------
# exchange_maker_rebate
# ---------------------------------------------------------------------------


class TestMakerRebate:
    def test_known_value(self):
        # 1000 shares × $0.0020 = $2.00 paid TO the trader.
        assert exchange_maker_rebate(1000) == Decimal("2.00")

    def test_custom_rate(self):
        assert (
            exchange_maker_rebate(1000, rate_per_share=Decimal("0.0030"))
            == Decimal("3.00")
        )

    def test_zero_quantity_zero_rebate(self):
        assert exchange_maker_rebate(0) == Decimal("0.00")


# ---------------------------------------------------------------------------
# opportunity_cost
# ---------------------------------------------------------------------------


class TestOpportunityCost:
    def test_buy_market_runs_away_positive_cost(self):
        # 100 unfilled shares, market moved $0.50 against the buy
        # → caller passes +0.50 as the signed drift; cost = $50.00.
        assert opportunity_cost(100, Decimal("0.50")) == Decimal("50.00")

    def test_favourable_drift_yields_negative_cost(self):
        # Market dropped $0.20 while buy was waiting — buyer would
        # have been better off waiting longer; opportunity cost is
        # negative (a *gain* in implementation-shortfall terms).
        assert opportunity_cost(100, Decimal("-0.20")) == Decimal("-20.00")

    def test_zero_unfilled_zero_cost(self):
        assert opportunity_cost(0, Decimal("1.00")) == Decimal("0.00")

    def test_zero_drift_zero_cost(self):
        assert opportunity_cost(100, Decimal("0")) == Decimal("0.00")

    def test_negative_unfilled_rejected(self):
        with pytest.raises(ValueError):
            opportunity_cost(-1, Decimal("0.01"))

    def test_quantises_to_cent(self):
        # Awkward fractional drift: 100 shares × 0.123 = 12.30.
        assert opportunity_cost(100, Decimal("0.123")) == Decimal("12.30")
