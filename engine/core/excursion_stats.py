"""Intra-trade excursion analytics — MAE / MFE / efficiency (gh#97 follow-up).

Pure-function helpers operating on a sequence of ``TradeExcursion``
records — one per closed trade, each carrying the entry / exit prices
plus the most adverse and most favourable mark-to-market levels seen
between entry and exit. These metrics can't be derived from terminal
PnL alone and complement ``engine.core.trade_stats`` (#346).

Coverage (numbers from gh#97 taxonomy):

- 64  Maximum Adverse Excursion (MAE)        — ``mean_mae`` / ``max_mae``
- 65  Maximum Favourable Excursion (MFE)     — ``mean_mfe`` / ``max_mfe``
- 66  Edge ratio (MFE / MAE)                 — ``edge_ratio``
- 67  Trade efficiency (PnL / MFE for wins)  — ``trade_efficiency``

Conventions:

- ``mae`` is recorded as a positive magnitude (max drawdown inside
  the trade as a fraction of entry price). ``mfe`` is also a positive
  magnitude (max unrealised gain inside the trade).
- All helpers handle empty input by returning ``0.0``.
- Long vs short side is encoded by the sign convention on excursion
  values themselves; the caller computes them once at trade close and
  hands the magnitudes to this module.

Out of scope:
- Bar-level path reconstruction (caller already collapsed each trade
  to its (pnl, mfe, mae) summary).
- Time-to-MFE / time-to-MAE (separate slice — needs timestamps).
- Per-symbol / per-strategy rollups.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True)
class TradeExcursion:
    """One trade's terminal pnl plus its intra-trade excursion magnitudes.

    ``pnl`` — terminal trade PnL (signed: positive = win, negative = loss).
    ``mfe`` — maximum favourable excursion as a positive magnitude (best
    unrealised gain inside the trade).
    ``mae`` — maximum adverse excursion as a positive magnitude (worst
    unrealised loss inside the trade). Always ``>= 0``.
    """

    pnl: float
    mfe: float
    mae: float

    def __post_init__(self) -> None:
        if self.mfe < 0:
            raise ValueError(f"mfe must be >= 0; got {self.mfe}")
        if self.mae < 0:
            raise ValueError(f"mae must be >= 0; got {self.mae}")


def mean_mae(trades: Sequence[TradeExcursion]) -> float:
    """Average MAE across trades. ``0.0`` for empty input."""
    if not trades:
        return 0.0
    return sum(t.mae for t in trades) / len(trades)


def mean_mfe(trades: Sequence[TradeExcursion]) -> float:
    """Average MFE across trades. ``0.0`` for empty input."""
    if not trades:
        return 0.0
    return sum(t.mfe for t in trades) / len(trades)


def max_mae(trades: Sequence[TradeExcursion]) -> float:
    """Worst single-trade MAE. ``0.0`` for empty input."""
    if not trades:
        return 0.0
    return max(t.mae for t in trades)


def max_mfe(trades: Sequence[TradeExcursion]) -> float:
    """Best single-trade MFE. ``0.0`` for empty input."""
    if not trades:
        return 0.0
    return max(t.mfe for t in trades)


def edge_ratio(trades: Sequence[TradeExcursion]) -> float:
    """Mean MFE / mean MAE (Sweeney's edge ratio).

    Returns ``0.0`` for empty input or when mean MAE is zero (caller
    distinguishes "no losses" via ``mean_mae == 0``).
    """
    avg_mfe = mean_mfe(trades)
    avg_mae = mean_mae(trades)
    if avg_mae == 0.0:
        return 0.0
    return avg_mfe / avg_mae


def trade_efficiency(trades: Sequence[TradeExcursion]) -> float:
    """Average ``pnl / mfe`` for winning trades.

    Tells you how much of the favourable excursion the strategy
    captured. ``1.0`` = exited at the high; ``0.5`` = gave back half;
    negative is impossible by construction (a winning trade has
    pnl > 0 and mfe ≥ pnl). Returns ``0.0`` if no winning trades or
    if all wins have ``mfe == 0`` (degenerate).
    """
    eligible = [t for t in trades if t.pnl > 0 and t.mfe > 0]
    if not eligible:
        return 0.0
    return sum(t.pnl / t.mfe for t in eligible) / len(eligible)


def adverse_efficiency(trades: Sequence[TradeExcursion]) -> float:
    """Average ``|pnl| / mae`` for losing trades.

    Tells you how much of the adverse excursion the loss captured.
    A losing trade with pnl == -mae stopped at the worst tick (1.0);
    a tighter stop yields ``< 1.0``. Returns ``0.0`` if no losing
    trades or all losses have ``mae == 0``.
    """
    eligible = [t for t in trades if t.pnl < 0 and t.mae > 0]
    if not eligible:
        return 0.0
    return sum(abs(t.pnl) / t.mae for t in eligible) / len(eligible)


__all__ = [
    "TradeExcursion",
    "adverse_efficiency",
    "edge_ratio",
    "max_mae",
    "max_mfe",
    "mean_mae",
    "mean_mfe",
    "trade_efficiency",
]
