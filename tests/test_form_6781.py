"""Tests for the Form 6781 Section 1256 summary (gh#155 follow-up)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from engine.core.tax.reports import (
    LONG_TERM_PCT,
    SHORT_TERM_PCT,
    Form6781Summary,
    Section1256Contract,
    contracts_to_csv,
    summarize_form6781,
)


def _contract(*, proceeds: str, cost: str) -> Section1256Contract:
    return Section1256Contract(
        description="ESH4 future",
        acquired=date(2024, 1, 1),
        closed_or_year_end=date(2024, 12, 31),
        proceeds_or_fmv=Decimal(proceeds),
        cost=Decimal(cost),
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestContractValidation:
    def test_acquired_after_closed_rejected(self):
        with pytest.raises(ValueError):
            Section1256Contract(
                description="x",
                acquired=date(2024, 12, 31),
                closed_or_year_end=date(2024, 1, 1),
                proceeds_or_fmv=Decimal("100"),
                cost=Decimal("80"),
            )

    def test_negative_proceeds_rejected(self):
        with pytest.raises(ValueError):
            Section1256Contract(
                description="x",
                acquired=date(2024, 1, 1),
                closed_or_year_end=date(2024, 12, 31),
                proceeds_or_fmv=Decimal("-1"),
                cost=Decimal("80"),
            )

    def test_negative_cost_rejected(self):
        with pytest.raises(ValueError):
            Section1256Contract(
                description="x",
                acquired=date(2024, 1, 1),
                closed_or_year_end=date(2024, 12, 31),
                proceeds_or_fmv=Decimal("100"),
                cost=Decimal("-1"),
            )

    def test_gain_loss_property_quantises(self):
        c = Section1256Contract(
            description="x",
            acquired=date(2024, 1, 1),
            closed_or_year_end=date(2024, 12, 31),
            proceeds_or_fmv=Decimal("100.123"),
            cost=Decimal("50.456"),
        )
        assert c.gain_loss == Decimal("49.67")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_60_40_split_constants_pinned(self):
        assert Decimal("0.60") == LONG_TERM_PCT
        assert Decimal("0.40") == SHORT_TERM_PCT
        assert Decimal("1.00") == LONG_TERM_PCT + SHORT_TERM_PCT


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


class TestEmpty:
    def test_empty_input_zero_summary(self):
        s = summarize_form6781([])

        assert isinstance(s, Form6781Summary)
        assert s.contract_count == 0
        assert s.proceeds_or_fmv_total == Decimal("0.00")
        assert s.cost_total == Decimal("0.00")
        assert s.net_gain_loss == Decimal("0.00")
        assert s.short_term_amount == Decimal("0.00")
        assert s.long_term_amount == Decimal("0.00")


class TestNetGain:
    def test_pure_gain_splits_60_40(self):
        # +10,000 net → 4,000 short + 6,000 long.
        s = summarize_form6781(
            [_contract(proceeds="60000", cost="50000")]
        )

        assert s.net_gain_loss == Decimal("10000.00")
        assert s.short_term_amount == Decimal("4000.00")
        assert s.long_term_amount == Decimal("6000.00")

    def test_multiple_contracts_aggregate_then_split(self):
        s = summarize_form6781(
            [
                _contract(proceeds="6000", cost="5000"),  # +1,000
                _contract(proceeds="9000", cost="6000"),  # +3,000
            ]
        )

        assert s.contract_count == 2
        assert s.net_gain_loss == Decimal("4000.00")
        assert s.short_term_amount == Decimal("1600.00")
        assert s.long_term_amount == Decimal("2400.00")

    def test_short_long_sum_back_to_net_gain(self):
        s = summarize_form6781(
            [_contract(proceeds="60001", cost="50000")]
        )

        # Both halves should add to the net within rounding.
        assert s.short_term_amount + s.long_term_amount == s.net_gain_loss


class TestNetLoss:
    def test_loss_splits_60_40_keeps_signs(self):
        # -10,000 net → -4,000 short + -6,000 long.
        s = summarize_form6781(
            [_contract(proceeds="40000", cost="50000")]
        )

        assert s.net_gain_loss == Decimal("-10000.00")
        assert s.short_term_amount == Decimal("-4000.00")
        assert s.long_term_amount == Decimal("-6000.00")
        assert s.short_term_amount + s.long_term_amount == s.net_gain_loss


class TestMixedSigns:
    def test_gain_offset_by_loss_then_split_on_net(self):
        # +5,000 gain - 8,000 loss = -3,000 net.
        s = summarize_form6781(
            [
                _contract(proceeds="10000", cost="5000"),  # +5,000
                _contract(proceeds="2000", cost="10000"),  # -8,000
            ]
        )

        assert s.net_gain_loss == Decimal("-3000.00")
        assert s.short_term_amount == Decimal("-1200.00")
        assert s.long_term_amount == Decimal("-1800.00")


# ---------------------------------------------------------------------------
# CSV serialisation
# ---------------------------------------------------------------------------


class TestCsv:
    def test_csv_header_and_per_contract_row(self):
        contracts = [
            Section1256Contract(
                description="ESH4 future",
                acquired=date(2024, 6, 1),
                closed_or_year_end=date(2024, 12, 31),
                proceeds_or_fmv=Decimal("60000"),
                cost=Decimal("50000"),
            ),
        ]

        out = contracts_to_csv(contracts)
        lines = out.strip().splitlines()
        assert lines[0].split(",") == [
            "description",
            "acquired",
            "closed_or_year_end",
            "proceeds_or_fmv",
            "cost",
            "gain_loss",
        ]
        # Dates ISO 8601, money 2 decimals, gain/loss = +10,000.
        assert "2024-06-01" in lines[1]
        assert "2024-12-31" in lines[1]
        assert ",60000.00," in lines[1]
        assert ",50000.00," in lines[1]
        assert lines[1].endswith(",10000.00")
