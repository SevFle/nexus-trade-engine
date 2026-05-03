"""Tests for Form 6781 Part II § 1092 straddle-loss limitation (gh#155)."""

from __future__ import annotations

import csv as _csv
import io
from decimal import Decimal

import pytest

from engine.core.tax.reports import (
    Form6781PartIISummary,
    StraddleLeg,
    legs_to_csv,
    summarize_form6781_part_ii,
)


def _leg(
    *,
    description: str = "ABC long call",
    loss: str,
    offset: str,
) -> StraddleLeg:
    return StraddleLeg(
        description=description,
        recognized_loss=Decimal(loss),
        unrecognized_offsetting_gain=Decimal(offset),
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestStraddleLegValidation:
    def test_negative_recognized_loss_rejected(self):
        with pytest.raises(ValueError):
            StraddleLeg(
                description="x",
                recognized_loss=Decimal("-1"),
                unrecognized_offsetting_gain=Decimal("0"),
            )

    def test_negative_offset_gain_rejected(self):
        with pytest.raises(ValueError):
            StraddleLeg(
                description="x",
                recognized_loss=Decimal("0"),
                unrecognized_offsetting_gain=Decimal("-1"),
            )

    def test_both_zero_allowed(self):
        # Degenerate but legal — operator may pass an empty stub.
        leg = StraddleLeg(
            description="x",
            recognized_loss=Decimal("0"),
            unrecognized_offsetting_gain=Decimal("0"),
        )
        assert leg.allowed_loss == Decimal("0.00")
        assert leg.deferred_loss == Decimal("0.00")


# ---------------------------------------------------------------------------
# Per-leg deferral mechanics
# ---------------------------------------------------------------------------


class TestLegMechanics:
    def test_loss_below_offset_fully_deferred(self):
        # Loss 100, offsetting gain 300 → entire loss deferred.
        leg = _leg(loss="100", offset="300")
        assert leg.allowed_loss == Decimal("0.00")
        assert leg.deferred_loss == Decimal("100.00")

    def test_loss_equal_offset_fully_deferred(self):
        leg = _leg(loss="500", offset="500")
        assert leg.allowed_loss == Decimal("0.00")
        assert leg.deferred_loss == Decimal("500.00")

    def test_loss_above_offset_excess_allowed(self):
        # Loss 1,000, offset 300 → 700 allowed, 300 deferred.
        leg = _leg(loss="1000", offset="300")
        assert leg.allowed_loss == Decimal("700.00")
        assert leg.deferred_loss == Decimal("300.00")

    def test_zero_offset_full_loss_allowed(self):
        # No offsetting unrealized gain → no deferral.
        leg = _leg(loss="500", offset="0")
        assert leg.allowed_loss == Decimal("500.00")
        assert leg.deferred_loss == Decimal("0.00")


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


class TestEmpty:
    def test_empty_input_zero_summary(self):
        s = summarize_form6781_part_ii([])

        assert isinstance(s, Form6781PartIISummary)
        assert s.leg_count == 0
        assert s.total_recognized_loss == Decimal("0.00")
        assert s.total_unrecognized_offsetting_gain == Decimal("0.00")
        assert s.total_allowed_loss == Decimal("0.00")
        assert s.total_deferred_loss == Decimal("0.00")


class TestAggregation:
    def test_per_leg_sums_independently(self):
        # No cross-pair offset under § 1092 outside identified-straddle
        # election (not modelled). Each leg's allowed/deferred is summed
        # independently.
        legs = [
            _leg(loss="1000", offset="300"),  # 700 allowed, 300 deferred
            _leg(loss="200", offset="500"),  # 0 allowed, 200 deferred
            _leg(loss="800", offset="0"),  # 800 allowed, 0 deferred
        ]
        s = summarize_form6781_part_ii(legs)

        assert s.leg_count == 3
        assert s.total_recognized_loss == Decimal("2000.00")
        assert s.total_unrecognized_offsetting_gain == Decimal("800.00")
        assert s.total_allowed_loss == Decimal("1500.00")
        assert s.total_deferred_loss == Decimal("500.00")
        # Sanity: allowed + deferred always equals recognized total.
        assert (
            s.total_allowed_loss + s.total_deferred_loss
            == s.total_recognized_loss
        )


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


class TestCsv:
    def test_csv_header_and_per_leg_row(self):
        legs = [_leg(description="ABC put", loss="1000", offset="300")]

        out = legs_to_csv(legs)
        rows = list(_csv.reader(io.StringIO(out)))
        assert len(rows) == 2
        assert rows[0] == [
            "description",
            "recognized_loss",
            "unrecognized_offsetting_gain",
            "allowed_loss",
            "deferred_loss",
        ]
        assert rows[1] == [
            "ABC put",
            "1000.00",
            "300.00",
            "700.00",
            "300.00",
        ]

    def test_csv_quantises_to_two_decimals(self):
        legs = [_leg(loss="100.123", offset="50.456")]

        out = legs_to_csv(legs)
        rows = list(_csv.reader(io.StringIO(out)))
        # Both columns rendered to exactly two decimals; allowed/
        # deferred derived from the same precision.
        assert rows[1][1] == "100.12"
        assert rows[1][2] == "50.46"
