"""Tests for ``flatten_summary_to_csv`` (gh#155 follow-up)."""

from __future__ import annotations

import csv as _csv
import io
from datetime import date
from decimal import Decimal

from engine.core.tax.reports import (
    TaxableDisposal,
    flatten_summary_to_csv,
    report_for_jurisdiction,
)


def _disp(*, proceeds: str, cost: str) -> TaxableDisposal:
    return TaxableDisposal(
        description="100 ABC",
        acquired=date(2023, 6, 1),
        disposed=date(2024, 6, 1),
        proceeds=Decimal(proceeds),
        cost=Decimal(cost),
    )


def _parse(out: str) -> tuple[list[str], list[str]]:
    reader = _csv.reader(io.StringIO(out))
    rows = list(reader)
    assert len(rows) == 2, "flattener emits exactly header + values"
    return rows[0], rows[1]


# ---------------------------------------------------------------------------
# Per-jurisdiction shape
# ---------------------------------------------------------------------------


class TestUsShape:
    def test_us_summary_flattens_with_dotted_part_columns(self):
        summary = report_for_jurisdiction(
            "US",
            [
                TaxableDisposal(
                    description="3 shares MSFT",
                    acquired=date(2022, 1, 1),
                    disposed=date(2024, 6, 1),
                    proceeds=Decimal("9000"),
                    cost=Decimal("4000"),
                )
            ],
        )

        header, values = _parse(flatten_summary_to_csv(summary))
        # Nested ScheduleDPartTotal fields surface as dotted columns.
        assert "short_term.row_count" in header
        assert "short_term.gain_loss" in header
        assert "long_term.row_count" in header
        assert "long_term.gain_loss" in header
        assert "net_gain_loss" in header

        idx = {col: i for i, col in enumerate(header)}
        assert values[idx["long_term.row_count"]] == "1"
        assert values[idx["long_term.gain_loss"]] == "5000.00"
        assert values[idx["net_gain_loss"]] == "5000.00"


class TestGbShape:
    def test_gb_summary_flattens_to_flat_columns(self):
        summary = report_for_jurisdiction(
            "GB", [_disp(proceeds="15000", cost="10000")]
        )

        header, values = _parse(flatten_summary_to_csv(summary))
        assert {
            "disposal_count",
            "proceeds_total",
            "cost_total",
            "net_gain",
            "net_loss",
            "annual_exempt_amount_used",
            "taxable_gain",
        } <= set(header)

        idx = {col: i for i, col in enumerate(header)}
        assert values[idx["net_gain"]] == "5000.00"
        assert values[idx["annual_exempt_amount_used"]] == "3000.00"
        assert values[idx["taxable_gain"]] == "2000.00"


class TestDeShape:
    def test_de_summary_flattens_with_kest_breakdown(self):
        summary = report_for_jurisdiction(
            "DE", [_disp(proceeds="6000", cost="1000")]
        )

        header, values = _parse(flatten_summary_to_csv(summary))
        idx = {col: i for i, col in enumerate(header)}
        assert values[idx["kest"]] == "1000.00"
        assert values[idx["solidarity_surcharge"]] == "55.00"
        assert values[idx["total_tax"]] == "1055.00"


class TestFrShape:
    def test_fr_summary_flattens_with_pfu_breakdown(self):
        summary = report_for_jurisdiction(
            "FR", [_disp(proceeds="6000", cost="5000")]
        )

        header, values = _parse(flatten_summary_to_csv(summary))
        idx = {col: i for i, col in enumerate(header)}
        assert values[idx["income_tax"]] == "128.00"
        assert values[idx["social_charges"]] == "172.00"
        assert values[idx["total_tax"]] == "300.00"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_same_input_yields_same_csv(self):
        summary = report_for_jurisdiction(
            "GB", [_disp(proceeds="15000", cost="10000")]
        )
        a = flatten_summary_to_csv(summary)
        b = flatten_summary_to_csv(summary)
        assert a == b
