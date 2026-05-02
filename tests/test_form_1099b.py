"""Unit tests for the IRS Form 1099-B / 8949 row generator (gh#155)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from engine.core.tax.reports import (
    HoldingTerm,
    LotDisposition,
    generate_1099b_rows,
    rows_to_csv,
)
from engine.core.tax.reports.form_1099b import to_date


# ---------------------------------------------------------------------------
# LotDisposition validation
# ---------------------------------------------------------------------------


class TestLotDispositionValidation:
    def test_acquired_after_sold_rejected(self):
        with pytest.raises(ValueError):
            LotDisposition(
                description="10 sh AAPL",
                acquired=date(2026, 6, 1),
                sold=date(2026, 5, 1),
                proceeds=Decimal("1000"),
                cost_basis=Decimal("900"),
            )

    def test_negative_proceeds_rejected(self):
        with pytest.raises(ValueError):
            LotDisposition(
                description="10 sh AAPL",
                acquired=date(2025, 1, 1),
                sold=date(2026, 1, 1),
                proceeds=Decimal("-1"),
                cost_basis=Decimal("900"),
            )

    def test_negative_basis_rejected(self):
        with pytest.raises(ValueError):
            LotDisposition(
                description="10 sh AAPL",
                acquired=date(2025, 1, 1),
                sold=date(2026, 1, 1),
                proceeds=Decimal("1000"),
                cost_basis=Decimal("-1"),
            )

    def test_negative_disallowed_rejected(self):
        with pytest.raises(ValueError):
            LotDisposition(
                description="10 sh AAPL",
                acquired=date(2025, 1, 1),
                sold=date(2026, 1, 1),
                proceeds=Decimal("1000"),
                cost_basis=Decimal("900"),
                wash_sale_disallowed=Decimal("-1"),
            )


# ---------------------------------------------------------------------------
# Holding term classification
# ---------------------------------------------------------------------------


class TestHoldingTerm:
    def test_short_term_under_one_year(self):
        rows = generate_1099b_rows(
            [
                LotDisposition(
                    description="10 sh AAPL",
                    acquired=date(2026, 1, 1),
                    sold=date(2026, 6, 1),
                    proceeds=Decimal("1100"),
                    cost_basis=Decimal("1000"),
                )
            ]
        )
        assert rows[0].term == HoldingTerm.SHORT_TERM

    def test_long_term_over_one_year(self):
        rows = generate_1099b_rows(
            [
                LotDisposition(
                    description="10 sh AAPL",
                    acquired=date(2024, 1, 1),
                    sold=date(2026, 1, 2),
                    proceeds=Decimal("1100"),
                    cost_basis=Decimal("1000"),
                )
            ]
        )
        assert rows[0].term == HoldingTerm.LONG_TERM

    def test_exactly_one_year_is_short_term(self):
        # IRS: must hold *more than* one year. Exactly 365 days = short.
        rows = generate_1099b_rows(
            [
                LotDisposition(
                    description="10 sh AAPL",
                    acquired=date(2025, 1, 1),
                    sold=date(2026, 1, 1),
                    proceeds=Decimal("1100"),
                    cost_basis=Decimal("1000"),
                )
            ]
        )
        assert rows[0].term == HoldingTerm.SHORT_TERM


# ---------------------------------------------------------------------------
# Gain / loss + adjustments
# ---------------------------------------------------------------------------


class TestGainLoss:
    def test_simple_gain(self):
        rows = generate_1099b_rows(
            [
                LotDisposition(
                    description="10 sh AAPL",
                    acquired=date(2025, 1, 1),
                    sold=date(2026, 6, 1),
                    proceeds=Decimal("1500"),
                    cost_basis=Decimal("1000"),
                )
            ]
        )
        assert rows[0].gain_loss == Decimal("500.00")
        assert rows[0].adjustment_codes == ""
        assert rows[0].adjustment_amount == Decimal("0")

    def test_simple_loss(self):
        rows = generate_1099b_rows(
            [
                LotDisposition(
                    description="10 sh AAPL",
                    acquired=date(2025, 1, 1),
                    sold=date(2026, 6, 1),
                    proceeds=Decimal("800"),
                    cost_basis=Decimal("1000"),
                )
            ]
        )
        assert rows[0].gain_loss == Decimal("-200.00")
        assert rows[0].adjustment_codes == ""

    def test_wash_sale_disallowed_zeros_loss(self):
        # 200 loss + 200 wash-sale add-back = 0 reportable.
        rows = generate_1099b_rows(
            [
                LotDisposition(
                    description="10 sh AAPL",
                    acquired=date(2025, 1, 1),
                    sold=date(2026, 6, 1),
                    proceeds=Decimal("800"),
                    cost_basis=Decimal("1000"),
                    wash_sale_disallowed=Decimal("200"),
                )
            ]
        )
        assert rows[0].gain_loss == Decimal("0.00")
        assert rows[0].adjustment_codes == "W"
        assert rows[0].adjustment_amount == Decimal("200")

    def test_partial_wash_sale_disallowance(self):
        # 200 loss, only 80 disallowed → reported loss = -120.
        rows = generate_1099b_rows(
            [
                LotDisposition(
                    description="10 sh AAPL",
                    acquired=date(2025, 1, 1),
                    sold=date(2026, 6, 1),
                    proceeds=Decimal("800"),
                    cost_basis=Decimal("1000"),
                    wash_sale_disallowed=Decimal("80"),
                )
            ]
        )
        assert rows[0].gain_loss == Decimal("-120.00")
        assert rows[0].adjustment_codes == "W"


class TestPreservesOrder:
    def test_rows_returned_in_input_order(self):
        dispositions = [
            LotDisposition(
                description=f"lot {i}",
                acquired=date(2025, 1, 1),
                sold=date(2026, 1, 1),
                proceeds=Decimal("100"),
                cost_basis=Decimal("90"),
                lot_id=f"L{i}",
            )
            for i in range(5)
        ]
        rows = generate_1099b_rows(dispositions)
        assert [r.lot_id for r in rows] == ["L0", "L1", "L2", "L3", "L4"]


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


class TestCsv:
    def test_header_columns(self):
        rows = generate_1099b_rows(
            [
                LotDisposition(
                    description="10 sh AAPL",
                    acquired=date(2025, 1, 1),
                    sold=date(2026, 1, 1),
                    proceeds=Decimal("100"),
                    cost_basis=Decimal("90"),
                    lot_id="L1",
                )
            ]
        )
        csv_text = rows_to_csv(rows)
        first = csv_text.splitlines()[0]
        assert first == (
            "description,acquired,sold,proceeds,cost_basis,"
            "adjustment_codes,adjustment_amount,gain_loss,term,lot_id"
        )

    def test_iso_dates(self):
        rows = generate_1099b_rows(
            [
                LotDisposition(
                    description="10 sh AAPL",
                    acquired=date(2025, 7, 4),
                    sold=date(2026, 11, 11),
                    proceeds=Decimal("100"),
                    cost_basis=Decimal("90"),
                )
            ]
        )
        csv_text = rows_to_csv(rows)
        body = csv_text.splitlines()[1]
        assert "2025-07-04" in body
        assert "2026-11-11" in body

    def test_amounts_quantize_to_two_decimals(self):
        rows = generate_1099b_rows(
            [
                LotDisposition(
                    description="x",
                    acquired=date(2025, 1, 1),
                    sold=date(2026, 1, 1),
                    proceeds=Decimal("100.123"),
                    cost_basis=Decimal("90.001"),
                )
            ]
        )
        body = rows_to_csv(rows).splitlines()[1]
        # Proceeds and basis appear quantised to 2dp.
        assert "100.12" in body
        assert "90.00" in body

    def test_empty_input(self):
        csv_text = rows_to_csv([])
        # Header-only is valid output.
        assert csv_text.splitlines() == [
            "description,acquired,sold,proceeds,cost_basis,"
            "adjustment_codes,adjustment_amount,gain_loss,term,lot_id"
        ]


# ---------------------------------------------------------------------------
# to_date helper
# ---------------------------------------------------------------------------


class TestToDate:
    def test_date_passes_through(self):
        d = date(2026, 5, 3)
        assert to_date(d) is d

    def test_datetime_truncates(self):
        dt = datetime(2026, 5, 3, 12, 34, 56)
        assert to_date(dt) == date(2026, 5, 3)

    def test_other_raises(self):
        with pytest.raises(TypeError):
            to_date("2026-05-03")
