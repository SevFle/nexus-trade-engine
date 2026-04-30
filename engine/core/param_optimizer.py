"""Strategy hyperparameter optimization.

Pure-Python search algorithms for tuning strategy parameters against
an objective function (typically a backtest's Sharpe or compound
return). PR1 ships grid, random, and genetic search; Bayesian and
Hyperband land in PR2.

Three abstractions:

- :class:`ParameterSpace` — typed search space (continuous / discrete /
  categorical dimensions).
- :class:`Optimizer` Protocol — algorithm-specific iteration logic
  exposing ``ask`` (next candidate) and ``tell`` (observed score).
- :func:`optimize` — driver that wires an optimizer to an objective
  function for ``n_trials`` rounds and returns an :class:`OptimizationResult`.

All optimizers maximize. To minimize, negate the objective.
"""

from __future__ import annotations

import math
import random as _stdlib_random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


class OptimizerError(Exception):
    """Raised on malformed parameter spaces or optimizer configuration."""


_MIN_POPULATION = 4
_CROSSOVER_PROB = 0.5


@dataclass(frozen=True)
class ContinuousFloat:
    """Real-valued parameter sampled uniformly from [low, high]."""

    low: float
    high: float

    def __post_init__(self) -> None:
        if not (math.isfinite(self.low) and math.isfinite(self.high)):
            msg = "ContinuousFloat bounds must be finite"
            raise OptimizerError(msg)
        if self.low > self.high:
            msg = f"ContinuousFloat: low {self.low} > high {self.high}"
            raise OptimizerError(msg)

    def contains(self, value: Any) -> bool:
        return isinstance(value, (int, float)) and self.low <= value <= self.high

    def sample(self, rng: _stdlib_random.Random) -> float:
        return rng.uniform(self.low, self.high)


@dataclass(frozen=True)
class DiscreteInt:
    """Integer-valued parameter on an arithmetic grid."""

    low: int
    high: int
    step: int = 1

    def __post_init__(self) -> None:
        if self.step <= 0:
            msg = f"DiscreteInt step must be positive; got {self.step}"
            raise OptimizerError(msg)
        if self.low > self.high:
            msg = f"DiscreteInt: low {self.low} > high {self.high}"
            raise OptimizerError(msg)

    def contains(self, value: Any) -> bool:
        if not isinstance(value, int) or isinstance(value, bool):
            return False
        if not (self.low <= value <= self.high):
            return False
        return (value - self.low) % self.step == 0

    def values(self) -> list[int]:
        return list(range(self.low, self.high + 1, self.step))

    def sample(self, rng: _stdlib_random.Random) -> int:
        return rng.choice(self.values())


@dataclass(frozen=True)
class Categorical:
    """Discrete categorical choice."""

    choices: tuple[Any, ...]

    def __post_init__(self) -> None:
        if not self.choices:
            msg = "Categorical requires at least one choice"
            raise OptimizerError(msg)

    def contains(self, value: Any) -> bool:
        return value in self.choices

    def sample(self, rng: _stdlib_random.Random) -> Any:
        return rng.choice(list(self.choices))


Dimension = ContinuousFloat | DiscreteInt | Categorical


@dataclass(frozen=True)
class ParameterSpace:
    """Named collection of dimensions defining the search hypercube."""

    dimensions: dict[str, Dimension] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.dimensions:
            msg = "ParameterSpace requires at least one dimension"
            raise OptimizerError(msg)

    def contains(self, point: dict[str, Any]) -> bool:
        if set(point) != set(self.dimensions):
            return False
        return all(self.dimensions[k].contains(v) for k, v in point.items())

    def sample(self, rng: _stdlib_random.Random) -> dict[str, Any]:
        return {name: dim.sample(rng) for name, dim in self.dimensions.items()}


@dataclass(frozen=True)
class OptimizationResult:
    best_params: dict[str, Any]
    best_score: float
    n_trials_run: int
    history: list[dict[str, Any]]


class Optimizer(Protocol):
    """Algorithm-specific iteration."""

    def ask(self, space: ParameterSpace) -> Iterator[dict[str, Any]]:
        """Yield candidate parameter dicts. May be infinite."""
        ...

    def tell(self, params: dict[str, Any], score: float) -> None:
        """Record the observed score for the candidate."""
        ...


def optimize(
    objective: Callable[[dict[str, Any]], float],
    space: ParameterSpace,
    optimizer: Optimizer,
    *,
    n_trials: int | None,
) -> OptimizationResult:
    """Run an optimizer against an objective.

    ``n_trials=None`` runs the optimizer to exhaustion (only meaningful
    for finite optimizers like grid search). Otherwise stops after the
    given number of evaluations.
    """
    if n_trials is not None and n_trials < 1:
        msg = f"n_trials must be >= 1 or None; got {n_trials}"
        raise OptimizerError(msg)

    history: list[dict[str, Any]] = []
    best_params: dict[str, Any] | None = None
    best_score: float = float("-inf")

    iterator = optimizer.ask(space)
    n_run = 0
    for params in iterator:
        if n_trials is not None and n_run >= n_trials:
            break
        try:
            score = float(objective(params))
        except Exception as exc:
            optimizer.tell(params, float("-inf"))
            history.append(
                {"params": dict(params), "score": float("-inf"), "error": str(exc)}
            )
            n_run += 1
            continue
        if math.isnan(score):
            score = float("-inf")
        history.append({"params": dict(params), "score": score})
        optimizer.tell(params, score)
        if score > best_score:
            best_score = score
            best_params = dict(params)
        n_run += 1

    return OptimizationResult(
        best_params=best_params if best_params is not None else {},
        best_score=best_score,
        n_trials_run=n_run,
        history=history,
    )


class GridSearchOptimizer:
    """Exhaustive Cartesian product over discrete / categorical dims."""

    def ask(self, space: ParameterSpace) -> Iterator[dict[str, Any]]:
        names: list[str] = []
        value_lists: list[list[Any]] = []
        for name, dim in space.dimensions.items():
            if isinstance(dim, ContinuousFloat):
                msg = (
                    f"GridSearchOptimizer cannot enumerate continuous "
                    f"dimension {name!r}; use Discretize or "
                    f"RandomSearchOptimizer"
                )
                raise OptimizerError(msg)
            if isinstance(dim, DiscreteInt):
                value_lists.append(list(dim.values()))
            else:
                value_lists.append(list(dim.choices))
            names.append(name)

        def _recurse(
            idx: int, current: dict[str, Any]
        ) -> Iterator[dict[str, Any]]:
            if idx == len(names):
                yield dict(current)
                return
            for v in value_lists[idx]:
                current[names[idx]] = v
                yield from _recurse(idx + 1, current)

        yield from _recurse(0, {})

    def tell(self, params: dict[str, Any], score: float) -> None:
        del params, score  # ignored — this optimizer doesn't learn


class RandomSearchOptimizer:
    """Independent uniform sampling from the parameter space."""

    def __init__(self, seed: int | None = None) -> None:
        self._rng = _stdlib_random.Random(seed)  # noqa: S311 - non-crypto RNG for parameter search

    def ask(self, space: ParameterSpace) -> Iterator[dict[str, Any]]:
        while True:
            yield space.sample(self._rng)

    def tell(self, params: dict[str, Any], score: float) -> None:
        del params, score  # ignored — this optimizer doesn't learn


class GeneticOptimizer:
    """Generational genetic algorithm with elitism + uniform crossover."""

    def __init__(
        self,
        *,
        population_size: int = 20,
        mutation_rate: float = 0.2,
        seed: int | None = None,
    ) -> None:
        if population_size < _MIN_POPULATION:
            msg = (
                f"population_size must be >= {_MIN_POPULATION}; "
                f"got {population_size}"
            )
            raise OptimizerError(msg)
        if not 0.0 <= mutation_rate <= 1.0:
            msg = f"mutation_rate must be in [0, 1]; got {mutation_rate}"
            raise OptimizerError(msg)
        self.population_size = population_size
        self.mutation_rate = mutation_rate
        self._rng = _stdlib_random.Random(seed)  # noqa: S311 - non-crypto RNG for parameter search
        self._population: list[tuple[dict[str, Any], float]] = []
        self._pending: list[dict[str, Any]] = []

    def ask(self, space: ParameterSpace) -> Iterator[dict[str, Any]]:
        for _ in range(self.population_size):
            params = space.sample(self._rng)
            self._pending.append(params)
            yield params
        while True:
            self._evolve(space)
            for params in self._pending:
                yield params

    def tell(self, params: dict[str, Any], score: float) -> None:
        self._population.append((params, score))

    def _evolve(self, space: ParameterSpace) -> None:
        gen = self._population[-self.population_size :]
        gen.sort(key=lambda t: t[1], reverse=True)
        survivors = [p for p, _ in gen[: max(2, self.population_size // 2)]]
        offspring: list[dict[str, Any]] = []
        while len(offspring) < self.population_size:
            a = self._rng.choice(survivors)
            b = self._rng.choice(survivors)
            child = self._crossover(a, b)
            child = self._mutate(child, space)
            offspring.append(child)
        self._pending = offspring

    def _crossover(
        self, a: dict[str, Any], b: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            k: (a[k] if self._rng.random() < _CROSSOVER_PROB else b[k])
            for k in a
        }

    def _mutate(
        self, params: dict[str, Any], space: ParameterSpace
    ) -> dict[str, Any]:
        out = dict(params)
        for name, dim in space.dimensions.items():
            if self._rng.random() < self.mutation_rate:
                out[name] = dim.sample(self._rng)
        return out


__all__ = [
    "Categorical",
    "ContinuousFloat",
    "Dimension",
    "DiscreteInt",
    "GeneticOptimizer",
    "GridSearchOptimizer",
    "OptimizationResult",
    "Optimizer",
    "OptimizerError",
    "ParameterSpace",
    "RandomSearchOptimizer",
    "optimize",
]
