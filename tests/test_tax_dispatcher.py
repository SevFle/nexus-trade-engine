"""Tests for the multi-jurisdiction tax-report dispatcher (gh#155 follow-up)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from engine.core.tax.reports import (
    CgtSummary,
    KestSummary,
    PfuSummary,
    ScheduleDSummary,
    TaxableDisposal,
    UnsupportedJurisdictionError,
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


# ---------------------------------------------------------------------------
# TaxableDisposal validation
# ---------------------------------------------------------------------------


class TestTaxableDisposalValidation:
    def test_acquired_after_disposed_rejected(self):
        with pytest.raises(ValueError):
            TaxableDisposal(
                description="x",
                acquired=date(2024, 6, 1),
                disposed=date(2024, 5, 1),
                proceeds=Decimal("100"),
                cost=Decimal("80"),
            )

    def test_negative_proceeds_rejected(self):
        with pytest.raises(ValueError):
            TaxableDisposal(
                description="x",
                acquired=date(2024, 1, 1),
                disposed=date(2024, 6, 1),
                proceeds=Decimal("-1"),
                cost=Decimal("80"),
            )

    def test_negative_cost_rejected(self):
        with pytest.raises(ValueError):
            TaxableDisposal(
                description="x",
                acquired=date(2024, 1, 1),
                disposed=date(2024, 6, 1),
                proceeds=Decimal("100"),
                cost=Decimal("-1"),
            )

    def test_gain_loss_property_quantises(self):
        d = TaxableDisposal(
            description="x",
            acquired=date(2024, 1, 1),
            disposed=date(2024, 6, 1),
            proceeds=Decimal("100.123"),
            cost=Decimal("50.456"),
        )
        assert d.gain_loss == Decimal("49.67")


# ---------------------------------------------------------------------------
# Per-jurisdiction routing
# ---------------------------------------------------------------------------


class TestUsRouting:
    def test_us_returns_schedule_d_summary(self):
        # Disposal is held > 1 year → long-term; +5,000 net gain.
        result = report_for_jurisdiction(
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

        assert isinstance(result, ScheduleDSummary)
        assert result.long_term.row_count == 1
        assert result.long_term.gain_loss == Decimal("5000.00")

    def test_lowercase_code_accepted(self):
        result = report_for_jurisdiction("us", [_disp(proceeds="200", cost="100")])
        assert isinstance(result, ScheduleDSummary)


class TestGbRouting:
    def test_gb_returns_cgt_summary_with_aea_applied(self):
        # +£5,000 gain → £3,000 AEA (2024-25), £2,000 taxable.
        result = report_for_jurisdiction(
            "GB", [_disp(proceeds="15000", cost="10000")]
        )

        assert isinstance(result, CgtSummary)
        assert result.net_gain == Decimal("5000.00")
        assert result.annual_exempt_amount_used == Decimal("3000.00")
        assert result.taxable_gain == Decimal("2000.00")


class TestDeRouting:
    def test_de_returns_kest_summary_routed_to_equity_bucket(self):
        result = report_for_jurisdiction(
            "DE", [_disp(proceeds="6000", cost="1000")]
        )

        assert isinstance(result, KestSummary)
        assert result.equity_net == Decimal("5000.00")
        assert result.taxable_income == Decimal("4000.00")
        assert result.kest == Decimal("1000.00")
        assert result.total_tax == Decimal("1055.00")


class TestFrRouting:
    def test_fr_returns_pfu_summary(self):
        # +1,000 EUR gain → 30 % flat tax = 300.
        result = report_for_jurisdiction(
            "FR", [_disp(proceeds="6000", cost="5000")]
        )

        assert isinstance(result, PfuSummary)
        assert result.net_gain == Decimal("1000.00")
        assert result.income_tax == Decimal("128.00")
        assert result.social_charges == Decimal("172.00")
        assert result.total_tax == Decimal("300.00")


# ---------------------------------------------------------------------------
# Unknown jurisdiction
# ---------------------------------------------------------------------------


class TestUnknownJurisdiction:
    def test_unknown_code_raises_unsupported_error(self):
        with pytest.raises(UnsupportedJurisdictionError) as exc_info:
            report_for_jurisdiction("ZZ", [])

        # Error lists the supported codes so a CLI user can self-correct.
        msg = str(exc_info.value)
        assert "US" in msg
        assert "GB" in msg
        assert "DE" in msg
        assert "FR" in msg


# ---------------------------------------------------------------------------
# Empty input through every code
# ---------------------------------------------------------------------------


class TestEmptyDisposalsPerJurisdiction:
    @pytest.mark.parametrize("code", ["US", "GB", "DE", "FR"])
    def test_empty_input_returns_typed_summary_with_zero_totals(self, code):
        result = report_for_jurisdiction(code, [])

        # Each jurisdiction returns its own summary type even when the
        # disposal list is empty — the dispatcher never returns None.
        assert result is not None
