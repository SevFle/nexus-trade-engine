"""Tests for the Almgren-Chriss market-impact helpers (gh#96 follow-up)."""

from __future__ import annotations

import math

import pytest

from engine.core.market_impact import (
    DEFAULT_ETA,
    DEFAULT_PERMANENT_FRACTION,
    compute_permanent_impact,
    compute_temporary_impact,
    compute_total_market_impact,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_eta_pinned(self):
        assert DEFAULT_ETA == 0.314

    def test_permanent_fraction_pinned(self):
        assert DEFAULT_PERMANENT_FRACTION == 0.2


# ---------------------------------------------------------------------------
# Temporary impact
# ---------------------------------------------------------------------------


class TestTemporaryImpact:
    def test_zero_quantity_returns_zero(self):
        assert (
            compute_temporary_impact(0, 1_000_000, 0.02) == 0.0
        )

    def test_zero_volume_returns_zero(self):
        assert (
            compute_temporary_impact(1000, 0, 0.02) == 0.0
        )

    def test_zero_volatility_returns_zero(self):
        assert (
            compute_temporary_impact(1000, 1_000_000, 0) == 0.0
        )

    def test_known_value(self):
        # 100k shares against 10M ADV at 2% daily vol, η=0.314.
        # participation = 100k / 10M = 0.01
        # impact = 0.314 * 0.02 * sqrt(0.01) = 0.314 * 0.02 * 0.1
        #        = 0.000628
        out = compute_temporary_impact(
            100_000, 10_000_000, 0.02
        )
        assert out == pytest.approx(0.000628, rel=1e-9)

    def test_horizon_dilutes_impact(self):
        # Spreading the same order over 4 days should halve the
        # impact (sqrt(1/4) = 0.5).
        one_day = compute_temporary_impact(
            100_000, 10_000_000, 0.02, horizon_days=1.0
        )
        four_days = compute_temporary_impact(
            100_000, 10_000_000, 0.02, horizon_days=4.0
        )
        assert four_days == pytest.approx(one_day * 0.5, rel=1e-9)

    def test_quadrupling_quantity_doubles_impact(self):
        # sqrt(4) = 2 — square-root scaling with quantity.
        small = compute_temporary_impact(
            1000, 10_000_000, 0.02
        )
        large = compute_temporary_impact(
            4000, 10_000_000, 0.02
        )
        assert large == pytest.approx(small * 2.0, rel=1e-9)

    def test_eta_override(self):
        # Doubling η doubles the impact.
        baseline = compute_temporary_impact(
            1000, 10_000_000, 0.02, eta=0.314
        )
        doubled = compute_temporary_impact(
            1000, 10_000_000, 0.02, eta=0.628
        )
        assert doubled == pytest.approx(baseline * 2.0, rel=1e-9)

    def test_negative_quantity_rejected(self):
        with pytest.raises(ValueError):
            compute_temporary_impact(-1, 1_000_000, 0.02)

    def test_negative_volume_rejected(self):
        with pytest.raises(ValueError):
            compute_temporary_impact(100, -1, 0.02)

    def test_negative_volatility_rejected(self):
        with pytest.raises(ValueError):
            compute_temporary_impact(100, 1_000_000, -0.01)

    def test_zero_horizon_rejected(self):
        with pytest.raises(ValueError):
            compute_temporary_impact(100, 1_000_000, 0.02, horizon_days=0)

    def test_negative_horizon_rejected(self):
        with pytest.raises(ValueError):
            compute_temporary_impact(100, 1_000_000, 0.02, horizon_days=-1)


# ---------------------------------------------------------------------------
# Permanent impact
# ---------------------------------------------------------------------------


class TestPermanentImpact:
    def test_default_fraction(self):
        # Permanent = 20 % of temporary by default.
        assert compute_permanent_impact(0.001) == pytest.approx(0.0002)

    def test_custom_fraction(self):
        out = compute_permanent_impact(
            0.001, permanent_fraction=0.3
        )
        assert out == pytest.approx(0.0003)

    def test_zero_temporary_zero_permanent(self):
        assert compute_permanent_impact(0.0) == 0.0

    def test_negative_temporary_rejected(self):
        with pytest.raises(ValueError):
            compute_permanent_impact(-0.001)

    def test_negative_fraction_rejected(self):
        with pytest.raises(ValueError):
            compute_permanent_impact(0.001, permanent_fraction=-0.1)


# ---------------------------------------------------------------------------
# Total market impact (decomposition wrapper)
# ---------------------------------------------------------------------------


class TestTotalMarketImpact:
    def test_returns_three_components_summing_to_total(self):
        temp, perm, total = compute_total_market_impact(
            100_000, 10_000_000, 0.02
        )
        assert total == pytest.approx(temp + perm, rel=1e-9)

    def test_temp_matches_compute_temporary_alone(self):
        temp, _perm, _total = compute_total_market_impact(
            100_000, 10_000_000, 0.02
        )
        alone = compute_temporary_impact(100_000, 10_000_000, 0.02)
        assert temp == pytest.approx(alone)

    def test_perm_uses_permanent_fraction_kwarg(self):
        temp, perm, _total = compute_total_market_impact(
            100_000, 10_000_000, 0.02, permanent_fraction=0.25
        )
        assert perm == pytest.approx(temp * 0.25, rel=1e-9)

    def test_zero_quantity_zero_components(self):
        temp, perm, total = compute_total_market_impact(
            0, 10_000_000, 0.02
        )
        assert temp == 0.0
        assert perm == 0.0
        assert total == 0.0

    def test_full_decomposition_known_value(self):
        # 100k against 10M ADV at 2 % vol, η=0.314, perm_frac=0.2
        # temp = 0.314 * 0.02 * sqrt(0.01) = 0.000628
        # perm = 0.000628 * 0.2 = 0.0001256
        # total = 0.000628 + 0.0001256 = 0.0007536
        temp, perm, total = compute_total_market_impact(
            100_000, 10_000_000, 0.02
        )
        assert temp == pytest.approx(0.000628, rel=1e-9)
        assert perm == pytest.approx(0.0001256, rel=1e-9)
        assert total == pytest.approx(0.0007536, rel=1e-9)

    def test_basis_points_within_typical_range(self):
        # A 1 % participation should produce impact within tens of bps.
        _, _, total = compute_total_market_impact(
            100_000, 10_000_000, 0.02
        )
        # Sanity: ~7.5 bps for the example above.
        assert 0.0001 < total < 0.005
        assert math.isfinite(total)
