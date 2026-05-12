"""Tests for portfolio concentration + variance decomposition (gh#89 follow-up)."""

from __future__ import annotations

import pytest

from engine.core.portfolio_concentration import (
    effective_n,
    gini_coefficient,
    hhi,
    top_n_share,
    variance_decomposition,
)

# ---------------------------------------------------------------------------
# HHI
# ---------------------------------------------------------------------------


class TestHHI:
    def test_empty_returns_zero(self):
        assert hhi({}) == 0.0

    def test_single_holding_is_one(self):
        # Perfectly concentrated.
        assert hhi({"AAPL": 100.0}) == pytest.approx(1.0)

    def test_two_equal_holdings(self):
        # 0.5² + 0.5² = 0.5
        assert hhi({"a": 50.0, "b": 50.0}) == pytest.approx(0.5)

    def test_four_equal_holdings(self):
        # 4 · 0.25² = 0.25
        weights = {f"x{i}": 25.0 for i in range(4)}
        assert hhi(weights) == pytest.approx(0.25)

    def test_unequal_holdings(self):
        # 70/30 split → 0.7² + 0.3² = 0.58
        assert hhi({"a": 70.0, "b": 30.0}) == pytest.approx(0.58)

    def test_normalises_unscaled_input(self):
        # Same result regardless of scale.
        scaled = hhi({"a": 700.0, "b": 300.0})
        unscaled = hhi({"a": 7.0, "b": 3.0})
        assert scaled == pytest.approx(unscaled)

    def test_drops_non_positive_weights(self):
        out = hhi({"a": 50.0, "b": 50.0, "c": 0.0, "d": -10.0})
        # Only a and b counted → 0.5
        assert out == pytest.approx(0.5)

    def test_all_zero_returns_zero(self):
        assert hhi({"a": 0.0, "b": 0.0}) == 0.0


# ---------------------------------------------------------------------------
# effective_n
# ---------------------------------------------------------------------------


class TestEffectiveN:
    def test_empty_returns_zero(self):
        assert effective_n({}) == 0.0

    def test_single_holding_is_one(self):
        assert effective_n({"a": 100.0}) == pytest.approx(1.0)

    def test_n_equal_holdings(self):
        for n in (2, 5, 10, 100):
            weights = {f"x{i}": 1.0 for i in range(n)}
            assert effective_n(weights) == pytest.approx(float(n))

    def test_concentrated_portfolio_low_effective_n(self):
        # 90/10 split → effective_n = 1 / (0.81 + 0.01) ≈ 1.22
        out = effective_n({"a": 90.0, "b": 10.0})
        assert 1.2 < out < 1.3


# ---------------------------------------------------------------------------
# top_n_share
# ---------------------------------------------------------------------------


class TestTopNShare:
    def test_empty_returns_zero(self):
        assert top_n_share({}, 1) == 0.0

    def test_zero_n_returns_zero(self):
        assert top_n_share({"a": 100.0}, 0) == 0.0

    def test_negative_n_returns_zero(self):
        assert top_n_share({"a": 100.0}, -1) == 0.0

    def test_top_one(self):
        # Top-1 of 60/30/10 = 0.6
        assert top_n_share({"a": 60.0, "b": 30.0, "c": 10.0}, 1) == pytest.approx(0.6)

    def test_top_two(self):
        # Top-2 of 60/30/10 = 0.9
        assert top_n_share({"a": 60.0, "b": 30.0, "c": 10.0}, 2) == pytest.approx(0.9)

    def test_n_exceeds_holdings_caps_at_one(self):
        out = top_n_share({"a": 60.0, "b": 40.0}, 100)
        assert out == pytest.approx(1.0)

    def test_picks_largest_regardless_of_input_order(self):
        # Smallest holding listed first — function still returns top weights.
        out = top_n_share({"tiny": 1.0, "huge": 99.0}, 1)
        assert out == pytest.approx(0.99)


# ---------------------------------------------------------------------------
# Gini coefficient
# ---------------------------------------------------------------------------


class TestGini:
    def test_empty_returns_zero(self):
        assert gini_coefficient({}) == 0.0

    def test_single_holding_returns_zero(self):
        # Single holder is "perfectly equal" by convention here.
        assert gini_coefficient({"a": 100.0}) == 0.0

    def test_perfectly_equal_distribution(self):
        weights = {f"x{i}": 1.0 for i in range(10)}
        assert gini_coefficient(weights) == pytest.approx(0.0, abs=1e-12)

    def test_two_holdings_one_dominant(self):
        # 99/1 split — close to maximally unequal for n=2.
        out = gini_coefficient({"a": 99.0, "b": 1.0})
        # Max possible Gini for n=2 is 0.5 (perfect inequality).
        assert 0.45 < out <= 0.5

    def test_value_bounded_zero_one(self):
        # Skew-heavy 5-holding distribution.
        out = gini_coefficient({"a": 100.0, "b": 1.0, "c": 1.0, "d": 1.0, "e": 1.0})
        assert 0.0 <= out < 1.0

    def test_drops_non_positive_weights(self):
        equal_pos = {f"x{i}": 1.0 for i in range(5)}
        with_zeros = {**equal_pos, "z": 0.0, "n": -10.0}
        assert gini_coefficient(with_zeros) == pytest.approx(
            gini_coefficient(equal_pos)
        )


# ---------------------------------------------------------------------------
# variance_decomposition
# ---------------------------------------------------------------------------


class TestVarianceDecomposition:
    def test_empty_returns_all_zero(self):
        out = variance_decomposition([], [])
        assert all(v == 0.0 for v in out.values())

    def test_single_point_returns_all_zero(self):
        out = variance_decomposition([0.01], [0.01])
        assert all(v == 0.0 for v in out.values())

    def test_length_mismatch_returns_all_zero(self):
        out = variance_decomposition([0.01, 0.02], [0.01, 0.02, 0.03])
        assert all(v == 0.0 for v in out.values())

    def test_zero_variance_benchmark_returns_all_zero(self):
        out = variance_decomposition([0.01, -0.01, 0.02], [0.01, 0.01, 0.01])
        assert all(v == 0.0 for v in out.values())

    def test_perfect_correlation_yields_full_systematic(self):
        # Portfolio = 1 * benchmark → β = 1, var = systematic, idio = 0.
        bench = [0.01, -0.02, 0.03, -0.01, 0.02]
        port = list(bench)
        out = variance_decomposition(port, bench)
        assert out["beta"] == pytest.approx(1.0)
        assert out["systematic_variance"] == pytest.approx(out["total_variance"])
        assert out["idiosyncratic_variance"] == pytest.approx(0.0, abs=1e-12)
        assert out["r_squared"] == pytest.approx(1.0)

    def test_amplified_beta(self):
        # Portfolio = 2 * benchmark → β = 2.
        bench = [0.01, -0.02, 0.03, -0.01, 0.02]
        port = [2 * b for b in bench]
        out = variance_decomposition(port, bench)
        assert out["beta"] == pytest.approx(2.0)
        assert out["r_squared"] == pytest.approx(1.0)

    def test_inverse_correlation_negative_beta(self):
        bench = [0.01, -0.02, 0.03, -0.01, 0.02]
        port = [-b for b in bench]
        out = variance_decomposition(port, bench)
        assert out["beta"] == pytest.approx(-1.0)
        # Systematic = β² · var(B), still positive.
        assert out["systematic_variance"] > 0
        assert out["r_squared"] == pytest.approx(1.0)

    def test_orthogonal_returns_zero_beta(self):
        # Mean-zero anti-symmetric ⇒ covariance zero.
        bench = [1.0, -1.0, 1.0, -1.0]
        port = [1.0, 1.0, -1.0, -1.0]
        out = variance_decomposition(port, bench)
        assert out["beta"] == pytest.approx(0.0, abs=1e-12)
        assert out["systematic_variance"] == pytest.approx(0.0, abs=1e-12)
        # All variance is idiosyncratic.
        assert out["idiosyncratic_variance"] == pytest.approx(out["total_variance"])

    def test_idiosyncratic_never_negative(self):
        # Numerical edge case: even noisy inputs can't produce negative idio.
        bench = [0.01, -0.02, 0.03, -0.01, 0.02, 0.005, -0.015]
        port = [0.012, -0.018, 0.029, -0.013, 0.021, 0.003, -0.017]
        out = variance_decomposition(port, bench)
        assert out["idiosyncratic_variance"] >= 0.0

    def test_identity_holds(self):
        # var(P) = systematic + idiosyncratic (within float tolerance).
        bench = [0.01, -0.02, 0.03, -0.01, 0.02]
        port = [0.012, -0.018, 0.029, -0.013, 0.021]
        out = variance_decomposition(port, bench)
        assert out["systematic_variance"] + out["idiosyncratic_variance"] == pytest.approx(
            out["total_variance"], rel=1e-9
        )
