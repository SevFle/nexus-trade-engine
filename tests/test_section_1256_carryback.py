"""Tests for the Section 1256 § 1212(c) 3-year carryback (gh#155)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from engine.core.tax.reports import (
    CARRYBACK_YEARS,
    CarrybackAbsorption,
    PriorYearNetGain,
    Section1256Carryback,
    apply_section_1256_carryback,
)


# ---------------------------------------------------------------------------
# Constants / record validation
# ---------------------------------------------------------------------------


class TestPriorYearGain:
    def test_negative_net_gain_rejected(self):
        with pytest.raises(ValueError):
            PriorYearNetGain(year=2022, net_gain=Decimal("-1"))

    def test_zero_net_gain_allowed(self):
        # Zero gain leg has no carryback capacity but is a valid record.
        p = PriorYearNetGain(year=2022, net_gain=Decimal("0"))
        assert p.net_gain == Decimal("0")


class TestConstants:
    def test_three_year_window(self):
        assert CARRYBACK_YEARS == 3


# ---------------------------------------------------------------------------
# Zero / no-op
# ---------------------------------------------------------------------------


class TestNoOp:
    def test_zero_loss_yields_empty_record(self):
        result = apply_section_1256_carryback(Decimal("0"), [])

        assert isinstance(result, Section1256Carryback)
        assert result.loss_absorbed == Decimal("0.00")
        assert result.per_year == ()
        assert result.forward_carry == Decimal("0.00")

    def test_no_prior_years_carries_full_loss_forward(self):
        result = apply_section_1256_carryback(
            Decimal("5000"), []
        )

        assert result.loss_absorbed == Decimal("0.00")
        assert result.per_year == ()
        assert result.forward_carry == Decimal("5000.00")

    def test_negative_loss_input_rejected(self):
        with pytest.raises(ValueError):
            apply_section_1256_carryback(
                Decimal("-100"),
                [PriorYearNetGain(year=2022, net_gain=Decimal("500"))],
            )


# ---------------------------------------------------------------------------
# Single-year absorption
# ---------------------------------------------------------------------------


class TestSingleYear:
    def test_loss_below_prior_gain_fully_absorbed(self):
        result = apply_section_1256_carryback(
            Decimal("2000"),
            [PriorYearNetGain(year=2022, net_gain=Decimal("5000"))],
        )

        assert result.loss_absorbed == Decimal("2000.00")
        assert result.per_year == (
            CarrybackAbsorption(year=2022, amount=Decimal("2000.00")),
        )
        assert result.forward_carry == Decimal("0.00")

    def test_loss_above_prior_gain_carries_remainder(self):
        result = apply_section_1256_carryback(
            Decimal("8000"),
            [PriorYearNetGain(year=2022, net_gain=Decimal("3000"))],
        )

        assert result.loss_absorbed == Decimal("3000.00")
        assert result.per_year == (
            CarrybackAbsorption(year=2022, amount=Decimal("3000.00")),
        )
        assert result.forward_carry == Decimal("5000.00")

    def test_zero_prior_gain_skipped(self):
        result = apply_section_1256_carryback(
            Decimal("1000"),
            [PriorYearNetGain(year=2022, net_gain=Decimal("0"))],
        )

        # Zero-capacity prior year produces no absorption row.
        assert result.loss_absorbed == Decimal("0.00")
        assert result.per_year == ()
        assert result.forward_carry == Decimal("1000.00")


# ---------------------------------------------------------------------------
# Three-year FIFO absorption
# ---------------------------------------------------------------------------


class TestThreeYearFifo:
    def test_oldest_year_absorbs_first_then_next(self):
        # 2021 has 2,000; 2022 has 1,500; 2023 has 5,000.
        # Loss of 6,000 → 2021 takes 2,000, 2022 takes 1,500,
        # 2023 takes 2,500. Nothing carries forward.
        result = apply_section_1256_carryback(
            Decimal("6000"),
            [
                PriorYearNetGain(year=2023, net_gain=Decimal("5000")),
                PriorYearNetGain(year=2021, net_gain=Decimal("2000")),
                PriorYearNetGain(year=2022, net_gain=Decimal("1500")),
            ],
        )

        assert [a.year for a in result.per_year] == [2021, 2022, 2023]
        assert [a.amount for a in result.per_year] == [
            Decimal("2000.00"),
            Decimal("1500.00"),
            Decimal("2500.00"),
        ]
        assert result.loss_absorbed == Decimal("6000.00")
        assert result.forward_carry == Decimal("0.00")

    def test_loss_above_total_capacity_carries_remainder(self):
        result = apply_section_1256_carryback(
            Decimal("20000"),
            [
                PriorYearNetGain(year=2021, net_gain=Decimal("2000")),
                PriorYearNetGain(year=2022, net_gain=Decimal("1500")),
                PriorYearNetGain(year=2023, net_gain=Decimal("5000")),
            ],
        )

        assert result.loss_absorbed == Decimal("8500.00")
        assert result.forward_carry == Decimal("11500.00")

    def test_more_than_three_years_supplied_keeps_most_recent_three(self):
        # Five prior years; only the most recent 3 (2021-2023) eligible.
        # Loss 1,000 fully absorbed by 2021's 5,000.
        result = apply_section_1256_carryback(
            Decimal("1000"),
            [
                PriorYearNetGain(year=2019, net_gain=Decimal("9999")),
                PriorYearNetGain(year=2020, net_gain=Decimal("9999")),
                PriorYearNetGain(year=2021, net_gain=Decimal("5000")),
                PriorYearNetGain(year=2022, net_gain=Decimal("3000")),
                PriorYearNetGain(year=2023, net_gain=Decimal("2000")),
            ],
        )

        # Years older than the 3-year window are dropped silently.
        assert [a.year for a in result.per_year] == [2021]
        assert result.loss_absorbed == Decimal("1000.00")
        assert result.forward_carry == Decimal("0.00")


class TestPartialAbsorption:
    def test_absorption_stops_when_loss_exhausted(self):
        # 2021 has 10,000; 2022 has 10,000; 2023 has 10,000.
        # Loss of 7,500 → 2021 takes the whole 7,500, others untouched.
        result = apply_section_1256_carryback(
            Decimal("7500"),
            [
                PriorYearNetGain(year=2021, net_gain=Decimal("10000")),
                PriorYearNetGain(year=2022, net_gain=Decimal("10000")),
                PriorYearNetGain(year=2023, net_gain=Decimal("10000")),
            ],
        )

        assert len(result.per_year) == 1
        assert result.per_year[0].year == 2021
        assert result.per_year[0].amount == Decimal("7500.00")
        assert result.forward_carry == Decimal("0.00")
