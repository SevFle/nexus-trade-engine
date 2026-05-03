"""Tests for the Schedule D summary aggregator (gh#155 follow-up)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from engine.core.tax.reports import (
    HoldingTerm,
    LotDisposition,
    ScheduleDSummary,
    generate_1099b_rows,
    summarize_schedule_d,
    summary_to_csv,
)
from engine.core.tax.reports.form_1099b import Schedule1099BRow


def _row(
    *,
    proceeds: str,
    cost_basis: str,
    term: HoldingTerm,
    adj: str = "0",
    gain: str | None = None,
) -> Schedule1099BRow:
    proceeds_d = Decimal(proceeds)
    basis_d = Decimal(cost_basis)
    adj_d = Decimal(adj)
    if gain is None:
        gain_d = (proceeds_d - basis_d + adj_d).quantize(Decimal("0.01"))
    else:
        gain_d = Decimal(gain)
    return Schedule1099BRow(
        description="10 shares X",
        acquired=date(2024, 1, 1),
        sold=date(2024, 6, 1) if term == HoldingTerm.SHORT_TERM else date(2025, 6, 2),
        proceeds=proceeds_d,
        cost_basis=basis_d,
        adjustment_codes="W" if adj_d > 0 else "",
        adjustment_amount=adj_d,
        gain_loss=gain_d,
        term=term,
        lot_id=None,
    )


# ---------------------------------------------------------------------------
# Empty input
# ---------------------------------------------------------------------------


class TestEmptyInput:
    def test_empty_rows_produces_zeroed_summary(self):
        summary = summarize_schedule_d([])

        assert isinstance(summary, ScheduleDSummary)
        assert summary.short_term.row_count == 0
        assert summary.long_term.row_count == 0
        assert summary.short_term.gain_loss == Decimal("0.00")
        assert summary.long_term.gain_loss == Decimal("0.00")
        assert summary.net_gain_loss == Decimal("0.00")


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


class TestAggregation:
    def test_short_term_only_aggregates_into_part_i(self):
        rows = [
            _row(proceeds="100", cost_basis="80", term=HoldingTerm.SHORT_TERM),
            _row(proceeds="50", cost_basis="60", term=HoldingTerm.SHORT_TERM),
        ]

        summary = summarize_schedule_d(rows)

        assert summary.short_term.row_count == 2
        assert summary.short_term.proceeds == Decimal("150.00")
        assert summary.short_term.cost_basis == Decimal("140.00")
        # Gains: +20, -10 → net +10.
        assert summary.short_term.gain_loss == Decimal("10.00")
        # Long-term part empty.
        assert summary.long_term.row_count == 0
        assert summary.long_term.gain_loss == Decimal("0.00")
        assert summary.net_gain_loss == Decimal("10.00")

    def test_long_term_only_aggregates_into_part_ii(self):
        rows = [
            _row(proceeds="500", cost_basis="200", term=HoldingTerm.LONG_TERM),
            _row(proceeds="300", cost_basis="350", term=HoldingTerm.LONG_TERM),
        ]

        summary = summarize_schedule_d(rows)

        assert summary.long_term.row_count == 2
        # Gains: +300, -50 → net +250.
        assert summary.long_term.gain_loss == Decimal("250.00")
        assert summary.short_term.row_count == 0

    def test_mixed_terms_partition_correctly(self):
        rows = [
            _row(proceeds="100", cost_basis="80", term=HoldingTerm.SHORT_TERM),
            _row(proceeds="500", cost_basis="200", term=HoldingTerm.LONG_TERM),
            _row(proceeds="50", cost_basis="60", term=HoldingTerm.SHORT_TERM),
        ]

        summary = summarize_schedule_d(rows)

        assert summary.short_term.row_count == 2
        assert summary.long_term.row_count == 1
        # Short net: +20 -10 = +10. Long net: +300. Total: +310.
        assert summary.short_term.gain_loss == Decimal("10.00")
        assert summary.long_term.gain_loss == Decimal("300.00")
        assert summary.net_gain_loss == Decimal("310.00")

    def test_wash_sale_adjustment_is_summed_into_part_total(self):
        # A short-term loss with a $40 wash-sale disallow adjusts back
        # the loss. summarize_schedule_d trusts the per-row gain_loss,
        # but it must also surface the adjustment column total so a CPA
        # can reconcile the part against Form 8949 column g.
        rows = [
            _row(
                proceeds="60",
                cost_basis="100",
                adj="40",
                term=HoldingTerm.SHORT_TERM,
                # 60 - 100 + 40 = 0 net loss after wash adjustment.
                gain="0.00",
            ),
        ]

        summary = summarize_schedule_d(rows)

        assert summary.short_term.adjustment_amount == Decimal("40.00")
        assert summary.short_term.gain_loss == Decimal("0.00")
        assert summary.net_gain_loss == Decimal("0.00")


# ---------------------------------------------------------------------------
# Round-trip with the per-lot generator
# ---------------------------------------------------------------------------


class TestRoundTripWith1099BRows:
    def test_summarises_rows_produced_by_generate_1099b_rows(self):
        # Two short-term lots (held under a year) + one long-term lot.
        dispositions = [
            LotDisposition(
                description="10 shares AAPL",
                acquired=date(2024, 1, 1),
                sold=date(2024, 6, 1),
                proceeds=Decimal("1100"),
                cost_basis=Decimal("1000"),
            ),
            LotDisposition(
                description="5 shares AAPL",
                acquired=date(2024, 2, 1),
                sold=date(2024, 5, 1),
                proceeds=Decimal("400"),
                cost_basis=Decimal("500"),
            ),
            LotDisposition(
                description="3 shares MSFT",
                acquired=date(2022, 1, 1),
                sold=date(2024, 6, 1),  # > 1 year held
                proceeds=Decimal("900"),
                cost_basis=Decimal("600"),
            ),
        ]

        rows = generate_1099b_rows(dispositions)
        summary = summarize_schedule_d(rows)

        assert summary.short_term.row_count == 2
        assert summary.long_term.row_count == 1
        # Short-term net: +100 - 100 = 0. Long-term net: +300.
        assert summary.short_term.gain_loss == Decimal("0.00")
        assert summary.long_term.gain_loss == Decimal("300.00")
        assert summary.net_gain_loss == Decimal("300.00")


# ---------------------------------------------------------------------------
# CSV serialisation
# ---------------------------------------------------------------------------


class TestCsv:
    def test_csv_header_and_three_data_rows(self):
        rows = [
            _row(proceeds="100", cost_basis="80", term=HoldingTerm.SHORT_TERM),
            _row(proceeds="500", cost_basis="200", term=HoldingTerm.LONG_TERM),
        ]

        out = summary_to_csv(summarize_schedule_d(rows))

        lines = out.strip().splitlines()
        assert len(lines) == 4  # header + 3 rows
        assert lines[0].split(",") == [
            "section",
            "row_count",
            "proceeds",
            "cost_basis",
            "adjustment_amount",
            "gain_loss",
        ]
        assert lines[1].startswith("part_i_short_term,1,100.00")
        assert lines[2].startswith("part_ii_long_term,1,500.00")
        # Net row leaves the per-part dollar columns blank.
        assert lines[3].split(",") == ["net", "2", "", "", "", "320.00"]

    def test_csv_quantises_to_two_decimal_places(self):
        rows = [
            _row(
                proceeds="100.123",
                cost_basis="50.456",
                term=HoldingTerm.SHORT_TERM,
            ),
        ]

        out = summary_to_csv(summarize_schedule_d(rows))

        # Both money columns must be exactly 2 decimal places in the
        # rendered CSV — the engine keeps cents internally but Schedule
        # D rounds.
        assert ",100.12," in out
        assert ",50.46," in out
