"""Tests for the capital-loss carryover (gh#155 follow-up)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from engine.core.tax.reports import (
    DEDUCTIBLE_CAP_DEFAULT,
    DEDUCTIBLE_CAP_MFS,
    CapitalLossApplication,
    CapitalLossCarryover,
    ScheduleDPartTotal,
    ScheduleDSummary,
    apply_carryover,
)


def _summary(short: str, long_: str) -> ScheduleDSummary:
    short_total = ScheduleDPartTotal(
        row_count=1,
        proceeds=Decimal("0.00"),
        cost_basis=Decimal("0.00"),
        adjustment_amount=Decimal("0.00"),
        gain_loss=Decimal(short).quantize(Decimal("0.01")),
    )
    long_total = ScheduleDPartTotal(
        row_count=1,
        proceeds=Decimal("0.00"),
        cost_basis=Decimal("0.00"),
        adjustment_amount=Decimal("0.00"),
        gain_loss=Decimal(long_).quantize(Decimal("0.01")),
    )
    net = (short_total.gain_loss + long_total.gain_loss).quantize(Decimal("0.01"))
    return ScheduleDSummary(
        short_term=short_total,
        long_term=long_total,
        net_gain_loss=net,
    )


# ---------------------------------------------------------------------------
# Identity / no-op cases
# ---------------------------------------------------------------------------


class TestCarryoverConstructors:
    def test_zero_constructor_returns_both_legs_at_zero(self):
        c = CapitalLossCarryover.zero()
        assert c.short_term == Decimal("0.00")
        assert c.long_term == Decimal("0.00")
        assert c.total == Decimal("0.00")

    def test_total_quantises_to_two_decimals(self):
        c = CapitalLossCarryover(
            short_term=Decimal("100.123"), long_term=Decimal("50.456")
        )
        assert c.total == Decimal("150.58")


class TestNoLossNoCarryover:
    def test_pure_gain_year_yields_zero_deduction_and_no_carry(self):
        result = apply_carryover(_summary(short="500", long_="1000"))

        assert isinstance(result, CapitalLossApplication)
        assert result.current_year_deduction == Decimal("0.00")
        assert result.next_year_carryover == CapitalLossCarryover.zero()

    def test_short_loss_offset_by_long_gain_yields_no_deduction(self):
        # -500 short + 800 long = +300 net → no loss.
        result = apply_carryover(_summary(short="-500", long_="800"))

        assert result.current_year_deduction == Decimal("0.00")
        assert result.next_year_carryover.total == Decimal("0.00")


# ---------------------------------------------------------------------------
# Loss within the cap
# ---------------------------------------------------------------------------


class TestLossWithinCap:
    def test_small_loss_fully_deducted_no_carry(self):
        # -1500 short, 0 long → $1,500 deduction, no carry.
        result = apply_carryover(_summary(short="-1500", long_="0"))

        assert result.current_year_deduction == Decimal("1500.00")
        assert result.next_year_carryover == CapitalLossCarryover.zero()


# ---------------------------------------------------------------------------
# Loss above the cap → carryover
# ---------------------------------------------------------------------------


class TestLossAboveCap:
    def test_short_only_loss_caps_at_default_3000_then_carries(self):
        # -10,000 short, 0 long → $3,000 deduction, $7,000 short carry.
        result = apply_carryover(_summary(short="-10000", long_="0"))

        assert result.current_year_deduction == Decimal("3000.00")
        assert result.next_year_carryover.short_term == Decimal("7000.00")
        assert result.next_year_carryover.long_term == Decimal("0.00")

    def test_long_only_loss_caps_then_carries_to_long_bucket(self):
        # -10,000 long, 0 short → $3,000 deduction, $7,000 long carry.
        result = apply_carryover(_summary(short="0", long_="-10000"))

        assert result.current_year_deduction == Decimal("3000.00")
        assert result.next_year_carryover.short_term == Decimal("0.00")
        assert result.next_year_carryover.long_term == Decimal("7000.00")

    def test_both_legs_loss_deduction_pulled_off_short_first(self):
        # -2,000 short + -5,000 long = -7,000 net. $3,000 deduction
        # absorbs the entire $2,000 short loss + $1,000 of long loss.
        # Carry: 0 short + 4,000 long.
        result = apply_carryover(_summary(short="-2000", long_="-5000"))

        assert result.current_year_deduction == Decimal("3000.00")
        assert result.next_year_carryover.short_term == Decimal("0.00")
        assert result.next_year_carryover.long_term == Decimal("4000.00")

    def test_short_loss_offset_then_remainder_carries(self):
        # -8,000 short + 2,000 long = -6,000 net.
        # The $2,000 long gain offsets $2,000 of short loss intra-year.
        # Remaining short loss: $6,000. $3,000 deducted → $3,000 carry.
        result = apply_carryover(_summary(short="-8000", long_="2000"))

        assert result.current_year_deduction == Decimal("3000.00")
        # The residual lives entirely on the short-term leg.
        assert result.next_year_carryover.short_term == Decimal("3000.00")
        assert result.next_year_carryover.long_term == Decimal("0.00")


# ---------------------------------------------------------------------------
# Prior-year carryover applied to current year
# ---------------------------------------------------------------------------


class TestPriorYearCarryover:
    def test_prior_loss_offsets_current_gain_no_new_carry(self):
        # Prior: $4,000 short carry. Current: +5,000 long.
        # Prior absorbs into short leg: short_net = 0 - 4000 = -4000.
        # Combined: -4000 + 5000 = +1000 → no loss this year, no carry.
        prior = CapitalLossCarryover(
            short_term=Decimal("4000"), long_term=Decimal("0")
        )
        result = apply_carryover(_summary(short="0", long_="5000"), prior)

        assert result.current_year_deduction == Decimal("0.00")
        assert result.next_year_carryover == CapitalLossCarryover.zero()

    def test_prior_loss_plus_current_loss_compounds_carryover(self):
        # Prior: $2,000 short carry. Current: -1,000 short, 0 long.
        # Combined short loss: 3,000. $3,000 deducted, no carryover.
        prior = CapitalLossCarryover(
            short_term=Decimal("2000"), long_term=Decimal("0")
        )
        result = apply_carryover(_summary(short="-1000", long_="0"), prior)

        assert result.current_year_deduction == Decimal("3000.00")
        assert result.next_year_carryover == CapitalLossCarryover.zero()

    def test_prior_loss_plus_current_loss_above_cap_carries_remainder(self):
        # Prior: $5,000 short. Current: -3,000 short. Combined = -8,000
        # short. $3,000 deducted → $5,000 short carry.
        prior = CapitalLossCarryover(
            short_term=Decimal("5000"), long_term=Decimal("0")
        )
        result = apply_carryover(_summary(short="-3000", long_="0"), prior)

        assert result.current_year_deduction == Decimal("3000.00")
        assert result.next_year_carryover.short_term == Decimal("5000.00")
        assert result.next_year_carryover.long_term == Decimal("0.00")


# ---------------------------------------------------------------------------
# MFS cap
# ---------------------------------------------------------------------------


class TestMfsCap:
    def test_mfs_cap_halves_the_default_deduction(self):
        # Big loss with the MFS cap → only $1,500 deductible.
        result = apply_carryover(
            _summary(short="-10000", long_="0"),
            deductible_cap=DEDUCTIBLE_CAP_MFS,
        )

        assert result.current_year_deduction == Decimal("1500.00")
        assert result.next_year_carryover.short_term == Decimal("8500.00")

    def test_default_cap_is_three_thousand(self):
        assert Decimal("3000.00") == DEDUCTIBLE_CAP_DEFAULT
        assert Decimal("1500.00") == DEDUCTIBLE_CAP_MFS


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_negative_prior_short_term_rejected(self):
        with pytest.raises(ValueError):
            apply_carryover(
                _summary(short="0", long_="0"),
                CapitalLossCarryover(
                    short_term=Decimal("-1"), long_term=Decimal("0")
                ),
            )

    def test_zero_or_negative_cap_rejected(self):
        with pytest.raises(ValueError):
            apply_carryover(
                _summary(short="-100", long_="0"),
                deductible_cap=Decimal("0"),
            )
