"""Tests for engine.core.portfolio_optimizer — MVO, risk parity, HRP, BL."""

from __future__ import annotations

import numpy as np
import pytest

from engine.core.portfolio_optimizer import (
    OptimizerError,
    black_litterman,
    hierarchical_risk_parity,
    mean_variance_optimization,
    risk_parity,
)


class TestMVO:
    def test_minimum_variance_diagonal_cov(self):
        cov = np.diag([1.0, 4.0, 9.0])
        w = mean_variance_optimization(cov=cov)
        expected_unnorm = np.array([1.0, 0.25, 1.0 / 9.0])
        expected = expected_unnorm / expected_unnorm.sum()
        np.testing.assert_allclose(w, expected, atol=1e-8)

    def test_min_variance_weights_sum_to_one(self):
        cov = np.array([[0.04, 0.01], [0.01, 0.09]])
        w = mean_variance_optimization(cov=cov)
        assert pytest.approx(1.0, abs=1e-8) == w.sum()

    def test_equal_variance_yields_equal_weights(self):
        cov = np.eye(4) * 0.05
        w = mean_variance_optimization(cov=cov)
        np.testing.assert_allclose(w, np.full(4, 0.25), atol=1e-8)

    def test_max_sharpe_tilts_toward_high_return(self):
        cov = np.eye(2) * 0.04
        mu = np.array([0.10, 0.05])
        w = mean_variance_optimization(cov=cov, expected_returns=mu)
        assert w[0] > w[1]
        assert pytest.approx(1.0, abs=1e-8) == w.sum()

    def test_rejects_non_square_cov(self):
        with pytest.raises(OptimizerError):
            mean_variance_optimization(cov=np.zeros((2, 3)))

    def test_rejects_singular_cov(self):
        with pytest.raises(OptimizerError):
            mean_variance_optimization(cov=np.zeros((3, 3)))


class TestRiskParity:
    def test_diagonal_cov_yields_inverse_vol_weights(self):
        cov = np.diag([0.04, 0.16])
        w = risk_parity(cov=cov)
        np.testing.assert_allclose(w, np.array([2.0 / 3.0, 1.0 / 3.0]), atol=1e-3)

    def test_weights_sum_to_one(self):
        # Diagonally-dominant cov — guarantees positive sigma_w for
        # positive w during the Spinu iteration.
        cov = np.array(
            [
                [0.20, 0.02, 0.01, 0.01],
                [0.02, 0.25, 0.02, 0.01],
                [0.01, 0.02, 0.30, 0.02],
                [0.01, 0.01, 0.02, 0.35],
            ]
        )
        w = risk_parity(cov=cov)
        assert pytest.approx(1.0, abs=1e-6) == w.sum()
        assert (w > 0).all()

    def test_equal_risk_contribution_property(self):
        cov = np.array(
            [
                [0.10, 0.02, 0.04],
                [0.02, 0.20, 0.05],
                [0.04, 0.05, 0.30],
            ]
        )
        w = risk_parity(cov=cov)
        rc = w * (cov @ w)
        for i in range(1, len(rc)):
            assert pytest.approx(rc[0], rel=1e-3) == rc[i]


class TestHRP:
    def test_hrp_diagonal_cov_inverse_vol(self):
        cov = np.diag([0.01, 0.04, 0.09, 0.16])
        w = hierarchical_risk_parity(cov=cov)
        assert (w > 0).all()
        np.testing.assert_allclose(w.sum(), 1.0, atol=1e-8)

    def test_hrp_weights_positive_and_sum_to_one(self):
        rng = np.random.default_rng(7)
        a = rng.standard_normal((50, 5))
        cov = a.T @ a / 49.0
        w = hierarchical_risk_parity(cov=cov)
        assert (w > 0).all()
        np.testing.assert_allclose(w.sum(), 1.0, atol=1e-8)

    def test_hrp_handles_two_assets(self):
        cov = np.array([[0.04, 0.01], [0.01, 0.09]])
        w = hierarchical_risk_parity(cov=cov)
        assert (w > 0).all()
        np.testing.assert_allclose(w.sum(), 1.0, atol=1e-8)


class TestBlackLitterman:
    def test_no_views_returns_prior(self):
        prior = np.array([0.05, 0.07, 0.06])
        cov = np.diag([0.04, 0.04, 0.04])
        post_mu, _ = black_litterman(
            prior_returns=prior,
            prior_cov=cov,
            views_p=np.zeros((0, 3)),
            views_q=np.zeros(0),
            view_uncertainty=np.zeros((0, 0)),
            tau=0.05,
        )
        np.testing.assert_allclose(post_mu, prior, atol=1e-8)

    def test_absolute_view_shifts_posterior_toward_view(self):
        prior = np.array([0.05, 0.05])
        cov = np.eye(2) * 0.04
        p = np.array([[1.0, 0.0]])
        q = np.array([0.10])
        omega = np.array([[0.001]])
        post_mu, _ = black_litterman(
            prior_returns=prior,
            prior_cov=cov,
            views_p=p,
            views_q=q,
            view_uncertainty=omega,
            tau=0.05,
        )
        assert 0.05 < post_mu[0] <= 0.10
        assert pytest.approx(prior[1], abs=1e-3) == post_mu[1]


class TestErrors:
    def test_mvo_negative_returns_input_passes(self):
        # Σ⁻¹μ-summing-to-zero degenerates the unconstrained tangency
        # solution. Verify a non-pathological mixed-sign vector still
        # produces a valid weight vector that sums to 1.
        cov = np.eye(2) * 0.04
        mu = np.array([-0.02, 0.05])
        w = mean_variance_optimization(cov=cov, expected_returns=mu)
        assert pytest.approx(1.0, abs=1e-8) == w.sum()

    def test_mvo_zero_sum_tangency_raises(self):
        # Anti-symmetric μ gives a zero-sum tangency solution (long
        # one asset, short the other in equal magnitude). Without a
        # long-only constraint the closed form is degenerate.
        cov = np.eye(2) * 0.04
        mu = np.array([-0.05, 0.05])
        with pytest.raises(OptimizerError):
            mean_variance_optimization(cov=cov, expected_returns=mu)

    def test_risk_parity_singular_cov_raises(self):
        with pytest.raises(OptimizerError):
            risk_parity(cov=np.zeros((3, 3)))

    def test_hrp_non_square_raises(self):
        with pytest.raises(OptimizerError):
            hierarchical_risk_parity(cov=np.zeros((2, 3)))


class TestNumericProperties:
    def test_mvo_weights_are_finite(self):
        cov = np.eye(5) * 0.05
        w = mean_variance_optimization(cov=cov)
        assert np.isfinite(w).all()

    def test_hrp_repeatable(self):
        cov = np.array([[0.04, 0.02], [0.02, 0.09]])
        w1 = hierarchical_risk_parity(cov=cov)
        w2 = hierarchical_risk_parity(cov=cov)
        np.testing.assert_allclose(w1, w2)


class TestInputValidation:
    def test_mvo_rejects_nan_in_cov(self):
        cov = np.array([[0.04, np.nan], [np.nan, 0.09]])
        with pytest.raises(OptimizerError, match="non-finite"):
            mean_variance_optimization(cov=cov)

    def test_mvo_rejects_inf_in_returns(self):
        cov = np.eye(2) * 0.04
        with pytest.raises(OptimizerError, match="non-finite"):
            mean_variance_optimization(
                cov=cov, expected_returns=np.array([np.inf, 0.05])
            )

    def test_risk_parity_raises_on_non_convergence(self):
        # max_iter=1 with the multi-asset convergent default cov should
        # not converge — the iteration needs more steps.
        cov = np.array(
            [
                [0.20, 0.02, 0.01, 0.01],
                [0.02, 0.25, 0.02, 0.01],
                [0.01, 0.02, 0.30, 0.02],
                [0.01, 0.01, 0.02, 0.35],
            ]
        )
        with pytest.raises(OptimizerError, match="not converge"):
            risk_parity(cov=cov, max_iter=1, tol=1e-12)

    def test_black_litterman_rejects_non_positive_tau(self):
        prior = np.array([0.05, 0.05])
        cov = np.eye(2) * 0.04
        with pytest.raises(OptimizerError, match="tau"):
            black_litterman(
                prior_returns=prior,
                prior_cov=cov,
                views_p=np.zeros((0, 2)),
                views_q=np.zeros(0),
                view_uncertainty=np.zeros((0, 0)),
                tau=0.0,
            )
