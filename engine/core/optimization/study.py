"""Study orchestrator — runs an objective against a sampler (gh#120).

A :class:`Study` is the consumer of a sampler's stream. It evaluates
the operator-provided objective for each parameter assignment,
captures successes and exceptions, and selects the best trial
according to ``direction``.

Synchronous on purpose
----------------------
The Study runs trials inline in the calling task. Async / parallel
execution is the caller's responsibility — typically by wrapping each
parameter assignment in a TaskIQ job. The samplers themselves are
generators; callers can also pull a few at a time and run them in
batches.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from typing import Any

import structlog

from engine.core.optimization.samplers import grid_search, random_search
from engine.core.optimization.types import (
    Direction,
    ParamSpec,
    StudyResult,
    Trial,
)

logger = structlog.get_logger()


Objective = Callable[[dict[str, Any]], float]


class Study:
    """Run an objective over a parameter stream and pick the best trial."""

    def __init__(
        self,
        *,
        specs: list[ParamSpec],
        objective: Objective,
        direction: Direction = "maximize",
        max_trials: int | None = None,
    ) -> None:
        if direction not in ("maximize", "minimize"):
            raise ValueError(f"direction must be 'maximize' or 'minimize', got {direction!r}")
        if max_trials is not None and max_trials <= 0:
            raise ValueError("max_trials must be positive when set")
        self.specs = specs
        self.objective = objective
        self.direction = direction
        self.max_trials = max_trials

    # ------------------------------------------------------------------
    # Public runners
    # ------------------------------------------------------------------

    def run_grid(self) -> StudyResult:
        """Exhaustive grid search."""
        return self._run(grid_search(self.specs), sampler="grid")

    def run_random(self, n_trials: int, *, seed: int | None = None) -> StudyResult:
        """Random search with ``n_trials`` samples."""
        if n_trials <= 0:
            raise ValueError("n_trials must be positive")
        capped = n_trials if self.max_trials is None else min(n_trials, self.max_trials)
        return self._run(random_search(self.specs, capped, seed=seed), sampler="random")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _run(self, stream: Iterable[dict[str, Any]], *, sampler: str) -> StudyResult:
        trials: list[Trial] = []
        for index, params in enumerate(stream):
            if self.max_trials is not None and index >= self.max_trials:
                break
            try:
                score = float(self.objective(params))
            except Exception as exc:
                logger.warning(
                    "optimization.trial_failed",
                    sampler=sampler,
                    index=index,
                    params=params,
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:200],
                )
                trials.append(Trial(index=index, params=params, error=str(exc)[:200]))
                continue
            if math.isnan(score):
                logger.warning(
                    "optimization.trial_nan",
                    sampler=sampler,
                    index=index,
                    params=params,
                )
                trials.append(Trial(index=index, params=params, error="objective returned NaN"))
                continue
            trials.append(Trial(index=index, params=params, score=score))

        best = self._pick_best(trials)
        return StudyResult(
            trials=tuple(trials),
            best=best,
            direction=self.direction,
            sampler=sampler,
        )

    def _pick_best(self, trials: list[Trial]) -> Trial | None:
        scored = [t for t in trials if t.score is not None]
        if not scored:
            return None
        if self.direction == "maximize":
            return max(scored, key=lambda t: t.score)  # type: ignore[arg-type]
        return min(scored, key=lambda t: t.score)  # type: ignore[arg-type]
