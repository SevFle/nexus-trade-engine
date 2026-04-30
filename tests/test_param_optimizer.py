"""Tests for engine.core.param_optimizer — grid / random / genetic search."""

from __future__ import annotations

import pytest

from engine.core.param_optimizer import (
    Categorical,
    ContinuousFloat,
    DiscreteInt,
    GeneticOptimizer,
    GridSearchOptimizer,
    OptimizationResult,
    OptimizerError,
    ParameterSpace,
    RandomSearchOptimizer,
    optimize,
)


def _bowl(params: dict) -> float:
    """Negative-quadratic centered at x=2, y=3 (max at (2, 3))."""
    x = params["x"]
    y = params["y"]
    return -((x - 2.0) ** 2 + (y - 3.0) ** 2)


def _categorical_obj(params: dict) -> float:
    mapping = {"red": 1.0, "green": 5.0, "blue": 2.0}
    return mapping[params["color"]]


class TestParameterSpace:
    def test_continuous_dimension(self):
        d = ContinuousFloat(low=0.0, high=10.0)
        assert d.contains(5.0)
        assert not d.contains(-1.0)
        assert not d.contains(11.0)

    def test_discrete_dimension(self):
        d = DiscreteInt(low=0, high=10, step=2)
        assert d.contains(4)
        assert not d.contains(3)

    def test_categorical_dimension(self):
        d = Categorical(choices=("red", "green", "blue"))
        assert d.contains("red")
        assert not d.contains("yellow")

    def test_space_construction(self):
        space = ParameterSpace(
            {"x": ContinuousFloat(0.0, 10.0), "y": DiscreteInt(0, 10)}
        )
        assert "x" in space.dimensions
        assert "y" in space.dimensions

    def test_space_validates_point(self):
        space = ParameterSpace({"x": ContinuousFloat(0.0, 10.0)})
        assert space.contains({"x": 5.0})
        assert not space.contains({"x": 15.0})


class TestGridSearch:
    def test_grid_finds_max_on_bowl(self):
        space = ParameterSpace(
            {
                "x": DiscreteInt(low=0, high=4),
                "y": DiscreteInt(low=0, high=6),
            }
        )
        opt = GridSearchOptimizer()
        result = optimize(_bowl, space, opt, n_trials=None)
        assert result.best_params == {"x": 2, "y": 3}
        assert result.best_score == pytest.approx(0.0)

    def test_grid_iterates_full_cartesian_product(self):
        space = ParameterSpace(
            {"x": DiscreteInt(low=0, high=2), "y": DiscreteInt(low=0, high=1)}
        )
        opt = GridSearchOptimizer()
        result = optimize(lambda p: -1.0, space, opt, n_trials=None)
        assert result.n_trials_run == 6


class TestRandomSearch:
    def test_random_search_finds_high_score_region(self):
        space = ParameterSpace(
            {
                "x": ContinuousFloat(low=0.0, high=4.0),
                "y": ContinuousFloat(low=0.0, high=6.0),
            }
        )
        opt = RandomSearchOptimizer(seed=42)
        result = optimize(_bowl, space, opt, n_trials=200)
        assert result.best_score > -1.0

    def test_random_seeded_deterministic(self):
        space = ParameterSpace({"x": ContinuousFloat(0.0, 10.0)})
        a = optimize(_bowl, space, RandomSearchOptimizer(seed=7), n_trials=50)
        b = optimize(_bowl, space, RandomSearchOptimizer(seed=7), n_trials=50)
        assert a.best_score == b.best_score
        assert a.best_params == b.best_params

    def test_random_categorical_finds_best(self):
        space = ParameterSpace(
            {"color": Categorical(choices=("red", "green", "blue"))}
        )
        opt = RandomSearchOptimizer(seed=11)
        result = optimize(_categorical_obj, space, opt, n_trials=200)
        assert result.best_params == {"color": "green"}
        assert result.best_score == pytest.approx(5.0)


class TestGenetic:
    def test_genetic_finds_max_on_bowl(self):
        space = ParameterSpace(
            {
                "x": ContinuousFloat(low=0.0, high=4.0),
                "y": ContinuousFloat(low=0.0, high=6.0),
            }
        )
        opt = GeneticOptimizer(population_size=20, mutation_rate=0.3, seed=42)
        result = optimize(_bowl, space, opt, n_trials=200)
        assert result.best_score > -0.5

    def test_genetic_seeded_deterministic(self):
        space = ParameterSpace({"x": ContinuousFloat(0.0, 10.0)})
        a = optimize(
            _bowl,
            space,
            GeneticOptimizer(population_size=10, seed=42),
            n_trials=50,
        )
        b = optimize(
            _bowl,
            space,
            GeneticOptimizer(population_size=10, seed=42),
            n_trials=50,
        )
        assert a.best_score == b.best_score


class TestResult:
    def test_result_has_history(self):
        space = ParameterSpace({"x": ContinuousFloat(0.0, 10.0)})
        result = optimize(
            _bowl, space, RandomSearchOptimizer(seed=0), n_trials=10
        )
        assert len(result.history) == 10
        for trial in result.history:
            assert "params" in trial
            assert "score" in trial

    def test_result_dataclass(self):
        r = OptimizationResult(
            best_params={"x": 1.0},
            best_score=0.5,
            n_trials_run=10,
            history=[{"params": {"x": 1.0}, "score": 0.5}],
        )
        assert r.best_score == 0.5


class TestValidation:
    def test_zero_trials_raises(self):
        space = ParameterSpace({"x": ContinuousFloat(0.0, 10.0)})
        with pytest.raises(OptimizerError):
            optimize(_bowl, space, RandomSearchOptimizer(seed=0), n_trials=0)

    def test_grid_continuous_dimension_raises(self):
        space = ParameterSpace({"x": ContinuousFloat(0.0, 10.0)})
        with pytest.raises(OptimizerError, match="continuous"):
            optimize(_bowl, space, GridSearchOptimizer(), n_trials=None)

    def test_objective_returning_nan_skipped(self):
        space = ParameterSpace({"x": ContinuousFloat(0.0, 10.0)})

        def bad(params: dict) -> float:
            return float("nan")

        result = optimize(
            bad, space, RandomSearchOptimizer(seed=0), n_trials=5
        )
        assert result.best_score == float("-inf")

    def test_empty_space_rejected(self):
        with pytest.raises(OptimizerError):
            ParameterSpace({})
