"""Tests for rolling alpha/beta + IR/TE time series (gh#97 follow-up)."""

from __future__ import annotations

import pytest

from engine.core.rolling_benchmark import (
    DEFAULT_ANNUALISATION,
    rolling_alpha,
    rolling_beta,
    rolling_information_ratio,
    rolling_tracking_error,
)


# ---------------------------------------------------------------------------
# rolling_beta
# ---------------------------------------------------------------------------


class TestRollingBeta:
    def test_empty_returns_empty(self):
        assert rolling_beta([], [], 3) == []

    def test_window_too_large_all_none(self):
        assert rolling_beta([0.01, 0.02], [0.01, 0.02], 5) == [None, None]

    def test_first_indices_none(self):
        out = rolling_beta(
            [0.01, 0.02, 0.03, 0.04, 0.05],
            [0.01, 0.02, 0.03, 0.04, 0.05],
            3,
        )
        assert out[:2] == [None, None]
        assert out[2] is not None

    def test_unit_beta_for_replicator(self):
        bench = [0.01, -0.02, 0.03, -0.01, 0.02]
        port = list(bench)
        out = rolling_beta(port, bench, 3)
        for v in out[2:]:
            assert v == pytest.approx(1.0)

    def test_amplified_beta_two(self):
        bench = [0.01, -0.02, 0.03, -0.01, 0.02]
        port = [2 * b for b in bench]
        out = rolling_beta(port, bench, 3)
        for v in out[2:]:
            assert v == pytest.approx(2.0)

    def test_inverse_beta_negative(self):
        bench = [0.01, -0.02, 0.03, -0.01, 0.02]
        port = [-b for b in bench]
        out = rolling_beta(port, bench, 3)
        for v in out[2:]:
            assert v == pytest.approx(-1.0)

    def test_constant_benchmark_zero_beta(self):
        out = rolling_beta(
            [0.01, 0.02, 0.03], [0.05, 0.05, 0.05], 3
        )
        assert out[2] == 0.0

    def test_length_mismatch_rejected(self):
        with pytest.raises(ValueError, match="length mismatch"):
            rolling_beta([0.01, 0.02], [0.01, 0.02, 0.03], 2)

    def test_window_one_rejected(self):
        with pytest.raises(ValueError, match="window must be"):
            rolling_beta([0.01, 0.02], [0.01, 0.02], 1)


# ---------------------------------------------------------------------------
# rolling_alpha
# ---------------------------------------------------------------------------


class TestRollingAlpha:
    def test_empty_returns_empty(self):
        assert rolling_alpha([], [], 3) == []

    def test_first_indices_none(self):
        out = rolling_alpha(
            [0.01, 0.02, 0.03], [0.01, 0.02, 0.03], 3
        )
        assert out[:2] == [None, None]

    def test_perfect_replicator_zero_alpha(self):
        bench = [0.01, -0.02, 0.03, -0.01, 0.02]
        port = list(bench)
        out = rolling_alpha(port, bench, 3)
        for v in out[2:]:
            assert v == pytest.approx(0.0, abs=1e-12)

    def test_outperformance_positive_alpha(self):
        bench = [0.01, -0.02, 0.03, -0.01, 0.02]
        port = [b + 0.001 for b in bench]
        out = rolling_alpha(port, bench, 3)
        for v in out[2:]:
            assert v > 0

    def test_underperformance_negative_alpha(self):
        bench = [0.01, -0.02, 0.03, -0.01, 0.02]
        port = [b - 0.001 for b in bench]
        out = rolling_alpha(port, bench, 3)
        for v in out[2:]:
            assert v < 0

    def test_zero_annualisation_rejected(self):
        with pytest.raises(ValueError, match="annualisation_factor"):
            rolling_alpha([0.01, 0.02], [0.01, 0.02], 2, annualisation_factor=0)

    def test_length_mismatch_rejected(self):
        with pytest.raises(ValueError, match="length mismatch"):
            rolling_alpha([0.01], [0.01, 0.02], 2)

    def test_default_annualisation(self):
        assert DEFAULT_ANNUALISATION == 252


# ---------------------------------------------------------------------------
# rolling_tracking_error
# ---------------------------------------------------------------------------


class TestRollingTrackingError:
    def test_empty_returns_empty(self):
        assert rolling_tracking_error([], [], 3) == []

    def test_first_indices_none(self):
        out = rolling_tracking_error(
            [0.01, 0.02, 0.03], [0.01, 0.02, 0.03], 3
        )
        assert out[:2] == [None, None]
        assert out[2] is not None

    def test_perfect_replicator_zero_te(self):
        bench = [0.01, -0.02, 0.03, -0.01, 0.02]
        port = list(bench)
        out = rolling_tracking_error(port, bench, 3)
        for v in out[2:]:
            assert v == pytest.approx(0.0, abs=1e-12)

    def test_constant_active_zero_te(self):
        # Constant excess return → no variance → TE 0.
        out = rolling_tracking_error(
            [0.05, 0.05, 0.05, 0.05],
            [0.02, 0.02, 0.02, 0.02],
            3,
        )
        for v in out[2:]:
            assert v == pytest.approx(0.0, abs=1e-12)

    def test_non_constant_active_positive_te(self):
        out = rolling_tracking_error(
            [0.10, -0.05, 0.10, -0.05],
            [0.05, 0.05, 0.05, 0.05],
            3,
        )
        for v in out[2:]:
            assert v > 0

    def test_zero_annualisation_rejected(self):
        with pytest.raises(ValueError, match="annualisation_factor"):
            rolling_tracking_error(
                [0.01, 0.02], [0.01, 0.02], 2, annualisation_factor=0
            )

    def test_length_mismatch_rejected(self):
        with pytest.raises(ValueError, match="length mismatch"):
            rolling_tracking_error([0.01], [0.01, 0.02], 2)


# ---------------------------------------------------------------------------
# rolling_information_ratio
# ---------------------------------------------------------------------------


class TestRollingInformationRatio:
    def test_empty_returns_empty(self):
        assert rolling_information_ratio([], [], 3) == []

    def test_first_indices_none(self):
        out = rolling_information_ratio(
            [0.01, 0.02, 0.03], [0.01, 0.02, 0.03], 3
        )
        assert out[:2] == [None, None]
        assert out[2] is not None

    def test_perfect_replicator_zero_ir(self):
        # Active is all zero → constant → SD 0 → IR 0 short-circuit.
        bench = [0.01, -0.02, 0.03, -0.01, 0.02]
        port = list(bench)
        out = rolling_information_ratio(port, bench, 3)
        for v in out[2:]:
            assert v == 0.0

    def test_consistent_outperformance_zero_ir(self):
        # Constant active → zero variance → IR 0.0 (degenerate).
        out = rolling_information_ratio(
            [0.02, 0.02, 0.02, 0.02],
            [0.01, 0.01, 0.01, 0.01],
            3,
        )
        for v in out[2:]:
            assert v == 0.0

    def test_volatile_outperformance_positive_ir(self):
        # Uniformly above benchmark with varying excess → every window
        # has positive mean active and non-zero SD → IR > 0.
        out = rolling_information_ratio(
            [0.06, 0.04, 0.05, 0.07, 0.06],
            [0.01, 0.01, 0.01, 0.01, 0.01],
            3,
        )
        for v in out[2:]:
            assert v is not None
            assert v > 0

    def test_length_mismatch_rejected(self):
        with pytest.raises(ValueError, match="length mismatch"):
            rolling_information_ratio([0.01], [0.01, 0.02], 2)
