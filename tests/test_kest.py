"""Tests for the KESt § 32d summary (gh#155 follow-up)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from engine.core.tax.reports import (
    CHURCH_TAX_RATE_BAYERN_BW,
    CHURCH_TAX_RATE_OTHER,
    KEST_RATE,
    SOLZ_RATE,
    SPARER_PAUSCHBETRAG_2024,
    SPARER_PAUSCHBETRAG_JOINT_2024,
    AssetClass,
    KestDisposal,
    summarize_kest,
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
# Validation
# ---------------------------------------------------------------------------


class TestDisposalValidation:
    def test_acquired_after_disposed_rejected(self):
        with pytest.raises(ValueError):
            KestDisposal(
                description="x",
                acquired=date(2024, 6, 1),
                disposed=date(2024, 5, 1),
                proceeds=Decimal("100"),
                cost=Decimal("80"),
            )

    def test_negative_amounts_rejected(self):
        with pytest.raises(ValueError):
            KestDisposal(
                description="x",
                acquired=date(2024, 1, 1),
                disposed=date(2024, 6, 1),
                proceeds=Decimal("-1"),
                cost=Decimal("80"),
            )
        with pytest.raises(ValueError):
            KestDisposal(
                description="x",
                acquired=date(2024, 1, 1),
                disposed=date(2024, 6, 1),
                proceeds=Decimal("100"),
                cost=Decimal("-1"),
            )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


class TestEmpty:
    def test_empty_input_zero_summary(self):
        s = summarize_kest([])

        assert s.disposal_count == 0
        assert s.proceeds_total == Decimal("0.00")
        assert s.cost_total == Decimal("0.00")
        assert s.taxable_income == Decimal("0.00")
        assert s.kest == Decimal("0.00")
        assert s.solidarity_surcharge == Decimal("0.00")
        assert s.church_tax == Decimal("0.00")
        assert s.total_tax == Decimal("0.00")


class TestAllowance:
    def test_gain_below_allowance_yields_no_tax(self):
        # +500 EUR equity gain — well under the 1,000 EUR Pauschbetrag.
        s = summarize_kest([_disp(proceeds="1500", cost="1000")])

        assert s.equity_net == Decimal("500.00")
        assert s.allowance_used == Decimal("500.00")
        assert s.taxable_income == Decimal("0.00")
        assert s.kest == Decimal("0.00")
        assert s.total_tax == Decimal("0.00")

    def test_gain_at_allowance_boundary_yields_no_tax(self):
        s = summarize_kest([_disp(proceeds="2000", cost="1000")])

        assert s.equity_net == Decimal("1000.00")
        assert s.allowance_used == Decimal("1000.00")
        assert s.taxable_income == Decimal("0.00")
        assert s.kest == Decimal("0.00")

    def test_gain_above_allowance_taxable_remainder(self):
        # +5,000 equity gain → 4,000 taxable after 1,000 allowance.
        # KESt = 1,000. SolZ = 55. Total = 1,055 (no church).
        s = summarize_kest([_disp(proceeds="6000", cost="1000")])

        assert s.equity_net == Decimal("5000.00")
        assert s.allowance_used == Decimal("1000.00")
        assert s.taxable_income == Decimal("4000.00")
        assert s.kest == Decimal("1000.00")
        assert s.solidarity_surcharge == Decimal("55.00")
        assert s.church_tax == Decimal("0.00")
        assert s.total_tax == Decimal("1055.00")

    def test_joint_allowance_doubles_default(self):
        # +1,500 equity gain. Single → 500 taxable; joint → 0 taxable.
        single = summarize_kest([_disp(proceeds="2500", cost="1000")])
        joint = summarize_kest(
            [_disp(proceeds="2500", cost="1000")],
            allowance=SPARER_PAUSCHBETRAG_JOINT_2024,
        )

        assert single.taxable_income == Decimal("500.00")
        assert joint.taxable_income == Decimal("0.00")


class TestEquityRingFence:
    def test_equity_loss_does_not_offset_other_gain(self):
        # -2,000 equity + +5,000 other = +5,000 taxable base
        # (equity ring-fenced for carry-forward).
        s = summarize_kest(
            [
                _disp(proceeds="500", cost="2500"),  # -2,000 equity
                _disp(
                    proceeds="6000",
                    cost="1000",
                    asset_class=AssetClass.OTHER,
                ),  # +5,000 other
            ]
        )

        assert s.equity_net == Decimal("-2000.00")
        assert s.other_net == Decimal("5000.00")
        # 5,000 - 1,000 allowance = 4,000 taxable.
        assert s.taxable_income == Decimal("4000.00")

    def test_equity_loss_only_keeps_total_tax_at_zero(self):
        s = summarize_kest([_disp(proceeds="500", cost="2500")])

        assert s.equity_net == Decimal("-2000.00")
        assert s.taxable_income == Decimal("0.00")
        assert s.total_tax == Decimal("0.00")
        # Equity loss survives as a negative bucket for the caller to
        # carry forward.
        assert s.equity_net < 0

    def test_other_loss_offsets_equity_gain(self):
        # +2,000 equity + (-500) other = +1,500 base.
        s = summarize_kest(
            [
                _disp(proceeds="3000", cost="1000"),  # +2,000 equity
                _disp(
                    proceeds="500",
                    cost="1000",
                    asset_class=AssetClass.OTHER,
                ),  # -500 other
            ]
        )

        assert s.equity_net == Decimal("2000.00")
        assert s.other_net == Decimal("-500.00")
        # Base = 2,000 + (-500) = 1,500; allowance fully consumes.
        assert s.taxable_income == Decimal("500.00")


class TestChurchTax:
    def test_bayern_bw_8_percent_added_on_kest(self):
        # +5,000 equity gain → 4,000 taxable, 1,000 KESt, 55 SolZ.
        # Church 8 % of KESt = 80. Total = 1,135.
        s = summarize_kest(
            [_disp(proceeds="6000", cost="1000")],
            church_tax_rate=CHURCH_TAX_RATE_BAYERN_BW,
        )

        assert s.kest == Decimal("1000.00")
        assert s.church_tax == Decimal("80.00")
        assert s.total_tax == Decimal("1135.00")

    def test_default_other_lander_9_percent(self):
        s = summarize_kest(
            [_disp(proceeds="6000", cost="1000")],
            church_tax_rate=CHURCH_TAX_RATE_OTHER,
        )

        assert s.church_tax == Decimal("90.00")
        assert s.total_tax == Decimal("1145.00")


class TestValidation:
    def test_negative_allowance_rejected(self):
        with pytest.raises(ValueError):
            summarize_kest([], allowance=Decimal("-1"))

    def test_negative_church_tax_rate_rejected(self):
        with pytest.raises(ValueError):
            summarize_kest([], church_tax_rate=Decimal("-0.01"))


class TestConstants:
    def test_kest_rate_25_percent(self):
        assert Decimal("0.25") == KEST_RATE

    def test_solz_rate_5_5_percent(self):
        assert Decimal("0.055") == SOLZ_RATE

    def test_2024_pauschbetrag_thousand_and_two_thousand(self):
        assert Decimal("1000.00") == SPARER_PAUSCHBETRAG_2024
        assert Decimal("2000.00") == SPARER_PAUSCHBETRAG_JOINT_2024
