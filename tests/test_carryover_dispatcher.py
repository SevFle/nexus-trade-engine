"""Tests for the multi-jurisdiction carryover dispatcher (gh#155 follow-up)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from engine.core.tax.reports import (
    CapitalLossApplication,
    CapitalLossCarryover,
    CgtApplication,
    CgtCarryover,
    KestApplication,
    KestCarryover,
    PfuApplication,
    PfuCarryover,
    TaxableDisposal,
    UnsupportedJurisdictionError,
    carryover_for_jurisdiction,
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
# Per-jurisdiction routing
# ---------------------------------------------------------------------------


class TestUsRouting:
    def test_us_returns_capital_loss_application(self):
        # Long-term loss: -2,000.
        disposals = [
            TaxableDisposal(
                description="3 shares MSFT",
                acquired=date(2022, 1, 1),
                disposed=date(2024, 6, 1),
                proceeds=Decimal("100"),
                cost=Decimal("2100"),
            )
        ]

        result = carryover_for_jurisdiction("US", disposals)

        assert isinstance(result, CapitalLossApplication)
        # 2,000 loss → fully deducted under the $3,000 cap.
        assert result.current_year_deduction == Decimal("2000.00")
        assert result.next_year_carryover == CapitalLossCarryover.zero()

    def test_us_prior_loss_compounds_within_cap(self):
        prior = CapitalLossCarryover(
            short_term=Decimal("2000"), long_term=Decimal("0")
        )
        result = carryover_for_jurisdiction("US", [], prior)

        assert isinstance(result, CapitalLossApplication)
        assert result.current_year_deduction == Decimal("2000.00")

    def test_us_wrong_prior_type_rejected(self):
        with pytest.raises(TypeError):
            carryover_for_jurisdiction("US", [], CgtCarryover.zero())


class TestGbRouting:
    def test_gb_returns_cgt_application(self):
        result = carryover_for_jurisdiction(
            "GB",
            [_disp(proceeds="20000", cost="10000")],
        )

        assert isinstance(result, CgtApplication)
        # +£10,000 → £7,000 post-AEA.
        assert result.summary.taxable_gain == Decimal("7000.00")
        assert result.taxable_gain_after_carryover == Decimal("7000.00")

    def test_gb_prior_loss_offsets_post_aea(self):
        prior = CgtCarryover(loss=Decimal("4000"))
        result = carryover_for_jurisdiction(
            "GB",
            [_disp(proceeds="20000", cost="10000")],
            prior,
        )

        assert result.carryover_loss_used == Decimal("4000.00")
        assert result.taxable_gain_after_carryover == Decimal("3000.00")

    def test_gb_wrong_prior_type_rejected(self):
        with pytest.raises(TypeError):
            carryover_for_jurisdiction(
                "GB", [], CapitalLossCarryover.zero()
            )


class TestDeRouting:
    def test_de_returns_kest_application(self):
        result = carryover_for_jurisdiction(
            "DE", [_disp(proceeds="6000", cost="1000")]
        )

        assert isinstance(result, KestApplication)
        # +5,000 → 4,000 taxable post-allowance.
        assert result.summary.taxable_income == Decimal("4000.00")
        assert result.summary.kest == Decimal("1000.00")

    def test_de_prior_equity_carryover_applied(self):
        prior = KestCarryover(
            equity=Decimal("4000"), other=Decimal("0")
        )
        result = carryover_for_jurisdiction(
            "DE",
            [_disp(proceeds="6000", cost="1000")],
            prior,
        )

        # Equity after prior: 5,000 - 4,000 = 1,000. Below allowance.
        assert result.summary.equity_net == Decimal("1000.00")
        assert result.summary.taxable_income == Decimal("0.00")

    def test_de_wrong_prior_type_rejected(self):
        with pytest.raises(TypeError):
            carryover_for_jurisdiction("DE", [], CgtCarryover.zero())


class TestFrRouting:
    def test_fr_requires_current_year(self):
        with pytest.raises(ValueError):
            carryover_for_jurisdiction("FR", [])

    def test_fr_returns_pfu_application(self):
        result = carryover_for_jurisdiction(
            "FR",
            [_disp(proceeds="6000", cost="5000")],
            current_year=2024,
        )

        assert isinstance(result, PfuApplication)
        assert result.taxable_gain_after_carryover == Decimal("1000.00")
        assert result.total_tax_after_carryover == Decimal("300.00")

    def test_fr_prior_carryover_applied(self):
        prior = PfuCarryover.zero()
        result = carryover_for_jurisdiction(
            "FR",
            [_disp(proceeds="500", cost="2000")],
            prior,
            current_year=2024,
        )

        # Loss year creates a new vintage tagged 2024.
        assert isinstance(result, PfuApplication)
        assert len(result.next_year_carryover.vintages) == 1
        assert result.next_year_carryover.vintages[0].year == 2024

    def test_fr_wrong_prior_type_rejected(self):
        with pytest.raises(TypeError):
            carryover_for_jurisdiction(
                "FR", [], KestCarryover.zero(), current_year=2024
            )


# ---------------------------------------------------------------------------
# Casing + unknown jurisdiction
# ---------------------------------------------------------------------------


class TestCasing:
    def test_lowercase_code_accepted(self):
        result = carryover_for_jurisdiction(
            "us", [_disp(proceeds="100", cost="200")]
        )
        assert isinstance(result, CapitalLossApplication)


class TestUnknown:
    def test_unknown_code_raises_unsupported_error(self):
        with pytest.raises(UnsupportedJurisdictionError):
            carryover_for_jurisdiction("ZZ", [])


# ---------------------------------------------------------------------------
# Forwarded kwargs
# ---------------------------------------------------------------------------


class TestKwargForwarding:
    def test_us_deductible_cap_forwarded(self):
        # MFS filing → $1,500 cap. -$5,000 loss → $1,500 deducted,
        # $3,500 carry.
        disposals = [
            TaxableDisposal(
                description="x",
                acquired=date(2023, 1, 1),
                disposed=date(2024, 1, 1),
                proceeds=Decimal("0"),
                cost=Decimal("5000"),
            )
        ]
        result = carryover_for_jurisdiction(
            "US", disposals, deductible_cap=Decimal("1500")
        )
        assert result.current_year_deduction == Decimal("1500.00")
        assert result.next_year_carryover.short_term == Decimal("3500.00")

    def test_gb_annual_exempt_amount_forwarded(self):
        # 2022-23 AEA was £12,300; +£10,000 gain → 0 taxable.
        result = carryover_for_jurisdiction(
            "GB",
            [_disp(proceeds="20000", cost="10000")],
            annual_exempt_amount=Decimal("12300"),
        )
        assert result.summary.taxable_gain == Decimal("0.00")

    def test_de_church_tax_rate_forwarded(self):
        result = carryover_for_jurisdiction(
            "DE",
            [_disp(proceeds="6000", cost="1000")],
            church_tax_rate=Decimal("0.09"),
        )
        # Church tax 9 % of KESt (1,000) = 90.
        assert result.summary.church_tax == Decimal("90.00")
