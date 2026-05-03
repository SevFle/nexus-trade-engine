"""Unit tests for the parameter-optimization module — gh#120."""

from __future__ import annotations

import pytest

from engine.core.optimization import (
    ParamSpec,
    Study,
    StudyResult,
    grid_search,
    random_search,
)

# ---------------------------------------------------------------------------
# ParamSpec validation
# ---------------------------------------------------------------------------


class TestParamSpec:
    def test_discrete_ok(self):
        s = ParamSpec(name="x", choices=(1, 2, 3))
        assert s.is_discrete

    def test_continuous_ok(self):
        s = ParamSpec(name="x", low=0.0, high=1.0)
        assert not s.is_discrete

    def test_log_continuous_ok(self):
        s = ParamSpec(name="lr", low=1e-4, high=1e-1, log=True)
        assert not s.is_discrete

    def test_neither_set_rejected(self):
        with pytest.raises(ValueError):
            ParamSpec(name="x")

    def test_both_set_rejected(self):
        with pytest.raises(ValueError):
            ParamSpec(name="x", choices=(1,), low=0, high=1)

    def test_inverted_range_rejected(self):
        with pytest.raises(ValueError):
            ParamSpec(name="x", low=1.0, high=0.0)

    def test_log_with_zero_low_rejected(self):
        with pytest.raises(ValueError):
            ParamSpec(name="x", low=0.0, high=1.0, log=True)


# ---------------------------------------------------------------------------
# Grid search
# ---------------------------------------------------------------------------


class TestGridSearch:
    def test_empty_specs_yields_one_empty(self):
        out = list(grid_search([]))
        assert out == [{}]

    def test_single_spec(self):
        specs = [ParamSpec(name="x", choices=(1, 2, 3))]
        out = list(grid_search(specs))
        assert out == [{"x": 1}, {"x": 2}, {"x": 3}]

    def test_cartesian_product_size(self):
        specs = [
            ParamSpec(name="a", choices=(1, 2, 3)),
            ParamSpec(name="b", choices=("x", "y")),
        ]
        out = list(grid_search(specs))
        assert len(out) == 6
        # First spec varies slowest (outer loop):
        assert out[0] == {"a": 1, "b": "x"}
        assert out[1] == {"a": 1, "b": "y"}
        assert out[2] == {"a": 2, "b": "x"}

    def test_continuous_spec_rejected(self):
        specs = [ParamSpec(name="x", low=0.0, high=1.0)]
        with pytest.raises(ValueError):
            list(grid_search(specs))


# ---------------------------------------------------------------------------
# Random search
# ---------------------------------------------------------------------------


class TestRandomSearch:
    def test_count_matches_n_trials(self):
        specs = [ParamSpec(name="x", choices=(1, 2, 3))]
        out = list(random_search(specs, n_trials=10, seed=0))
        assert len(out) == 10

    def test_zero_trials_yields_nothing(self):
        out = list(random_search([], n_trials=0, seed=0))
        assert out == []

    def test_seed_is_deterministic(self):
        specs = [
            ParamSpec(name="x", choices=(1, 2, 3, 4, 5)),
            ParamSpec(name="y", low=0.0, high=1.0),
        ]
        a = list(random_search(specs, n_trials=20, seed=42))
        b = list(random_search(specs, n_trials=20, seed=42))
        assert a == b

    def test_different_seeds_produce_different_streams(self):
        specs = [ParamSpec(name="x", choices=tuple(range(50)))]
        a = list(random_search(specs, n_trials=30, seed=1))
        b = list(random_search(specs, n_trials=30, seed=2))
        assert a != b

    def test_continuous_uniform_in_range(self):
        specs = [ParamSpec(name="x", low=10.0, high=20.0)]
        out = list(random_search(specs, n_trials=200, seed=0))
        for trial in out:
            assert 10.0 <= trial["x"] <= 20.0

    def test_log_uniform_in_range(self):
        specs = [ParamSpec(name="lr", low=1e-4, high=1e-1, log=True)]
        out = list(random_search(specs, n_trials=200, seed=0))
        for trial in out:
            assert 1e-4 <= trial["lr"] <= 1e-1


# ---------------------------------------------------------------------------
# Study orchestrator
# ---------------------------------------------------------------------------


def _quadratic(params: dict) -> float:
    # Maximum at x=2, y=-3.
    return -((params["x"] - 2.0) ** 2 + (params["y"] + 3.0) ** 2)


class TestStudy:
    def test_grid_finds_known_max(self):
        specs = [
            ParamSpec(name="x", choices=(0, 1, 2, 3, 4)),
            ParamSpec(name="y", choices=(-5, -4, -3, -2, -1)),
        ]
        study = Study(specs=specs, objective=_quadratic, direction="maximize")
        result = study.run_grid()
        assert isinstance(result, StudyResult)
        assert result.best is not None
        assert result.best.params == {"x": 2, "y": -3}
        assert result.best.score == pytest.approx(0.0)
        assert len(result.trials) == 25
        assert len(result.failed) == 0

    def test_minimize_direction(self):
        specs = [ParamSpec(name="x", choices=(1, 2, 3))]
        study = Study(
            specs=specs,
            objective=lambda p: float(p["x"]),
            direction="minimize",
        )
        result = study.run_grid()
        assert result.best is not None
        assert result.best.params == {"x": 1}
        assert result.best.score == 1.0

    def test_objective_exception_captured_not_raised(self):
        def angry(p):
            if p["x"] == 2:
                raise RuntimeError("nope")
            return float(p["x"])

        specs = [ParamSpec(name="x", choices=(1, 2, 3))]
        study = Study(specs=specs, objective=angry, direction="maximize")
        result = study.run_grid()
        assert len(result.trials) == 3
        assert len(result.failed) == 1
        assert result.best is not None
        # x=3 wins (x=2 errored out, x=1 < x=3)
        assert result.best.params == {"x": 3}

    def test_nan_score_treated_as_failure(self):
        specs = [ParamSpec(name="x", choices=(1, 2, 3))]
        study = Study(
            specs=specs,
            objective=lambda p: float("nan") if p["x"] == 2 else float(p["x"]),
            direction="maximize",
        )
        result = study.run_grid()
        assert len(result.failed) == 1
        assert result.best is not None
        assert result.best.params == {"x": 3}

    def test_max_trials_caps_grid(self):
        specs = [ParamSpec(name="x", choices=tuple(range(100)))]
        study = Study(
            specs=specs,
            objective=lambda p: float(p["x"]),
            direction="maximize",
            max_trials=10,
        )
        result = study.run_grid()
        assert len(result.trials) == 10

    def test_random_search_runs(self):
        specs = [
            ParamSpec(name="x", low=0.0, high=10.0),
            ParamSpec(name="y", choices=("a", "b", "c")),
        ]
        study = Study(specs=specs, objective=lambda p: p["x"], direction="maximize")
        result = study.run_random(n_trials=15, seed=7)
        assert len(result.trials) == 15
        assert result.best is not None
        # Random is bounded above by 10.0 (continuous).
        assert result.best.score is not None
        assert result.best.score <= 10.0

    def test_invalid_direction_rejected(self):
        with pytest.raises(ValueError):
            Study(specs=[], objective=lambda p: 0.0, direction="dunno")  # type: ignore[arg-type]

    def test_zero_max_trials_rejected(self):
        with pytest.raises(ValueError):
            Study(
                specs=[ParamSpec(name="x", choices=(1,))],
                objective=lambda p: 0.0,
                max_trials=0,
            )

    def test_zero_n_trials_rejected(self):
        study = Study(
            specs=[ParamSpec(name="x", choices=(1,))],
            objective=lambda p: 0.0,
        )
        with pytest.raises(ValueError):
            study.run_random(n_trials=0)
