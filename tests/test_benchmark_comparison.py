"""Tests for alpha/beta + capture ratio helpers (gh#97 follow-up)."""

from __future__ import annotations

import pytest

from engine.core.benchmark_comparison import (
    beta,
    capture_ratio,
    correlation,
    down_capture_ratio,
    jensen_alpha,
    up_capture_ratio,
)

# ---------------------------------------------------------------------------
# beta
# ---------------------------------------------------------------------------


class TestBeta:
    def test_empty_returns_zero(self):
        assert beta([], []) == 0.0

    def test_single_point_returns_zero(self):
        assert beta([0.01], [0.01]) == 0.0

    def test_zero_variance_benchmark_returns_zero(self):
        assert beta([0.01, 0.02, 0.03], [0.05, 0.05, 0.05]) == 0.0

    def test_perfect_correlation_unit_beta(self):
        bench = [0.01, -0.02, 0.03, -0.01, 0.02]
        port = list(bench)
        assert beta(port, bench) == pytest.approx(1.0)

    def test_amplified_beta_two(self):
        bench = [0.01, -0.02, 0.03, -0.01, 0.02]
        port = [2 * b for b in bench]
        assert beta(port, bench) == pytest.approx(2.0)

    def test_inverse_correlation_negative_beta(self):
        bench = [0.01, -0.02, 0.03, -0.01, 0.02]
        port = [-b for b in bench]
        assert beta(port, bench) == pytest.approx(-1.0)

    def test_orthogonal_zero_beta(self):
        bench = [1.0, -1.0, 1.0, -1.0]
        port = [1.0, 1.0, -1.0, -1.0]
        assert beta(port, bench) == pytest.approx(0.0, abs=1e-12)

    def test_length_mismatch_rejected(self):
        with pytest.raises(ValueError, match="length mismatch"):
            beta([0.01, 0.02], [0.01, 0.02, 0.03])


# ---------------------------------------------------------------------------
# jensen_alpha
# ---------------------------------------------------------------------------


class TestJensenAlpha:
    def test_empty_returns_zero(self):
        assert jensen_alpha([], []) == 0.0

    def test_single_point_returns_zero(self):
        assert jensen_alpha([0.01], [0.01]) == 0.0

    def test_perfect_replicator_zero_alpha(self):
        # Portfolio = benchmark with rf=0 → alpha = 0.
        bench = [0.01, -0.02, 0.03, -0.01, 0.02]
        port = list(bench)
        assert jensen_alpha(port, bench) == pytest.approx(0.0, abs=1e-12)

    def test_outperformance_positive_alpha(self):
        # Portfolio = benchmark + 0.001/day on every bar.
        bench = [0.01, -0.02, 0.03, -0.01, 0.02]
        port = [b + 0.001 for b in bench]
        assert jensen_alpha(port, bench) > 0

    def test_underperformance_negative_alpha(self):
        bench = [0.01, -0.02, 0.03, -0.01, 0.02]
        port = [b - 0.001 for b in bench]
        assert jensen_alpha(port, bench) < 0

    def test_annualisation_scales_alpha(self):
        # Doubling annualisation factor doubles annual alpha.
        bench = [0.01, -0.02, 0.03, -0.01, 0.02]
        port = [b + 0.001 for b in bench]
        a252 = jensen_alpha(port, bench, annualisation_factor=252)
        a504 = jensen_alpha(port, bench, annualisation_factor=504)
        assert a504 == pytest.approx(2 * a252, rel=1e-9)

    def test_zero_annualisation_rejected(self):
        with pytest.raises(ValueError, match="annualisation_factor"):
            jensen_alpha([0.01, 0.02], [0.01, 0.02], annualisation_factor=0)

    def test_length_mismatch_rejected(self):
        with pytest.raises(ValueError, match="length mismatch"):
            jensen_alpha([0.01], [0.01, 0.02])


# ---------------------------------------------------------------------------
# up_capture_ratio
# ---------------------------------------------------------------------------


class TestUpCaptureRatio:
    def test_empty_returns_zero(self):
        assert up_capture_ratio([], []) == 0.0

    def test_no_up_bars_returns_zero(self):
        # All benchmark bars non-positive — no up market exists.
        out = up_capture_ratio([0.01, 0.02], [-0.01, -0.02])
        assert out == 0.0

    def test_perfect_replicator_unit_capture(self):
        out = up_capture_ratio([0.01, 0.02, -0.01], [0.01, 0.02, -0.01])
        assert out == pytest.approx(1.0)

    def test_outperforms_in_up_market(self):
        # Portfolio doubles benchmark on up bars.
        out = up_capture_ratio([0.02, 0.04, -0.01], [0.01, 0.02, -0.01])
        assert out > 1.0

    def test_underperforms_in_up_market(self):
        out = up_capture_ratio([0.005, 0.01, -0.01], [0.01, 0.02, -0.01])
        assert 0.0 < out < 1.0

    def test_length_mismatch_rejected(self):
        with pytest.raises(ValueError, match="length mismatch"):
            up_capture_ratio([0.01], [0.01, 0.02])


# ---------------------------------------------------------------------------
# down_capture_ratio
# ---------------------------------------------------------------------------


class TestDownCaptureRatio:
    def test_empty_returns_zero(self):
        assert down_capture_ratio([], []) == 0.0

    def test_no_down_bars_returns_zero(self):
        out = down_capture_ratio([0.01, 0.02], [0.01, 0.02])
        assert out == 0.0

    def test_perfect_replicator_unit_capture(self):
        out = down_capture_ratio([-0.01, -0.02, 0.01], [-0.01, -0.02, 0.01])
        assert out == pytest.approx(1.0)

    def test_defensive_lower_capture(self):
        # Portfolio falls less than benchmark on down days — defensive.
        out = down_capture_ratio([-0.005, -0.01, 0.01], [-0.01, -0.02, 0.01])
        assert 0.0 < out < 1.0

    def test_amplified_drawdown(self):
        out = down_capture_ratio([-0.02, -0.04, 0.01], [-0.01, -0.02, 0.01])
        assert out > 1.0

    def test_length_mismatch_rejected(self):
        with pytest.raises(ValueError, match="length mismatch"):
            down_capture_ratio([0.01], [0.01, 0.02])


# ---------------------------------------------------------------------------
# capture_ratio
# ---------------------------------------------------------------------------


class TestCaptureRatio:
    def test_empty_returns_zero(self):
        assert capture_ratio([], []) == 0.0

    def test_no_down_bars_returns_zero(self):
        # Down capture undefined → capture_ratio 0.0 short-circuit.
        out = capture_ratio([0.02, 0.03], [0.01, 0.02])
        assert out == 0.0

    def test_perfect_replicator_one(self):
        bench = [0.01, -0.02, 0.03, -0.01, 0.02]
        out = capture_ratio(bench, bench)
        assert out == pytest.approx(1.0)

    def test_asymmetric_upside_above_one(self):
        # Up capture 1.5, down capture 0.5 → ratio 3.0.
        bench = [0.10, -0.10]
        port = [0.15, -0.05]  # capture 150% on up, 50% on down.
        out = capture_ratio(port, bench)
        assert out > 1.0


# ---------------------------------------------------------------------------
# correlation
# ---------------------------------------------------------------------------


class TestCorrelation:
    def test_empty_returns_zero(self):
        assert correlation([], []) == 0.0

    def test_single_point_returns_zero(self):
        assert correlation([0.01], [0.01]) == 0.0

    def test_perfect_correlation_one(self):
        out = correlation([1.0, 2.0, 3.0], [2.0, 4.0, 6.0])
        assert out == pytest.approx(1.0)

    def test_perfect_anti_correlation(self):
        out = correlation([1.0, 2.0, 3.0], [3.0, 2.0, 1.0])
        assert out == pytest.approx(-1.0)

    def test_zero_variance_input_zero(self):
        out = correlation([1.0, 1.0, 1.0], [1.0, 2.0, 3.0])
        assert out == 0.0

    def test_length_mismatch_rejected(self):
        with pytest.raises(ValueError, match="length mismatch"):
            correlation([0.01], [0.01, 0.02])
