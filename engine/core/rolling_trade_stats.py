"""Rolling trade-stat + Calmar time series (gh#97 follow-up).

Pure-function helpers producing the *full* rolling series of trade-level
metrics across an input sequence. Complements the full-period helpers
in ``engine.core.trade_stats`` (#346) and ``engine.core.rolling_metrics``
(#343) by giving callers chartable rolling versions of profit factor,
hit ratio, win/loss, and Calmar.

Coverage (numbers from gh#97 taxonomy):

- 56b Rolling Calmar           — ``rolling_calmar``
- 61  Rolling profit factor    — ``rolling_profit_factor``
- 62  Rolling hit ratio        — ``rolling_hit_ratio``
- 63  Rolling win/loss ratio   — ``rolling_win_loss_ratio``

Output convention follows ``engine.core.rolling_metrics`` (#343):
same-length output with ``None`` for the first ``window - 1`` indices.

Out of scope:
- Rolling MAR / Sterling — analogous slice when needed.
- Rolling expectancy — caller can compose from rolling_mean +
  rolling_hit_ratio.
- Calendar-time rollups (those live in ``engine.core.time_metrics``).
"""

from __future__ import annotations

from collections.abc import Sequence

from engine.core.drawdown_analytics import underwater_curve
from engine.core.trade_stats import (
    hit_ratio,
    profit_factor,
    win_loss_ratio,
)


def _validate_window(window: int) -> None:
    if window < 2:
        raise ValueError("window must be >= 2")


def rolling_hit_ratio(
    trade_pnls: Sequence[float], window: int
) -> list[float | None]:
    """Hit ratio over each ``window``-sized trailing slice of trades.

    ``None`` for the first ``window - 1`` indices.
    """
    _validate_window(window)
    n = len(trade_pnls)
    out: list[float | None] = [None] * n
    if n < window:
        return out
    for i in range(window - 1, n):
        out[i] = hit_ratio(trade_pnls[i - window + 1 : i + 1])
    return out


def rolling_profit_factor(
    trade_pnls: Sequence[float], window: int
) -> list[float | None]:
    """Profit factor over each trailing window.

    Mirrors ``engine.core.trade_stats.profit_factor`` semantics:
    ``None`` for windows with no losses, ``0.0`` for windows with no
    wins, ``0.0`` for empty windows. ``None`` for first ``window - 1``
    indices (insufficient data).
    """
    _validate_window(window)
    n = len(trade_pnls)
    out: list[float | None] = [None] * n
    if n < window:
        return out
    for i in range(window - 1, n):
        out[i] = profit_factor(trade_pnls[i - window + 1 : i + 1])
    return out


def rolling_win_loss_ratio(
    trade_pnls: Sequence[float], window: int
) -> list[float | None]:
    """Win/loss ratio over each trailing window."""
    _validate_window(window)
    n = len(trade_pnls)
    out: list[float | None] = [None] * n
    if n < window:
        return out
    for i in range(window - 1, n):
        out[i] = win_loss_ratio(trade_pnls[i - window + 1 : i + 1])
    return out


def _annualised_return(equity_window: Sequence[float], periods_per_year: int) -> float:
    """Compounded return × (periods_per_year / window) — annualised."""
    if len(equity_window) < 2 or equity_window[0] <= 0:
        return 0.0
    total_return = equity_window[-1] / equity_window[0] - 1.0
    bars = len(equity_window) - 1
    if bars <= 0:
        return 0.0
    years = bars / periods_per_year
    if years <= 0:
        return 0.0
    return (1.0 + total_return) ** (1.0 / years) - 1.0


def rolling_calmar(
    equity: Sequence[float],
    window: int,
    *,
    periods_per_year: int = 252,
) -> list[float | None]:
    """Annualised return / max drawdown over each trailing window.

    Calmar uses the underwater curve (``engine.core.drawdown_analytics``)
    inside each window to find the deepest drawdown, then divides
    annualised return by that magnitude. Returns ``None`` when the
    window has no drawdown (avoids divide-by-zero) and the annualised
    return is positive (Calmar undefined). Returns ``0.0`` when the
    annualised return is non-positive with zero drawdown.
    """
    _validate_window(window)
    if periods_per_year <= 0:
        raise ValueError("periods_per_year must be > 0")
    n = len(equity)
    out: list[float | None] = [None] * n
    if n < window:
        return out
    for i in range(window - 1, n):
        win = equity[i - window + 1 : i + 1]
        ann_ret = _annualised_return(win, periods_per_year)
        uw = underwater_curve(win)
        max_dd = -min(uw) if uw else 0.0
        if max_dd <= 0.0:
            out[i] = 0.0 if ann_ret <= 0.0 else None
        else:
            out[i] = ann_ret / max_dd
    return out


__all__ = [
    "rolling_calmar",
    "rolling_hit_ratio",
    "rolling_profit_factor",
    "rolling_win_loss_ratio",
]
