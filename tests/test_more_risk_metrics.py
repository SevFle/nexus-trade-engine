"""Tests for Treynor / MAR / Sterling / K-Ratio extras (gh#97)."""

from __future__ import annotations

import math

import pytest

from engine.core.metrics_extras import (
    compute_k_ratio,
    compute_mar_ratio,
    compute_sterling_ratio,
    compute_treynor_ratio,
)


# ---------------------------------------------------------------------------
# Treynor
# ---------------------------------------------------------------------------


class TestTreynor:
    def test_known_value(self):
        # 12 % return, 5 % rf, beta 1.2 → (0.12 - 0.05) / 1.2 ≈ 0.0583
        assert compute_treynor_ratio(0.12, 0.05, 1.2) == pytest.approx(
            0.0583, rel=1e-3
        )

    def test_zero_beta_returns_zero(self):
        assert compute_treynor_ratio(0.12, 0.05, 0.0) == 0.0

    def test_negative_beta_yields_negative_treynor(self):
        # Inverse-correlated portfolio: +7 % excess / -1 beta = -0.07.
        assert compute_treynor_ratio(0.12, 0.05, -1.0) == pytest.approx(-0.07)

    def test_underperformance_yields_negative(self):
        # Returns less than rf with positive beta → negative Treynor.
        assert compute_treynor_ratio(0.03, 0.05, 1.0) == pytest.approx(-0.02)


# ---------------------------------------------------------------------------
# MAR
# ---------------------------------------------------------------------------


class TestMar:
    def test_known_value(self):
        # 25 % CAGR / 10 % max drawdown → 2.5
        assert compute_mar_ratio(25.0, 10.0) == pytest.approx(2.5)

    def test_zero_drawdown_returns_zero(self):
        assert compute_mar_ratio(25.0, 0.0) == 0.0

    def test_negative_drawdown_returns_zero(self):
        assert compute_mar_ratio(25.0, -5.0) == 0.0

    def test_loss_with_drawdown_negative_mar(self):
        # Loss CAGR with positive drawdown → negative ratio.
        assert compute_mar_ratio(-10.0, 20.0) == pytest.approx(-0.5)


# ---------------------------------------------------------------------------
# Sterling
# ---------------------------------------------------------------------------


class TestSterling:
    def test_known_value_default_floor(self):
        # 30 % CAGR / (15 % avg DD - 10 % floor) = 30 / 5 = 6.0
        assert compute_sterling_ratio(30.0, 15.0) == pytest.approx(6.0)

    def test_below_floor_returns_zero(self):
        # avg DD 5 % - 10 % floor = -5 % → undefined, returns 0.
        assert compute_sterling_ratio(30.0, 5.0) == 0.0

    def test_at_floor_returns_zero(self):
        # avg DD == floor → denominator zero → returns 0.
        assert compute_sterling_ratio(30.0, 10.0) == 0.0

    def test_custom_floor(self):
        # 20 % avg DD - 5 % floor = 15 % denominator → 30 / 15 = 2.0.
        assert compute_sterling_ratio(
            30.0, 20.0, drawdown_floor_pct=5.0
        ) == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# K-Ratio
# ---------------------------------------------------------------------------


class TestKRatio:
    def test_too_few_points_returns_zero(self):
        assert compute_k_ratio([100.0]) == 0.0
        assert compute_k_ratio([]) == 0.0

    def test_perfectly_smooth_growth_yields_huge_k_ratio(self):
        # A near-perfect exponential has tiny float-precision residuals
        # → K-Ratio is huge but finite. The helper's se=0 short-circuit
        # only triggers when residuals are literally zero.
        curve = [100.0 * (1.001 ** i) for i in range(50)]
        result = compute_k_ratio(curve)
        assert result > 1e6
        assert math.isfinite(result)

    def test_noisy_uptrend_yields_positive_k_ratio(self):
        # Trend + small noise → positive slope, finite SE → K-Ratio > 0.
        curve = [
            100.0 * (1.001 ** i) + (-1) ** i
            for i in range(50)
        ]
        assert compute_k_ratio(curve) > 0.0

    def test_downtrend_yields_negative_k_ratio(self):
        curve = [
            100.0 * (0.999 ** i) + (-1) ** i
            for i in range(50)
        ]
        assert compute_k_ratio(curve) < 0.0

    def test_non_positive_value_returns_zero(self):
        # Log undefined for non-positive equity values.
        assert compute_k_ratio([100.0, 0.0, 50.0]) == 0.0
        assert compute_k_ratio([100.0, -10.0, 50.0]) == 0.0

    def test_constant_equity_yields_zero(self):
        # Flat curve → log values constant → slope 0 → K-Ratio 0.
        curve = [100.0] * 20
        result = compute_k_ratio(curve)
        assert result == 0.0
        # Sanity: not inf/NaN.
        assert math.isfinite(result)
