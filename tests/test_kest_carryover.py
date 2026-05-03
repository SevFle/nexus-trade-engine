"""Tests for the German Verlustvortrag carry-forward (gh#155 follow-up)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from engine.core.tax.reports import (
    AssetClass,
    KestApplication,
    KestCarryover,
    KestDisposal,
    apply_kest_carryover,
)


def _disp(
    *,
    proceeds: str,
    cost: str,
    asset_class: AssetClass = AssetClass.EQUITY,
) -> KestDisposal:
    return KestDisposal(
        description="100 ABC",
        acquired=date(2023, 6, 1),
        disposed=date(2024, 6, 1),
        proceeds=Decimal(proceeds),
        cost=Decimal(cost),
        asset_class=asset_class,
    )


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


class TestCarryoverConstructor:
    def test_zero_constructor_returns_both_legs_at_zero(self):
        c = KestCarryover.zero()
        assert c.equity == Decimal("0.00")
        assert c.other == Decimal("0.00")
        assert c.total == Decimal("0.00")

    def test_total_quantises_to_two_decimals(self):
        c = KestCarryover(equity=Decimal("100.123"), other=Decimal("50.456"))
        assert c.total == Decimal("150.58")


# ---------------------------------------------------------------------------
# No prior carry — straight passthrough
# ---------------------------------------------------------------------------


class TestNoPrior:
    def test_pure_gain_year_yields_no_next_year_carry(self):
        result = apply_kest_carryover([_disp(proceeds="6000", cost="1000")])

        assert isinstance(result, KestApplication)
        assert result.summary.equity_net == Decimal("5000.00")
        assert result.summary.taxable_income == Decimal("4000.00")
        assert result.next_year_carryover == KestCarryover.zero()

    def test_pure_equity_loss_year_carries_only_equity_bucket(self):
        result = apply_kest_carryover([_disp(proceeds="500", cost="2500")])

        assert result.summary.equity_net == Decimal("-2000.00")
        assert result.summary.total_tax == Decimal("0.00")
        assert result.next_year_carryover.equity == Decimal("2000.00")
        assert result.next_year_carryover.other == Decimal("0.00")

    def test_pure_other_loss_year_carries_only_other_bucket(self):
        result = apply_kest_carryover(
            [
                _disp(
                    proceeds="500",
                    cost="2500",
                    asset_class=AssetClass.OTHER,
                )
            ]
        )

        assert result.summary.other_net == Decimal("-2000.00")
        assert result.summary.total_tax == Decimal("0.00")
        assert result.next_year_carryover.other == Decimal("2000.00")
        assert result.next_year_carryover.equity == Decimal("0.00")


# ---------------------------------------------------------------------------
# Prior loss applied to current year
# ---------------------------------------------------------------------------


class TestPriorEquityCarry:
    def test_prior_equity_loss_offsets_current_equity_gain(self):
        # Prior 4,000 EUR equity loss + +5,000 EUR current equity gain
        # → 1,000 net equity. Below allowance → no tax, no carry.
        prior = KestCarryover(
            equity=Decimal("4000"), other=Decimal("0")
        )
        result = apply_kest_carryover(
            [_disp(proceeds="6000", cost="1000")], prior
        )

        assert result.summary.equity_net == Decimal("1000.00")
        assert result.summary.taxable_income == Decimal("0.00")
        assert result.summary.total_tax == Decimal("0.00")
        assert result.next_year_carryover == KestCarryover.zero()

    def test_prior_equity_loss_above_current_gain_carries_remainder(self):
        # Prior 8,000 + current +5,000 = -3,000 still on equity bucket.
        prior = KestCarryover(
            equity=Decimal("8000"), other=Decimal("0")
        )
        result = apply_kest_carryover(
            [_disp(proceeds="6000", cost="1000")], prior
        )

        assert result.summary.equity_net == Decimal("-3000.00")
        assert result.summary.total_tax == Decimal("0.00")
        assert result.next_year_carryover.equity == Decimal("3000.00")

    def test_prior_equity_loss_does_not_offset_other_gain(self):
        # Prior 10,000 equity carry + only "other" gain this year.
        # Equity ring-fence: prior equity does not reduce other base.
        prior = KestCarryover(
            equity=Decimal("10000"), other=Decimal("0")
        )
        result = apply_kest_carryover(
            [
                _disp(
                    proceeds="6000",
                    cost="1000",
                    asset_class=AssetClass.OTHER,
                )
            ],
            prior,
        )

        # Other bucket: +5,000; equity bucket after prior: -10,000.
        # Taxable: 5,000 - 1,000 allowance = 4,000.
        assert result.summary.equity_net == Decimal("-10000.00")
        assert result.summary.other_net == Decimal("5000.00")
        assert result.summary.taxable_income == Decimal("4000.00")
        # Equity loss survives untouched.
        assert result.next_year_carryover.equity == Decimal("10000.00")


class TestPriorOtherCarry:
    def test_prior_other_loss_offsets_current_other_gain(self):
        prior = KestCarryover(
            equity=Decimal("0"), other=Decimal("3000")
        )
        result = apply_kest_carryover(
            [
                _disp(
                    proceeds="5000",
                    cost="1000",
                    asset_class=AssetClass.OTHER,
                )
            ],
            prior,
        )

        # Other after prior: 4,000 - 3,000 = +1,000.
        # Allowance fully consumes; no tax, no carry.
        assert result.summary.other_net == Decimal("1000.00")
        assert result.summary.taxable_income == Decimal("0.00")
        assert result.next_year_carryover == KestCarryover.zero()

    def test_prior_other_loss_above_current_other_gain_carries_remainder(self):
        prior = KestCarryover(
            equity=Decimal("0"), other=Decimal("5000")
        )
        result = apply_kest_carryover(
            [
                _disp(
                    proceeds="2000",
                    cost="1000",
                    asset_class=AssetClass.OTHER,
                )
            ],
            prior,
        )

        # Other after prior: 1,000 - 5,000 = -4,000.
        assert result.next_year_carryover.other == Decimal("4000.00")
        assert result.next_year_carryover.equity == Decimal("0.00")


# ---------------------------------------------------------------------------
# Compound prior + current losses
# ---------------------------------------------------------------------------


class TestCompoundLosses:
    def test_prior_loss_plus_current_loss_compounds_carryover(self):
        prior = KestCarryover(
            equity=Decimal("2000"), other=Decimal("1000")
        )
        result = apply_kest_carryover(
            [
                _disp(proceeds="500", cost="1500"),  # -1,000 equity
                _disp(
                    proceeds="200",
                    cost="700",
                    asset_class=AssetClass.OTHER,
                ),  # -500 other
            ],
            prior,
        )

        # equity_after = -1,000 - 2,000 = -3,000.
        # other_after = -500 - 1,000 = -1,500.
        assert result.next_year_carryover.equity == Decimal("3000.00")
        assert result.next_year_carryover.other == Decimal("1500.00")
        assert result.summary.total_tax == Decimal("0.00")


# ---------------------------------------------------------------------------
# Disposal count preserved
# ---------------------------------------------------------------------------


class TestDisposalCount:
    def test_summary_disposal_count_matches_input_not_synthetic(self):
        # Even after the synthetic per-bucket disposals are fed through
        # summarize_kest, the surfaced count is the real input length.
        disposals = [
            _disp(proceeds="6000", cost="1000"),
            _disp(proceeds="3000", cost="2000"),
        ]
        result = apply_kest_carryover(disposals)
        assert result.summary.disposal_count == 2


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_negative_prior_equity_rejected(self):
        with pytest.raises(ValueError):
            apply_kest_carryover(
                [],
                KestCarryover(
                    equity=Decimal("-1"), other=Decimal("0")
                ),
            )

    def test_negative_prior_other_rejected(self):
        with pytest.raises(ValueError):
            apply_kest_carryover(
                [],
                KestCarryover(
                    equity=Decimal("0"), other=Decimal("-1")
                ),
            )
