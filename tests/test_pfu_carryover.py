"""Tests for the France PFU 10-year loss carry-forward (gh#155 follow-up)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from engine.core.tax.reports import (
    PfuApplication,
    PfuCarryover,
    PfuDisposal,
    PfuLossVintage,
    apply_pfu_carryover,
)
from engine.core.tax.reports.pfu_carryover import (
    CARRY_FORWARD_YEARS,
    normalised,
)


def _disp(*, proceeds: str, cost: str) -> PfuDisposal:
    return PfuDisposal(
        description="100 ABC.PA",
        acquired=date(2023, 6, 1),
        disposed=date(2024, 6, 1),
        proceeds=Decimal(proceeds),
        cost=Decimal(cost),
    )


# ---------------------------------------------------------------------------
# Vintage / Carryover constructors
# ---------------------------------------------------------------------------


class TestVintage:
    def test_negative_amount_rejected(self):
        with pytest.raises(ValueError):
            PfuLossVintage(year=2024, amount=Decimal("-1"))

    def test_zero_amount_allowed(self):
        # The dataclass itself permits zero; ``normalised`` later drops
        # zero vintages so they don't pollute the carryover.
        v = PfuLossVintage(year=2024, amount=Decimal("0"))
        assert v.amount == Decimal("0")


class TestCarryoverConstructor:
    def test_zero_returns_empty_tuple(self):
        c = PfuCarryover.zero()
        assert c.vintages == ()
        assert c.total == Decimal("0.00")

    def test_total_sums_vintages(self):
        c = PfuCarryover(
            vintages=(
                PfuLossVintage(year=2024, amount=Decimal("1000")),
                PfuLossVintage(year=2023, amount=Decimal("500.5")),
            )
        )
        assert c.total == Decimal("1500.50")


class TestNormalised:
    def test_sorts_oldest_first_and_drops_zero_amounts(self):
        c = PfuCarryover(
            vintages=(
                PfuLossVintage(year=2024, amount=Decimal("100")),
                PfuLossVintage(year=2022, amount=Decimal("200")),
                PfuLossVintage(year=2023, amount=Decimal("0")),
            )
        )
        out = normalised(c)
        assert [v.year for v in out.vintages] == [2022, 2024]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_ten_year_window(self):
        assert CARRY_FORWARD_YEARS == 10


# ---------------------------------------------------------------------------
# No prior — same-year mechanics
# ---------------------------------------------------------------------------


class TestNoPrior:
    def test_pure_gain_year_no_carry_just_pfu(self):
        result = apply_pfu_carryover(
            [_disp(proceeds="6000", cost="5000")],
            current_year=2024,
        )
        assert isinstance(result, PfuApplication)
        assert result.summary.net_gain == Decimal("1000.00")
        assert result.taxable_gain_after_carryover == Decimal("1000.00")
        assert result.income_tax_after_carryover == Decimal("128.00")
        assert result.social_charges_after_carryover == Decimal("172.00")
        assert result.total_tax_after_carryover == Decimal("300.00")
        assert result.next_year_carryover == PfuCarryover.zero()

    def test_pure_loss_year_creates_new_vintage(self):
        result = apply_pfu_carryover(
            [_disp(proceeds="500", cost="2000")],
            current_year=2024,
        )
        assert result.summary.net_loss == Decimal("1500.00")
        assert result.total_tax_after_carryover == Decimal("0.00")
        assert len(result.next_year_carryover.vintages) == 1
        v = result.next_year_carryover.vintages[0]
        assert v.year == 2024
        assert v.amount == Decimal("1500.00")


# ---------------------------------------------------------------------------
# Prior loss applied FIFO
# ---------------------------------------------------------------------------


class TestFifo:
    def test_oldest_vintage_absorbs_first(self):
        prior = PfuCarryover(
            vintages=(
                PfuLossVintage(year=2017, amount=Decimal("400")),
                PfuLossVintage(year=2020, amount=Decimal("300")),
            )
        )
        # +500 EUR gain, prior 700 EUR. 2017 vintage absorbs 400 first,
        # then 100 from 2020. 2020 carries 200 forward.
        result = apply_pfu_carryover(
            [_disp(proceeds="1500", cost="1000")],
            prior,
            current_year=2024,
        )

        assert result.loss_used == Decimal("500.00")
        assert result.taxable_gain_after_carryover == Decimal("0.00")
        assert result.total_tax_after_carryover == Decimal("0.00")
        # 2017 vintage fully consumed → dropped. 2020 reduced to 200.
        assert len(result.next_year_carryover.vintages) == 1
        v = result.next_year_carryover.vintages[0]
        assert v.year == 2020
        assert v.amount == Decimal("200.00")

    def test_gain_above_prior_taxes_remainder(self):
        prior = PfuCarryover(
            vintages=(
                PfuLossVintage(year=2020, amount=Decimal("300")),
            )
        )
        # +1,000 gain, 300 prior → 700 taxable. 12.8% = 89.6, 17.2% =
        # 120.4, total = 210 (rounded to two decimals).
        result = apply_pfu_carryover(
            [_disp(proceeds="2000", cost="1000")],
            prior,
            current_year=2024,
        )

        assert result.loss_used == Decimal("300.00")
        assert result.taxable_gain_after_carryover == Decimal("700.00")
        assert result.total_tax_after_carryover == Decimal("210.00")
        assert result.next_year_carryover == PfuCarryover.zero()


# ---------------------------------------------------------------------------
# 10-year expiry
# ---------------------------------------------------------------------------


class TestExpiry:
    def test_vintage_at_ten_year_wall_is_expired(self):
        # Loss vintage from 2014 against a 2024 gain: 2024 - 2014 = 10
        # → expired (window is years < 10 since vintage).
        prior = PfuCarryover(
            vintages=(
                PfuLossVintage(year=2014, amount=Decimal("500")),
            )
        )
        result = apply_pfu_carryover(
            [_disp(proceeds="2000", cost="1000")],
            prior,
            current_year=2024,
        )

        # Expired vintage cannot offset; full +1,000 gain is taxable.
        assert result.loss_used == Decimal("0.00")
        assert result.taxable_gain_after_carryover == Decimal("1000.00")
        assert result.next_year_carryover == PfuCarryover.zero()
        # Surfaced for audit.
        assert len(result.expired) == 1
        assert result.expired[0].year == 2014

    def test_vintage_one_year_inside_window_still_usable(self):
        # 2015 vintage in 2024 → 9 years, still valid.
        prior = PfuCarryover(
            vintages=(
                PfuLossVintage(year=2015, amount=Decimal("500")),
            )
        )
        result = apply_pfu_carryover(
            [_disp(proceeds="2000", cost="1000")],
            prior,
            current_year=2024,
        )

        assert result.loss_used == Decimal("500.00")
        assert result.taxable_gain_after_carryover == Decimal("500.00")
        assert result.expired == ()


# ---------------------------------------------------------------------------
# Multi-year sequence
# ---------------------------------------------------------------------------


class TestMultiYearSequence:
    def test_loss_year_then_partial_use_then_full_use(self):
        # Year 1 (2024): loss of 1,000.
        a = apply_pfu_carryover(
            [_disp(proceeds="500", cost="1500")],
            current_year=2024,
        )
        assert a.next_year_carryover.total == Decimal("1000.00")

        # Year 2 (2025): gain of 600. Vintage drops to 400.
        b = apply_pfu_carryover(
            [_disp(proceeds="1600", cost="1000")],
            a.next_year_carryover,
            current_year=2025,
        )
        assert b.loss_used == Decimal("600.00")
        assert b.taxable_gain_after_carryover == Decimal("0.00")
        assert b.next_year_carryover.total == Decimal("400.00")
        # 2024 vintage still tagged as such.
        assert b.next_year_carryover.vintages[0].year == 2024

        # Year 3 (2026): gain of 400. Carryover fully consumed.
        c = apply_pfu_carryover(
            [_disp(proceeds="1400", cost="1000")],
            b.next_year_carryover,
            current_year=2026,
        )
        assert c.loss_used == Decimal("400.00")
        assert c.taxable_gain_after_carryover == Decimal("0.00")
        assert c.next_year_carryover == PfuCarryover.zero()
