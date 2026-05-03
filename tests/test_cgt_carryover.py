"""Tests for the HMRC CGT loss carry-forward (gh#155 follow-up)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from engine.core.tax.reports import (
    CgtApplication,
    CgtCarryover,
    CgtDisposal,
    apply_cgt_carryover,
)
from engine.core.tax.reports.cgt_carryover import roll_forward


def _disp(*, proceeds: str, cost: str) -> CgtDisposal:
    return CgtDisposal(
        description="100 ABC.L",
        acquired=date(2023, 4, 6),
        disposed=date(2024, 4, 5),
        proceeds=Decimal(proceeds),
        cost=Decimal(cost),
    )


# ---------------------------------------------------------------------------
# Constructor / no-op
# ---------------------------------------------------------------------------


class TestCarryoverConstructor:
    def test_zero_constructor_returns_zero_loss(self):
        assert CgtCarryover.zero().loss == Decimal("0.00")

    def test_roll_forward_preserves_loss(self):
        c = CgtCarryover(loss=Decimal("123.456"))
        assert roll_forward(c).loss == Decimal("123.46")

    def test_roll_forward_rejects_negative_loss(self):
        with pytest.raises(ValueError):
            roll_forward(CgtCarryover(loss=Decimal("-1")))


# ---------------------------------------------------------------------------
# No prior — same-year mechanics
# ---------------------------------------------------------------------------


class TestNoPrior:
    def test_pure_gain_below_aea_no_taxable_no_carry(self):
        result = apply_cgt_carryover(
            [_disp(proceeds="12000", cost="10000")]
        )

        assert isinstance(result, CgtApplication)
        assert result.summary.net_gain == Decimal("2000.00")
        assert result.summary.taxable_gain == Decimal("0.00")
        assert result.taxable_gain_after_carryover == Decimal("0.00")
        assert result.carryover_loss_used == Decimal("0.00")
        assert result.next_year_carryover == CgtCarryover.zero()

    def test_pure_gain_above_aea_taxable_no_carry(self):
        # +£10,000 gain → £3,000 AEA, £7,000 taxable.
        result = apply_cgt_carryover(
            [_disp(proceeds="20000", cost="10000")]
        )

        assert result.summary.taxable_gain == Decimal("7000.00")
        assert result.taxable_gain_after_carryover == Decimal("7000.00")
        assert result.next_year_carryover.loss == Decimal("0.00")

    def test_pure_loss_year_carries_forward(self):
        result = apply_cgt_carryover(
            [_disp(proceeds="500", cost="2000")]
        )

        assert result.summary.net_loss == Decimal("1500.00")
        assert result.taxable_gain_after_carryover == Decimal("0.00")
        assert result.next_year_carryover.loss == Decimal("1500.00")


# ---------------------------------------------------------------------------
# Prior loss applied to current taxable gain
# ---------------------------------------------------------------------------


class TestPriorLossAfterAea:
    def test_prior_loss_does_not_eat_into_aea(self):
        # +£2,000 gain, all under AEA. Prior £5,000 loss should not be
        # consumed; AEA absorbs the whole gain.
        prior = CgtCarryover(loss=Decimal("5000"))
        result = apply_cgt_carryover(
            [_disp(proceeds="12000", cost="10000")], prior
        )

        assert result.summary.taxable_gain == Decimal("0.00")
        assert result.carryover_loss_used == Decimal("0.00")
        assert result.next_year_carryover.loss == Decimal("5000.00")

    def test_prior_loss_offsets_taxable_gain_above_aea(self):
        # +£10,000 gain → £7,000 taxable post-AEA.
        # Prior £4,000 loss → £3,000 taxable; £0 prior left.
        prior = CgtCarryover(loss=Decimal("4000"))
        result = apply_cgt_carryover(
            [_disp(proceeds="20000", cost="10000")], prior
        )

        assert result.summary.taxable_gain == Decimal("7000.00")
        assert result.carryover_loss_used == Decimal("4000.00")
        assert result.taxable_gain_after_carryover == Decimal("3000.00")
        assert result.next_year_carryover.loss == Decimal("0.00")

    def test_prior_loss_above_taxable_gain_carries_remainder(self):
        # +£10,000 gain → £7,000 taxable. Prior £15,000 loss absorbs
        # the £7,000 taxable; £8,000 prior left for next year.
        prior = CgtCarryover(loss=Decimal("15000"))
        result = apply_cgt_carryover(
            [_disp(proceeds="20000", cost="10000")], prior
        )

        assert result.carryover_loss_used == Decimal("7000.00")
        assert result.taxable_gain_after_carryover == Decimal("0.00")
        assert result.next_year_carryover.loss == Decimal("8000.00")


# ---------------------------------------------------------------------------
# Compounding losses
# ---------------------------------------------------------------------------


class TestCompoundLosses:
    def test_prior_plus_current_loss_compounds_carryover(self):
        # Prior £2,000 + current -£1,500 = £3,500 carryover.
        prior = CgtCarryover(loss=Decimal("2000"))
        result = apply_cgt_carryover(
            [_disp(proceeds="500", cost="2000")], prior
        )

        assert result.summary.net_loss == Decimal("1500.00")
        assert result.next_year_carryover.loss == Decimal("3500.00")

    def test_year_with_no_disposals_preserves_prior_intact(self):
        prior = CgtCarryover(loss=Decimal("4321.99"))
        result = apply_cgt_carryover([], prior)

        assert result.summary.disposal_count == 0
        assert result.next_year_carryover.loss == Decimal("4321.99")
        assert result.carryover_loss_used == Decimal("0.00")


# ---------------------------------------------------------------------------
# Custom AEA
# ---------------------------------------------------------------------------


class TestCustomAea:
    def test_prior_year_aea_consumed_first(self):
        # 2022-23 AEA was £12,300. +£10,000 gain → 0 taxable.
        # Prior loss should not be touched.
        prior = CgtCarryover(loss=Decimal("4000"))
        result = apply_cgt_carryover(
            [_disp(proceeds="20000", cost="10000")],
            prior,
            annual_exempt_amount=Decimal("12300"),
        )

        assert result.summary.taxable_gain == Decimal("0.00")
        assert result.carryover_loss_used == Decimal("0.00")
        assert result.next_year_carryover.loss == Decimal("4000.00")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_negative_aea_rejected(self):
        with pytest.raises(ValueError):
            apply_cgt_carryover([], annual_exempt_amount=Decimal("-1"))

    def test_negative_prior_loss_rejected(self):
        with pytest.raises(ValueError):
            apply_cgt_carryover(
                [], CgtCarryover(loss=Decimal("-0.01"))
            )
