"""Parameter optimization (gh#120).

Search a hyperparameter space against an operator-provided objective.
Today this ships **grid** and **random** samplers; Bayesian, genetic,
and Hyperband samplers are tracked as follow-ups in the gh#120 PR.

Public surface
--------------

- :class:`ParamSpec` — declarative description of the search space.
- :class:`Trial` — one parameter assignment + its objective score.
- :class:`StudyResult` — the full set of trials and the best one.
- :class:`Study` — the orchestrator. Synchronous execution; the caller
  wires async / TaskIQ on top.
- :func:`grid_search` — exhaustive Cartesian product.
- :func:`random_search` — uniform sampler over the same spec.

Design
------
The optimizer is intentionally decoupled from backtesting. It accepts a
``Callable[[dict[str, Any]], float]`` objective and an optional
``direction`` (``"maximize"`` or ``"minimize"``) and reports the best
trial. Callers wrap a backtest (or any other scoring routine) in a
small adapter that maps params → metric.
"""

from engine.core.optimization.samplers import grid_search, random_search
from engine.core.optimization.study import Study
from engine.core.optimization.types import ParamSpec, StudyResult, Trial

__all__ = [
    "ParamSpec",
    "Study",
    "StudyResult",
    "Trial",
    "grid_search",
    "random_search",
]
