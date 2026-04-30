"""Tests for engine.core.monte_carlo — bootstrap robustness analysis."""

from __future__ import annotations

import numpy as np
import pytest

from engine.core.monte_carlo import (
    MonteCarloError,
    SimulationStats,
    block_bootstrap,
    bootstrap_returns,
    max_drawdown,
)


class TestMaxDrawdown:
    def test_monotonic_up_zero_drawdown(self):
        eq = np.array([1.0, 1.1, 1.2, 1.3])
        assert max_drawdown(eq) == pytest.approx(0.0)

    def test_50pct_drop(self):
        eq = np.array([1.0, 2.0, 1.0])
        assert max_drawdown(eq) == pytest.approx(0.5)

    def test_recovery_remembers_worst_drop(self):
        eq = np.array([1.0, 2.0, 1.5, 2.5])
        assert max_drawdown(eq) == pytest.approx(0.25)

    def test_empty_array_returns_zero(self):
        assert max_drawdown(np.array([])) == pytest.approx(0.0)


class TestBootstrap:
    def test_returns_simulation_stats(self):
        rng = np.random.default_rng(42)
        returns = rng.normal(0.001, 0.01, size=200)
        out = bootstrap_returns(returns, n_simulations=500, seed=7)
        assert isinstance(out, SimulationStats)

    def test_seeded_deterministic(self):
        returns = np.array([0.01, -0.005, 0.002, 0.015, -0.01])
        a = bootstrap_returns(returns, n_simulations=100, seed=42)
        b = bootstrap_returns(returns, n_simulations=100, seed=42)
        assert a.mean_total_return == b.mean_total_return
        assert a.p5_total_return == b.p5_total_return

    def test_different_seeds_differ(self):
        returns = np.array([0.01, -0.005, 0.002, 0.015, -0.01])
        a = bootstrap_returns(returns, n_simulations=100, seed=1)
        b = bootstrap_returns(returns, n_simulations=100, seed=2)
        assert a.mean_total_return != b.mean_total_return

    def test_p5_le_median_le_p95(self):
        rng = np.random.default_rng(0)
        returns = rng.normal(0.0005, 0.02, size=500)
        out = bootstrap_returns(returns, n_simulations=1000, seed=11)
        assert out.p5_total_return <= out.median_total_return
        assert out.median_total_return <= out.p95_total_return

    def test_bootstrap_mean_close_to_sample_compound(self):
        rng = np.random.default_rng(0)
        returns = rng.normal(0.001, 0.001, size=100)
        out = bootstrap_returns(returns, n_simulations=2000, seed=13)
        sample_compound = float(np.prod(1.0 + returns) - 1.0)
        assert out.mean_total_return == pytest.approx(
            sample_compound, abs=0.05
        )


class TestBlockBootstrap:
    def test_block_bootstrap_runs(self):
        rng = np.random.default_rng(0)
        returns = rng.normal(0.0, 0.01, size=200)
        out = block_bootstrap(
            returns, n_simulations=200, block_size=10, seed=42
        )
        assert isinstance(out, SimulationStats)

    def test_block_bootstrap_seeded_deterministic(self):
        returns = np.array([0.01, -0.005, 0.002, 0.015, -0.01, 0.003] * 10)
        a = block_bootstrap(returns, n_simulations=100, block_size=5, seed=99)
        b = block_bootstrap(returns, n_simulations=100, block_size=5, seed=99)
        assert a.mean_total_return == b.mean_total_return


class TestValidation:
    def test_empty_returns_raises(self):
        with pytest.raises(MonteCarloError):
            bootstrap_returns(np.array([]), n_simulations=100, seed=0)

    def test_zero_simulations_raises(self):
        returns = np.array([0.01, 0.02])
        with pytest.raises(MonteCarloError):
            bootstrap_returns(returns, n_simulations=0, seed=0)

    def test_block_size_larger_than_returns_raises(self):
        returns = np.array([0.01, 0.02])
        with pytest.raises(MonteCarloError):
            block_bootstrap(returns, n_simulations=10, block_size=10, seed=0)

    def test_block_size_zero_raises(self):
        returns = np.array([0.01, 0.02])
        with pytest.raises(MonteCarloError):
            block_bootstrap(returns, n_simulations=10, block_size=0, seed=0)

    def test_non_finite_returns_rejected(self):
        with pytest.raises(MonteCarloError):
            bootstrap_returns(
                np.array([0.01, np.nan]), n_simulations=10, seed=0
            )


class TestSimulationStats:
    def test_dataclass_fields(self):
        s = SimulationStats(
            n_simulations=100,
            mean_total_return=0.05,
            median_total_return=0.04,
            p5_total_return=-0.02,
            p95_total_return=0.12,
            mean_max_drawdown=0.10,
            p95_max_drawdown=0.20,
        )
        assert s.n_simulations == 100
        assert s.p5_total_return < s.median_total_return < s.p95_total_return
