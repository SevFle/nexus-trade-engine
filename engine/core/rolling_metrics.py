"""Rolling-window time-series metrics (gh#97 follow-up).

Pure-function helpers producing the *full* rolling series across an
input return sequence. Complements the class-bound
``_rolling_window_metrics`` helper in ``engine.core.metrics`` which
returns only the *latest* snapshot per window size — this module
returns one value per input bar so callers can plot
rolling-Sharpe / rolling-vol charts.

Coverage (numbers from gh#97 taxonomy):

- 56  Rolling Sharpe          — ``rolling_sharpe``
- 57  Rolling Sortino         — ``rolling_sortino``
- 58  Rolling volatility      — ``rolling_volatility``
- 59  Rolling return          — ``rolling_return``
- 60  Rolling mean return     — ``rolling_mean``

Output convention:

- Each helper returns a list the same length as the input.
- Indices before the first full window are ``None`` (not ``0.0``) so
  callers can distinguish "not enough data yet" from "zero".
- Annualisation factor defaults to 252 (US trading days). Pass 365 for
  daily crypto, 12 for monthly returns, etc.

Out of scope:
- Bias correction (Lo 2002) for rolling Sharpe — caller can post-process.
- Welford's online-variance — current pass is O(N · W); fine up to a
  few million bars.
- Calendar-period rollups (those live in ``engine.core.time_metrics``).
"""

from __future__ import annotations

import math
from collections.abc import Sequence

DEFAULT_ANNUALISATION = 252


def _validate_window(window: int) -> None:
    if window < 2:
        raise ValueError("window must be >= 2")


def rolling_mean(
    returns: Sequence[float], window: int
) -> list[float | None]:
    """Mean of each ``window``-sized slice ending at index ``i``.

    ``None`` for the first ``window - 1`` indices.
    """
    _validate_window(window)
    n = len(returns)
    out: list[float | None] = [None] * n
    if n < window:
        return out
    for i in range(window - 1, n):
        slice_ = returns[i - window + 1 : i + 1]
        out[i] = sum(slice_) / window
    return out


def _stdev(xs: Sequence[float], *, ddof: int = 1) -> float:
    n = len(xs)
    if n - ddof <= 0:
        return 0.0
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - ddof)
    return math.sqrt(var)


def rolling_volatility(
    returns: Sequence[float],
    window: int,
    *,
    annualisation_factor: int = DEFAULT_ANNUALISATION,
) -> list[float | None]:
    """Annualised volatility (% as decimal, e.g. ``0.20`` = 20 %).

    Standard deviation × √(annualisation_factor). ``None`` for the
    first ``window - 1`` indices.
    """
    _validate_window(window)
    if annualisation_factor <= 0:
        raise ValueError("annualisation_factor must be > 0")
    n = len(returns)
    out: list[float | None] = [None] * n
    if n < window:
        return out
    sqrt_ann = math.sqrt(annualisation_factor)
    for i in range(window - 1, n):
        slice_ = returns[i - window + 1 : i + 1]
        out[i] = _stdev(slice_) * sqrt_ann
    return out


def rolling_sharpe(
    returns: Sequence[float],
    window: int,
    *,
    risk_free_rate: float = 0.0,
    annualisation_factor: int = DEFAULT_ANNUALISATION,
) -> list[float | None]:
    """Annualised rolling Sharpe ratio.

    ``risk_free_rate`` is the *annualised* rate (e.g. ``0.05`` for 5 %);
    converted to per-period internally. Returns ``0.0`` for windows
    where the std is zero (a degenerate flat-return window).
    """
    _validate_window(window)
    if annualisation_factor <= 0:
        raise ValueError("annualisation_factor must be > 0")
    n = len(returns)
    out: list[float | None] = [None] * n
    if n < window:
        return out
    rf_per_period = risk_free_rate / annualisation_factor
    sqrt_ann = math.sqrt(annualisation_factor)
    for i in range(window - 1, n):
        slice_ = returns[i - window + 1 : i + 1]
        mean = sum(slice_) / window
        sd = _stdev(slice_)
        if sd == 0.0:
            out[i] = 0.0
        else:
            out[i] = (mean - rf_per_period) / sd * sqrt_ann
    return out


def rolling_sortino(
    returns: Sequence[float],
    window: int,
    *,
    risk_free_rate: float = 0.0,
    annualisation_factor: int = DEFAULT_ANNUALISATION,
) -> list[float | None]:
    """Annualised rolling Sortino ratio.

    Uses downside deviation (RMS of negative excess returns) in the
    denominator. Returns ``0.0`` for windows where the downside
    deviation is zero (no losing periods inside the window).
    """
    _validate_window(window)
    if annualisation_factor <= 0:
        raise ValueError("annualisation_factor must be > 0")
    n = len(returns)
    out: list[float | None] = [None] * n
    if n < window:
        return out
    rf_per_period = risk_free_rate / annualisation_factor
    sqrt_ann = math.sqrt(annualisation_factor)
    for i in range(window - 1, n):
        slice_ = returns[i - window + 1 : i + 1]
        mean = sum(slice_) / window
        downside_sq = [min(r - rf_per_period, 0.0) ** 2 for r in slice_]
        downside_dev = math.sqrt(sum(downside_sq) / window)
        if downside_dev == 0.0:
            out[i] = 0.0
        else:
            out[i] = (mean - rf_per_period) / downside_dev * sqrt_ann
    return out


def rolling_return(
    returns: Sequence[float], window: int
) -> list[float | None]:
    """Compounded return over each ``window``-sized trailing slice.

    ``(1 + r_total) = ∏(1 + r_i)``; returns ``r_total``. ``None`` for
    the first ``window - 1`` indices.
    """
    _validate_window(window)
    n = len(returns)
    out: list[float | None] = [None] * n
    if n < window:
        return out
    for i in range(window - 1, n):
        slice_ = returns[i - window + 1 : i + 1]
        product = 1.0
        for r in slice_:
            product *= 1.0 + r
        out[i] = product - 1.0
    return out


__all__ = [
    "DEFAULT_ANNUALISATION",
    "rolling_mean",
    "rolling_return",
    "rolling_sharpe",
    "rolling_sortino",
    "rolling_volatility",
]
