"""Walk-forward analysis: rolling / expanding train+test split iterator.

Standard cross-validation pattern for time-series backtests: strict
temporal ordering, no leakage. Two modes:

- :class:`WindowMode.ROLLING` — fixed-size train window slides forward
  by ``step`` each iteration; older history drops off.
- :class:`WindowMode.EXPANDING` — train window starts at index 0 and
  grows by ``step`` each iteration; older history is retained.

In both modes the test window has constant size ``test_size`` and
immediately follows the train window. Test indices are contiguous and
disjoint from training data within that split.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

import numpy as np
import numpy.typing as npt

if TYPE_CHECKING:
    from collections.abc import Iterator

IntArray = npt.NDArray[np.int64]


class WalkForwardError(Exception):
    """Raised on malformed walk-forward configuration."""


class WindowMode(StrEnum):
    ROLLING = "rolling"
    EXPANDING = "expanding"


@dataclass(frozen=True)
class WindowSplit:
    """One train+test split."""

    train_indices: IntArray
    test_indices: IntArray


def walk_forward_splits(
    *,
    n_obs: int,
    train_size: int,
    test_size: int,
    step: int,
    mode: WindowMode = WindowMode.ROLLING,
) -> Iterator[WindowSplit]:
    """Yield successive train+test splits.

    First window starts at index 0; subsequent windows advance by
    ``step``. Iteration stops when the next window would extend past
    ``n_obs``.
    """
    if n_obs <= 0:
        msg = f"n_obs must be > 0; got {n_obs}"
        raise WalkForwardError(msg)
    if train_size <= 0:
        msg = f"train_size must be > 0; got {train_size}"
        raise WalkForwardError(msg)
    if test_size <= 0:
        msg = f"test_size must be > 0; got {test_size}"
        raise WalkForwardError(msg)
    if step <= 0:
        msg = f"step must be > 0; got {step}"
        raise WalkForwardError(msg)

    train_start = 0
    while True:
        train_end = train_start + train_size
        test_end = train_end + test_size
        if test_end > n_obs:
            return
        if mode == WindowMode.ROLLING:
            train_idx = np.arange(train_start, train_end, dtype=np.int64)
        else:
            train_idx = np.arange(0, train_end, dtype=np.int64)
        test_idx = np.arange(train_end, test_end, dtype=np.int64)
        yield WindowSplit(train_indices=train_idx, test_indices=test_idx)
        train_start += step


__all__ = [
    "WalkForwardError",
    "WindowMode",
    "WindowSplit",
    "walk_forward_splits",
]
