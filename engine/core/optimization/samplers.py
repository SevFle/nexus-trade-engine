"""Search-space samplers — grid + random (gh#120).

Both yield a stream of ``dict[str, Any]`` parameter assignments. The
:class:`~engine.core.optimization.study.Study` orchestrator consumes
the stream, evaluates the objective, and aggregates trials.

Determinism
-----------
- Grid search is fully deterministic — Cartesian product in declared
  spec order.
- Random search uses ``random.Random(seed)`` so two runs with the same
  ``seed`` produce identical trial streams.
"""

from __future__ import annotations

import math
import random
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

    from engine.core.optimization.types import ParamSpec


def grid_search(specs: list[ParamSpec]) -> Iterator[dict[str, Any]]:
    """Cartesian product over every discrete spec.

    Raises if any spec is continuous (``low``/``high`` only) — grid
    search has no inherent way to discretise a continuous range, and
    silently using endpoints is worse than failing fast.
    """
    if not specs:
        yield {}
        return

    for s in specs:
        if not s.is_discrete:
            raise ValueError(
                f"grid_search: ParamSpec({s.name!r}) is continuous; "
                f"convert to `choices=...` or use random_search"
            )

    yield from _cartesian(specs, 0, {})


def _cartesian(specs: list[ParamSpec], i: int, acc: dict[str, Any]) -> Iterator[dict[str, Any]]:
    if i == len(specs):
        yield dict(acc)
        return
    s = specs[i]
    assert s.choices is not None
    for v in s.choices:
        acc[s.name] = v
        yield from _cartesian(specs, i + 1, acc)
        acc.pop(s.name, None)


def random_search(
    specs: list[ParamSpec],
    n_trials: int,
    *,
    seed: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Sample ``n_trials`` uniformly from the space defined by ``specs``.

    Discrete specs: uniform pick from ``choices``.
    Continuous specs: uniform on ``[low, high]``, log-uniform if
    ``log=True``. Both endpoints are included.
    """
    if n_trials <= 0:
        return
    rng = random.Random(seed)
    for _ in range(n_trials):
        params: dict[str, Any] = {}
        for s in specs:
            if s.is_discrete:
                assert s.choices is not None
                params[s.name] = rng.choice(s.choices)
            else:
                assert s.low is not None
                assert s.high is not None
                if s.log:
                    lo, hi = math.log(s.low), math.log(s.high)
                    params[s.name] = math.exp(rng.uniform(lo, hi))
                else:
                    params[s.name] = rng.uniform(s.low, s.high)
        yield params
