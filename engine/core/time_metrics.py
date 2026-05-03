"""Time-based return analytics (gh#97 follow-up).

Pure-function helpers operating on either:

- A flat sequence of period returns (for best/worst/positive %).
- A sequence of ``(date, return)`` pairs (for month/week roll-ups).

KPIs covered (numbers from gh#97 taxonomy):

-  4  Monthly returns       — aggregate_returns_by_month
-  5  Weekly returns        — aggregate_returns_by_week
-  7  Best Day              — compute_best_period
-  8  Worst Day             — compute_worst_period
-  9  Best Month            — derived from monthly aggregate + best_period
- 10  Worst Month           — derived from monthly aggregate + worst_period
- 11  Positive Days %       — compute_positive_period_pct
- 12  Positive Months %     — derived

Period aggregation uses *compounded* returns per the financial
convention: monthly = ∏(1 + daily) - 1. Operators who need additive
weighting can switch to a custom aggregator (deferred — not shipped
in this slice).

Out of scope (explicit follow-ups):
- Calendar-period weighting (some platforms weight by trading days).
- Quarter / year aggregations.
- Period anchoring around a custom fiscal calendar.
- Distribution histograms (operators bin the monthly/weekly outputs
  themselves).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date


def compute_best_period(returns: Sequence[float]) -> float:
    """Maximum of ``returns``. Returns ``0.0`` on empty input."""
    if not returns:
        return 0.0
    return max(returns)


def compute_worst_period(returns: Sequence[float]) -> float:
    """Minimum of ``returns``. Returns ``0.0`` on empty input."""
    if not returns:
        return 0.0
    return min(returns)


def compute_positive_period_pct(returns: Sequence[float]) -> float:
    """Fraction of periods with strictly-positive return.

    Returns a value in ``[0.0, 1.0]``. Empty input → ``0.0``. Zero
    returns are *not* counted as positive (the convention used on
    every major analytics platform).
    """
    if not returns:
        return 0.0
    positives = sum(1 for r in returns if r > 0)
    return positives / len(returns)


def compute_negative_period_pct(returns: Sequence[float]) -> float:
    """Fraction of periods with strictly-negative return."""
    if not returns:
        return 0.0
    negatives = sum(1 for r in returns if r < 0)
    return negatives / len(returns)


def aggregate_returns_by_month(
    dated_returns: Sequence[tuple[date, float]],
) -> list[tuple[str, float]]:
    """Compound daily returns into monthly returns.

    ``dated_returns`` is a list of ``(date, daily_return)`` pairs (any
    order; we sort internally). Returns a list of ``(yyyy-mm,
    compounded_return)`` pairs in chronological order.

    Compounding: ``(1 + r_total) = ∏(1 + r_i)``; the function returns
    ``r_total``.
    """
    if not dated_returns:
        return []
    by_month: dict[str, float] = {}
    keys_seen: list[str] = []
    for d, r in sorted(dated_returns, key=lambda p: p[0]):
        key = f"{d.year:04d}-{d.month:02d}"
        if key not in by_month:
            by_month[key] = 1.0
            keys_seen.append(key)
        by_month[key] *= 1.0 + r
    return [(k, by_month[k] - 1.0) for k in keys_seen]


def aggregate_returns_by_week(
    dated_returns: Sequence[tuple[date, float]],
) -> list[tuple[str, float]]:
    """Compound daily returns into weekly returns (ISO 8601 week).

    Returns a list of ``(yyyy-Www, compounded_return)`` pairs in
    chronological order, e.g. ``2024-W23``. Uses ``date.isocalendar()``
    to assign each daily return to its ISO week.
    """
    if not dated_returns:
        return []
    by_week: dict[str, float] = {}
    keys_seen: list[str] = []
    for d, r in sorted(dated_returns, key=lambda p: p[0]):
        iso_year, iso_week, _ = d.isocalendar()
        key = f"{iso_year:04d}-W{iso_week:02d}"
        if key not in by_week:
            by_week[key] = 1.0
            keys_seen.append(key)
        by_week[key] *= 1.0 + r
    return [(k, by_week[k] - 1.0) for k in keys_seen]


__all__ = [
    "aggregate_returns_by_month",
    "aggregate_returns_by_week",
    "compute_best_period",
    "compute_negative_period_pct",
    "compute_positive_period_pct",
    "compute_worst_period",
]
