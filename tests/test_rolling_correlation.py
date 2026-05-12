"""Tests for rolling correlation analytics (gh#89 follow-up)."""

from __future__ import annotations

import math

import pytest

from engine.core.rolling_correlation import (
    mean_pairwise_correlation,
    rolling_correlation,
    rolling_correlation_matrix,
)

# ---------------------------------------------------------------------------
# rolling_correlation
# ---------------------------------------------------------------------------


class TestRollingCorrelation:
    def test_empty_inputs_empty_output(self):
        assert rolling_correlation([], [], 3) == []

    def test_window_too_large_all_none(self):
        out = rolling_correlation([0.01, 0.02], [0.01, 0.02], 5)
        assert out == [None, None]

    def test_first_indices_none(self):
        out = rolling_correlation([1.0, 2.0, 3.0, 4.0], [1.0, 2.0, 3.0, 4.0], 3)
        assert out[:2] == [None, None]
        assert out[2] is not None

    def test_perfectly_correlated(self):
        a = [1.0, 2.0, 3.0, 4.0, 5.0]
        b = [2.0, 4.0, 6.0, 8.0, 10.0]
        out = rolling_correlation(a, b, 3)
        for v in out[2:]:
            assert v == pytest.approx(1.0)

    def test_perfectly_anti_correlated(self):
        a = [1.0, 2.0, 3.0, 4.0, 5.0]
        b = [5.0, 4.0, 3.0, 2.0, 1.0]
        out = rolling_correlation(a, b, 3)
        for v in out[2:]:
            assert v == pytest.approx(-1.0)

    def test_constant_series_zero_correlation(self):
        # Zero variance in one series → 0.0 short-circuit.
        a = [1.0, 1.0, 1.0, 1.0, 1.0]
        b = [1.0, 2.0, 3.0, 4.0, 5.0]
        out = rolling_correlation(a, b, 3)
        for v in out[2:]:
            assert v == 0.0

    def test_length_mismatch_rejected(self):
        with pytest.raises(ValueError, match="length mismatch"):
            rolling_correlation([1.0, 2.0], [1.0, 2.0, 3.0], 2)

    def test_window_one_rejected(self):
        with pytest.raises(ValueError, match="window must be"):
            rolling_correlation([1.0, 2.0], [1.0, 2.0], 1)

    def test_output_length_matches_input(self):
        out = rolling_correlation([1.0] * 10, [1.0] * 10, 3)
        assert len(out) == 10

    def test_regime_shift_visible(self):
        # First half perfectly correlated, second half anti-correlated.
        a = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
        b = [2.0, 4.0, 6.0, 8.0, 6.0, 4.0, 2.0, 0.0]
        out = rolling_correlation(a, b, 4)
        # First full-window correlation is +1.
        assert out[3] == pytest.approx(1.0)
        # Last window's correlation is −1 (anti-correlated final 4 bars).
        assert out[-1] == pytest.approx(-1.0)


# ---------------------------------------------------------------------------
# rolling_correlation_matrix
# ---------------------------------------------------------------------------


class TestRollingCorrelationMatrix:
    def test_empty_input(self):
        assert rolling_correlation_matrix({}, 3) == {}

    def test_single_series_diagonal_only(self):
        out = rolling_correlation_matrix({"a": [1.0, 2.0, 3.0, 4.0]}, 3)
        assert list(out.keys()) == ["a"]
        # Diagonal: None for first window-1, then 1.0 (variation present).
        assert out["a"]["a"][:2] == [None, None]
        assert out["a"]["a"][2] == 1.0

    def test_two_series_off_diagonal_symmetric(self):
        out = rolling_correlation_matrix(
            {"a": [1.0, 2.0, 3.0, 4.0], "b": [4.0, 3.0, 2.0, 1.0]}, 3
        )
        for idx in range(2, 4):
            ab = out["a"]["b"][idx]
            ba = out["b"]["a"][idx]
            assert ab is not None
            assert ba is not None
            assert math.isclose(ab, ba)

    def test_diagonal_is_one_for_varying_series(self):
        out = rolling_correlation_matrix(
            {"a": [1.0, 2.0, 3.0, 4.0]}, 3
        )
        for v in out["a"]["a"][2:]:
            assert v == 1.0

    def test_diagonal_is_zero_for_constant_window(self):
        # Constant series — variation == 0 → diagonal returns 0.0.
        out = rolling_correlation_matrix(
            {"a": [1.0, 1.0, 1.0, 1.0]}, 3
        )
        for v in out["a"]["a"][2:]:
            assert v == 0.0

    def test_unequal_lengths_rejected(self):
        with pytest.raises(ValueError, match="equal length"):
            rolling_correlation_matrix(
                {"a": [1.0, 2.0], "b": [1.0, 2.0, 3.0]}, 2
            )

    def test_window_validation(self):
        with pytest.raises(ValueError, match="window must be"):
            rolling_correlation_matrix({"a": [1.0, 2.0]}, 1)


# ---------------------------------------------------------------------------
# mean_pairwise_correlation
# ---------------------------------------------------------------------------


class TestMeanPairwiseCorrelation:
    def test_empty_returns_empty(self):
        assert mean_pairwise_correlation({}, 3) == []

    def test_single_series_returns_empty(self):
        # Single portfolio — no pairs to average.
        assert mean_pairwise_correlation({"a": [1.0, 2.0, 3.0]}, 3) == []

    def test_first_indices_none(self):
        out = mean_pairwise_correlation(
            {"a": [1.0, 2.0, 3.0, 4.0], "b": [1.0, 2.0, 3.0, 4.0]}, 3
        )
        assert out[:2] == [None, None]
        assert out[2] is not None

    def test_perfectly_correlated_group_yields_one(self):
        out = mean_pairwise_correlation(
            {
                "a": [1.0, 2.0, 3.0, 4.0],
                "b": [2.0, 4.0, 6.0, 8.0],
                "c": [3.0, 6.0, 9.0, 12.0],
            },
            3,
        )
        for v in out[2:]:
            assert v == pytest.approx(1.0)

    def test_mixed_correlation(self):
        # Two perfectly correlated and one anti-correlated.
        # Pairs: (a,b)=1, (a,c)=-1, (b,c)=-1 → mean = -1/3.
        out = mean_pairwise_correlation(
            {
                "a": [1.0, 2.0, 3.0, 4.0],
                "b": [2.0, 4.0, 6.0, 8.0],
                "c": [4.0, 3.0, 2.0, 1.0],
            },
            3,
        )
        # Last full-window value:
        assert out[-1] == pytest.approx(-1 / 3, abs=1e-9)

    def test_unequal_lengths_rejected(self):
        with pytest.raises(ValueError, match="equal length"):
            mean_pairwise_correlation(
                {"a": [1.0, 2.0], "b": [1.0, 2.0, 3.0]}, 2
            )

    def test_output_length_matches_series(self):
        out = mean_pairwise_correlation(
            {"a": [1.0] * 10, "b": [2.0] * 10}, 3
        )
        assert len(out) == 10

    def test_window_too_large_all_none(self):
        out = mean_pairwise_correlation(
            {"a": [1.0, 2.0], "b": [1.0, 2.0]}, 5
        )
        assert out == [None, None]
