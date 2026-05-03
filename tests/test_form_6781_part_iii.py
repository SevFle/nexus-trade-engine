"""Tests for IRS Form 6781 Part III year-end disclosure (gh#155)."""

from __future__ import annotations

import csv as _csv
import io
from datetime import date
from decimal import Decimal

import pytest

from engine.core.tax.reports import (
    Form6781PartIIISummary,
    YearEndPosition,
    positions_to_csv,
    summarize_form6781_part_iii,
)


def _pos(
    *,
    description: str = "ABC future",
    acquired: date = date(2024, 6, 1),
    year_end: date = date(2024, 12, 31),
    basis: str = "1000",
    fmv: str = "1500",
) -> YearEndPosition:
    return YearEndPosition(
        description=description,
        acquired=acquired,
        year_end=year_end,
        basis=Decimal(basis),
        year_end_fmv=Decimal(fmv),
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestPositionValidation:
    def test_acquired_after_year_end_rejected(self):
        with pytest.raises(ValueError):
            YearEndPosition(
                description="x",
                acquired=date(2025, 1, 1),
                year_end=date(2024, 12, 31),
                basis=Decimal("100"),
                year_end_fmv=Decimal("100"),
            )

    def test_negative_basis_rejected(self):
        with pytest.raises(ValueError):
            YearEndPosition(
                description="x",
                acquired=date(2024, 1, 1),
                year_end=date(2024, 12, 31),
                basis=Decimal("-1"),
                year_end_fmv=Decimal("100"),
            )

    def test_negative_fmv_rejected(self):
        with pytest.raises(ValueError):
            YearEndPosition(
                description="x",
                acquired=date(2024, 1, 1),
                year_end=date(2024, 12, 31),
                basis=Decimal("100"),
                year_end_fmv=Decimal("-1"),
            )


# ---------------------------------------------------------------------------
# Per-position derived properties
# ---------------------------------------------------------------------------


class TestPerPosition:
    def test_unrecognized_gain_when_fmv_above_basis(self):
        p = _pos(basis="1000", fmv="1500")
        assert p.unrecognized_gain == Decimal("500.00")
        assert p.has_unrecognized_gain

    def test_unrecognized_gain_zero_when_at_basis(self):
        p = _pos(basis="1000", fmv="1000")
        assert p.unrecognized_gain == Decimal("0.00")
        assert not p.has_unrecognized_gain

    def test_unrecognized_gain_zero_when_under_water(self):
        p = _pos(basis="1500", fmv="1000")
        # Loss positions never appear on Part III; the property
        # clamps at zero rather than going negative.
        assert p.unrecognized_gain == Decimal("0.00")
        assert not p.has_unrecognized_gain


# ---------------------------------------------------------------------------
# Aggregation + filter
# ---------------------------------------------------------------------------


class TestEmpty:
    def test_empty_input_zero_summary(self):
        s = summarize_form6781_part_iii([])

        assert isinstance(s, Form6781PartIIISummary)
        assert s.position_count == 0
        assert s.total_unrecognized_gain == Decimal("0.00")
        assert s.positions == ()


class TestFilter:
    def test_under_water_positions_dropped(self):
        positions = [
            _pos(description="A", basis="1000", fmv="1500"),
            _pos(description="B at-the-money", basis="1000", fmv="1000"),
            _pos(description="C under water", basis="1500", fmv="1000"),
        ]

        s = summarize_form6781_part_iii(positions)

        # Only the gain leg (A) survives the filter.
        assert s.position_count == 1
        assert s.total_unrecognized_gain == Decimal("500.00")
        assert len(s.positions) == 1
        assert s.positions[0].description == "A"

    def test_positions_sorted_oldest_first(self):
        positions = [
            _pos(
                description="newer",
                acquired=date(2024, 9, 1),
                basis="100",
                fmv="200",
            ),
            _pos(
                description="oldest",
                acquired=date(2024, 1, 1),
                basis="100",
                fmv="200",
            ),
            _pos(
                description="middle",
                acquired=date(2024, 6, 1),
                basis="100",
                fmv="200",
            ),
        ]

        s = summarize_form6781_part_iii(positions)

        assert [p.description for p in s.positions] == [
            "oldest",
            "middle",
            "newer",
        ]


class TestAggregateTotal:
    def test_total_quantises_to_two_decimals(self):
        positions = [
            _pos(basis="100.123", fmv="200.456"),
            _pos(basis="50.111", fmv="75.999"),
        ]
        s = summarize_form6781_part_iii(positions)

        # Each leg's gain is computed first then summed; the sum is
        # quantised. (100.333 + 25.888 = 126.22 after two-decimal round.)
        assert s.total_unrecognized_gain == Decimal("126.22")


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


class TestCsv:
    def test_csv_header_and_per_position_row(self):
        positions = [
            _pos(
                description="ABC future",
                acquired=date(2024, 6, 1),
                year_end=date(2024, 12, 31),
                basis="1000",
                fmv="1500",
            )
        ]

        out = positions_to_csv(positions)
        rows = list(_csv.reader(io.StringIO(out)))
        assert rows[0] == [
            "description",
            "acquired",
            "year_end",
            "basis",
            "year_end_fmv",
            "unrecognized_gain",
        ]
        assert rows[1] == [
            "ABC future",
            "2024-06-01",
            "2024-12-31",
            "1000.00",
            "1500.00",
            "500.00",
        ]

    def test_csv_includes_under_water_positions_for_audit(self):
        # The CSV is intentionally wider than the Form 6781 attached
        # statement so callers can audit the filter — under-water
        # positions show ``unrecognized_gain == 0.00`` rather than
        # being dropped.
        positions = [
            _pos(description="loser", basis="1500", fmv="1000"),
        ]

        out = positions_to_csv(positions)
        rows = list(_csv.reader(io.StringIO(out)))
        assert len(rows) == 2
        assert rows[1][-1] == "0.00"
