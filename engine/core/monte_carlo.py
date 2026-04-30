"""Monte Carlo robustness testing for backtest results.

Resamples a return series with replacement to build a distribution of
possible alternate-history outcomes.

Two flavors:
- :func:`bootstrap_returns` — i.i.d. bootstrap (Efron). Samples
  independently and uniformly from the empirical distribution.
- :func:`block_bootstrap` — moving-block bootstrap (Künsch). Samples
  contiguous blocks so simulated paths preserve short-run autocorrelation.

Pure-numpy. Deterministic given a ``seed``. Inputs validated to reject
empty / non-finite series and impossible block sizes.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]


class MonteCarloError(Exception):
    """Raised on malformed inputs to a Monte Carlo simulation."""


@dataclass(frozen=True)
class SimulationStats:
    """Distribution summary across N simulated paths."""

    n_simulations: int
    mean_total_return: float
    median_total_return: float
    p5_total_return: float
    p95_total_return: float
    mean_max_drawdown: float
    p95_max_drawdown: float


def max_drawdown(equity_curve: FloatArray) -> float:
    """Worst peak-to-trough drop as a positive fraction in [0, 1]."""
    eq = np.asarray(equity_curve, dtype=np.float64)
    if eq.size == 0:
        return 0.0
    running_peak = np.maximum.accumulate(eq)
    drawdowns = (running_peak - eq) / running_peak
    drawdowns = np.where(running_peak > 0, drawdowns, 0.0)
    return float(np.max(drawdowns))


def _validate_returns(returns: FloatArray) -> FloatArray:
    arr = np.asarray(returns, dtype=np.float64)
    if arr.size == 0:
        msg = "returns must be non-empty"
        raise MonteCarloError(msg)
    if not np.isfinite(arr).all():
        msg = "returns must be finite (no NaN / Inf)"
        raise MonteCarloError(msg)
    return arr


def _validate_n_simulations(n: int) -> None:
    if n < 1:
        msg = f"n_simulations must be >= 1; got {n}"
        raise MonteCarloError(msg)


def _summarize(
    n_simulations: int,
    total_returns: FloatArray,
    max_drawdowns: FloatArray,
) -> SimulationStats:
    return SimulationStats(
        n_simulations=n_simulations,
        mean_total_return=float(np.mean(total_returns)),
        median_total_return=float(np.median(total_returns)),
        p5_total_return=float(np.percentile(total_returns, 5)),
        p95_total_return=float(np.percentile(total_returns, 95)),
        mean_max_drawdown=float(np.mean(max_drawdowns)),
        p95_max_drawdown=float(np.percentile(max_drawdowns, 95)),
    )


def bootstrap_returns(
    returns: FloatArray,
    *,
    n_simulations: int,
    seed: int | None = None,
) -> SimulationStats:
    """i.i.d. bootstrap of a return series."""
    arr = _validate_returns(returns)
    _validate_n_simulations(n_simulations)
    rng = np.random.default_rng(seed)
    n_obs = arr.size
    indices = rng.integers(0, n_obs, size=(n_simulations, n_obs))
    sampled = arr[indices]
    equity = np.cumprod(1.0 + sampled, axis=1).astype(np.float64, copy=False)
    total_returns = (equity[:, -1] - 1.0).astype(np.float64, copy=False)
    running_peak = np.maximum.accumulate(equity, axis=1)
    drawdowns = ((running_peak - equity) / running_peak).astype(np.float64, copy=False)
    max_dds = np.max(drawdowns, axis=1).astype(np.float64, copy=False)
    return _summarize(n_simulations, total_returns, max_dds)


def block_bootstrap(
    returns: FloatArray,
    *,
    n_simulations: int,
    block_size: int,
    seed: int | None = None,
) -> SimulationStats:
    """Moving-block bootstrap (Künsch).

    Samples contiguous blocks of length ``block_size`` to preserve
    short-run autocorrelation. Each path concatenates
    ``ceil(n_obs / block_size)`` blocks and truncates to ``n_obs``.
    """
    arr = _validate_returns(returns)
    _validate_n_simulations(n_simulations)
    n_obs = arr.size
    if block_size < 1:
        msg = f"block_size must be >= 1; got {block_size}"
        raise MonteCarloError(msg)
    if block_size >= n_obs:
        msg = f"block_size {block_size} must be < returns length {n_obs}"
        raise MonteCarloError(msg)

    rng = np.random.default_rng(seed)
    n_blocks = -(-n_obs // block_size)
    n_starts = n_obs - block_size + 1

    starts = rng.integers(0, n_starts, size=(n_simulations, n_blocks))
    block_offsets = np.arange(block_size)
    indices = starts[:, :, None] + block_offsets[None, None, :]
    indices = indices.reshape(n_simulations, -1)[:, :n_obs]
    sampled = arr[indices]
    equity = np.cumprod(1.0 + sampled, axis=1).astype(np.float64, copy=False)
    total_returns = (equity[:, -1] - 1.0).astype(np.float64, copy=False)
    running_peak = np.maximum.accumulate(equity, axis=1)
    drawdowns = ((running_peak - equity) / running_peak).astype(np.float64, copy=False)
    max_dds = np.max(drawdowns, axis=1).astype(np.float64, copy=False)
    return _summarize(n_simulations, total_returns, max_dds)


__all__ = [
    "MonteCarloError",
    "SimulationStats",
    "block_bootstrap",
    "bootstrap_returns",
    "max_drawdown",
]
