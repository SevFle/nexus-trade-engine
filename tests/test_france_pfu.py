"""Tests for the France PFU summary (gh#155 follow-up)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from engine.core.tax.reports import (
    PFU_INCOME_TAX_RATE,
    PFU_SOCIAL_CHARGES_RATE,
    PFU_TOTAL_RATE,
    PfuDisposal,
    PfuSummary,
    summarize_pfu,
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
# Validation
# ---------------------------------------------------------------------------


class TestDisposalValidation:
    def test_acquired_after_disposed_rejected(self):
        with pytest.raises(ValueError):
            PfuDisposal(
                description="x",
                acquired=date(2024, 6, 1),
                disposed=date(2024, 5, 1),
                proceeds=Decimal("100"),
                cost=Decimal("80"),
            )

    def test_negative_proceeds_rejected(self):
        with pytest.raises(ValueError):
            PfuDisposal(
                description="x",
                acquired=date(2024, 1, 1),
                disposed=date(2024, 6, 1),
                proceeds=Decimal("-1"),
                cost=Decimal("80"),
            )

    def test_negative_cost_rejected(self):
        with pytest.raises(ValueError):
            PfuDisposal(
                description="x",
                acquired=date(2024, 1, 1),
                disposed=date(2024, 6, 1),
                proceeds=Decimal("100"),
                cost=Decimal("-1"),
            )

    def test_gain_loss_property_quantises(self):
        d = PfuDisposal(
            description="x",
            acquired=date(2024, 1, 1),
            disposed=date(2024, 6, 1),
            proceeds=Decimal("100.123"),
            cost=Decimal("50.456"),
        )
        assert d.gain_loss == Decimal("49.67")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_components_sum_to_thirty_percent(self):
        assert Decimal("0.128") == PFU_INCOME_TAX_RATE
        assert Decimal("0.172") == PFU_SOCIAL_CHARGES_RATE
        assert Decimal("0.300") == PFU_TOTAL_RATE


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


class TestEmpty:
    def test_empty_input_zero_summary(self):
        s = summarize_pfu([])

        assert isinstance(s, PfuSummary)
        assert s.disposal_count == 0
        assert s.net_gain == Decimal("0.00")
        assert s.net_loss == Decimal("0.00")
        assert s.taxable_gain == Decimal("0.00")
        assert s.income_tax == Decimal("0.00")
        assert s.social_charges == Decimal("0.00")
        assert s.total_tax == Decimal("0.00")


class TestNetGain:
    def test_pure_gain_taxed_at_thirty_percent_split(self):
        # +10,000 gain → 1,280 income tax + 1,720 social = 3,000.
        s = summarize_pfu([_disp(proceeds="20000", cost="10000")])

        assert s.net_gain == Decimal("10000.00")
        assert s.taxable_gain == Decimal("10000.00")
        assert s.income_tax == Decimal("1280.00")
        assert s.social_charges == Decimal("1720.00")
        assert s.total_tax == Decimal("3000.00")

    def test_multiple_gains_aggregate(self):
        s = summarize_pfu(
            [
                _disp(proceeds="6000", cost="5000"),  # +1,000
                _disp(proceeds="9000", cost="6000"),  # +3,000
            ]
        )

        assert s.disposal_count == 2
        assert s.net_gain == Decimal("4000.00")
        assert s.income_tax == Decimal("512.00")  # 4,000 * 0.128
        assert s.social_charges == Decimal("688.00")  # 4,000 * 0.172
        assert s.total_tax == Decimal("1200.00")

    def test_gain_offset_intra_year_then_taxed_on_net(self):
        # +5,000 gain - 2,000 loss = +3,000 net.
        s = summarize_pfu(
            [
                _disp(proceeds="10000", cost="5000"),
                _disp(proceeds="3000", cost="5000"),
            ]
        )

        assert s.net_gain == Decimal("3000.00")
        assert s.income_tax == Decimal("384.00")
        assert s.social_charges == Decimal("516.00")
        assert s.total_tax == Decimal("900.00")


class TestNetLoss:
    def test_pure_loss_year_emits_zero_tax(self):
        s = summarize_pfu([_disp(proceeds="500", cost="2000")])

        assert s.net_gain == Decimal("0.00")
        assert s.net_loss == Decimal("1500.00")
        assert s.taxable_gain == Decimal("0.00")
        assert s.total_tax == Decimal("0.00")

    def test_gains_offset_by_larger_losses_yields_loss_no_refund(self):
        s = summarize_pfu(
            [
                _disp(proceeds="6000", cost="4000"),  # +2,000
                _disp(proceeds="500", cost="6000"),  # -5,500
            ]
        )

        assert s.net_gain == Decimal("0.00")
        assert s.net_loss == Decimal("3500.00")
        assert s.income_tax == Decimal("0.00")
        assert s.social_charges == Decimal("0.00")
        assert s.total_tax == Decimal("0.00")
