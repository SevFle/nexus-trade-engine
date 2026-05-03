"""Tests for the HMRC CGT summary (gh#155 follow-up)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from engine.core.tax.reports import (
    ANNUAL_EXEMPT_AMOUNT_2024_25,
    CgtDisposal,
    CgtSummary,
    disposals_to_csv,
    summarize_cgt,
)


def _disp(*, proceeds: str, cost: str) -> CgtDisposal:
    return CgtDisposal(
        description="100 ABC.L",
        acquired=date(2023, 4, 6),
        disposed=date(2024, 4, 5),
        proceeds=Decimal(proceeds),
        cost=Decimal(cost),
    )


# ---------------------------------------------------------------------------
# Disposal validation
# ---------------------------------------------------------------------------


class TestDisposalValidation:
    def test_acquired_after_disposed_rejected(self):
        with pytest.raises(ValueError):
            CgtDisposal(
                description="x",
                acquired=date(2024, 6, 1),
                disposed=date(2024, 5, 1),
                proceeds=Decimal("100"),
                cost=Decimal("80"),
            )

    def test_negative_proceeds_rejected(self):
        with pytest.raises(ValueError):
            CgtDisposal(
                description="x",
                acquired=date(2024, 1, 1),
                disposed=date(2024, 6, 1),
                proceeds=Decimal("-1"),
                cost=Decimal("80"),
            )

    def test_negative_cost_rejected(self):
        with pytest.raises(ValueError):
            CgtDisposal(
                description="x",
                acquired=date(2024, 1, 1),
                disposed=date(2024, 6, 1),
                proceeds=Decimal("100"),
                cost=Decimal("-1"),
            )

    def test_gain_loss_property_quantises(self):
        d = CgtDisposal(
            description="x",
            acquired=date(2024, 1, 1),
            disposed=date(2024, 6, 1),
            proceeds=Decimal("100.123"),
            cost=Decimal("50.456"),
        )
        assert d.gain_loss == Decimal("49.67")


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


class TestEmpty:
    def test_empty_disposals_zero_summary(self):
        s = summarize_cgt([])

        assert isinstance(s, CgtSummary)
        assert s.disposal_count == 0
        assert s.proceeds_total == Decimal("0.00")
        assert s.cost_total == Decimal("0.00")
        assert s.net_gain == Decimal("0.00")
        assert s.net_loss == Decimal("0.00")
        assert s.annual_exempt_amount_used == Decimal("0.00")
        assert s.taxable_gain == Decimal("0.00")


class TestNetGainBelowAea:
    def test_aea_consumes_entire_gain_no_taxable(self):
        # £2,000 gain — well under the 2024-25 £3,000 AEA → no tax due.
        s = summarize_cgt([_disp(proceeds="12000", cost="10000")])

        assert s.net_gain == Decimal("2000.00")
        assert s.net_loss == Decimal("0.00")
        assert s.annual_exempt_amount_used == Decimal("2000.00")
        assert s.taxable_gain == Decimal("0.00")


class TestNetGainAboveAea:
    def test_aea_caps_then_remainder_is_taxable(self):
        # £10,000 gain → £3,000 AEA, £7,000 taxable.
        s = summarize_cgt([_disp(proceeds="20000", cost="10000")])

        assert s.net_gain == Decimal("10000.00")
        assert s.annual_exempt_amount_used == Decimal("3000.00")
        assert s.taxable_gain == Decimal("7000.00")

    def test_multiple_gains_aggregate(self):
        s = summarize_cgt(
            [
                _disp(proceeds="10000", cost="5000"),
                _disp(proceeds="8000", cost="6000"),
            ]
        )

        # Gross gain: 5000 + 2000 = 7000.
        assert s.disposal_count == 2
        assert s.net_gain == Decimal("7000.00")
        assert s.annual_exempt_amount_used == Decimal("3000.00")
        assert s.taxable_gain == Decimal("4000.00")


class TestNetLoss:
    def test_pure_loss_year_no_aea_consumed_no_taxable(self):
        s = summarize_cgt([_disp(proceeds="500", cost="2000")])

        assert s.net_gain == Decimal("0.00")
        assert s.net_loss == Decimal("1500.00")
        # AEA not consumed when there is no net gain.
        assert s.annual_exempt_amount_used == Decimal("0.00")
        assert s.taxable_gain == Decimal("0.00")

    def test_gains_offset_by_larger_losses_yields_loss(self):
        # +2,000 gain - 5,000 loss = -3,000 net.
        s = summarize_cgt(
            [
                _disp(proceeds="7000", cost="5000"),
                _disp(proceeds="1000", cost="6000"),
            ]
        )

        assert s.net_gain == Decimal("0.00")
        assert s.net_loss == Decimal("3000.00")
        assert s.annual_exempt_amount_used == Decimal("0.00")
        assert s.taxable_gain == Decimal("0.00")


class TestCustomAea:
    def test_prior_year_aea_passed_in_explicitly(self):
        # 2022-23 AEA was £12,300 — gain of £10,000 falls fully under it.
        s = summarize_cgt(
            [_disp(proceeds="20000", cost="10000")],
            annual_exempt_amount=Decimal("12300"),
        )

        assert s.net_gain == Decimal("10000.00")
        assert s.annual_exempt_amount_used == Decimal("10000.00")
        assert s.taxable_gain == Decimal("0.00")

    def test_negative_aea_rejected(self):
        with pytest.raises(ValueError):
            summarize_cgt([], annual_exempt_amount=Decimal("-1"))


class TestConstants:
    def test_2024_25_aea_is_three_thousand(self):
        assert ANNUAL_EXEMPT_AMOUNT_2024_25 == Decimal("3000.00")


# ---------------------------------------------------------------------------
# CSV serialisation
# ---------------------------------------------------------------------------


class TestCsv:
    def test_csv_header_and_row_per_disposal(self):
        disposals = [
            CgtDisposal(
                description="100 ABC.L",
                acquired=date(2024, 1, 5),
                disposed=date(2024, 6, 30),
                proceeds=Decimal("1500"),
                cost=Decimal("1000"),
            ),
        ]

        out = disposals_to_csv(disposals)
        lines = out.strip().splitlines()
        assert lines[0].split(",") == [
            "description",
            "acquired",
            "disposed",
            "proceeds",
            "cost",
            "gain_loss",
        ]
        # Dates ISO 8601, money quantised to two decimals.
        assert "2024-01-05" in lines[1]
        assert "2024-06-30" in lines[1]
        assert ",1500.00," in lines[1]
        assert lines[1].endswith(",500.00")
