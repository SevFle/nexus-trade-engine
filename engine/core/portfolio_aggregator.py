"""Cross-portfolio aggregation helpers (gh#89).

Pure-function helpers operating on lightweight DTO inputs. The DB
models, REST routes and UI for portfolio groups + tags are deferred
follow-ups; this slice ships only the math layer that the eventual
``GET /api/v1/portfolio-groups/{id}/analytics`` endpoint will call.

Coverage:

- ``combined_nav`` — sum of NAVs across portfolios
- ``combined_equity_curve`` — element-wise sum of aligned equity series
- ``correlation_matrix`` — Pearson correlation of return series
- ``position_overlap`` — symbols held in 2+ portfolios with combined qty
- ``aggregate_exposure`` — group-level exposure rolled up by classifier
- ``filter_by_tags`` — set-membership filter for portfolio tags

Out of scope:
- Combined drawdown / Sharpe (call existing per-portfolio helpers on
  the combined equity curve from this module).
- ORM models for ``PortfolioGroup`` / ``portfolio_tags`` (deferred to
  a DB-shaped follow-up).
- REST surface and UI components.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence


@dataclass(frozen=True)
class PortfolioView:
    """Lightweight DTO consumed by aggregation helpers.

    The real ``Portfolio`` class lives in ``engine.core.portfolio``;
    aggregators take this minimal projection so they remain trivially
    testable without instantiating the full portfolio machinery.
    """

    portfolio_id: str
    nav: float
    tags: frozenset[str] = field(default_factory=frozenset)
    positions: Mapping[str, float] = field(default_factory=dict)


def combined_nav(portfolios: Iterable[PortfolioView]) -> float:
    """Sum of NAVs across portfolios. Empty input → ``0.0``."""
    return sum(p.nav for p in portfolios)


def combined_equity_curve(
    curves: Sequence[Sequence[float]],
) -> list[float]:
    """Element-wise sum of equally-sampled equity curves.

    All input curves must have identical length (the caller is
    responsible for re-sampling to a common time index). Empty input
    or empty curves return ``[]``.
    """
    if not curves:
        return []
    lengths = {len(c) for c in curves}
    if len(lengths) > 1:
        raise ValueError(f"all equity curves must have equal length; got {sorted(lengths)}")
    n = next(iter(lengths))
    if n == 0:
        return []
    return [sum(c[i] for c in curves) for i in range(n)]


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs)


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Pearson correlation. Returns 0.0 if either series has zero variance."""
    if len(xs) != len(ys):
        raise ValueError(f"series length mismatch: {len(xs)} vs {len(ys)}")
    if len(xs) < 2:
        return 0.0
    mx, my = _mean(xs), _mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=False))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0.0 or dy == 0.0:
        return 0.0
    return num / (dx * dy)


def correlation_matrix(
    return_series: Mapping[str, Sequence[float]],
) -> dict[str, dict[str, float]]:
    """Pearson correlation matrix across portfolio return series.

    Keys are portfolio IDs. Diagonal is always ``1.0`` (or ``0.0`` for
    a single-point series, since correlation is undefined). Off-diagonal
    entries are symmetric. All series must have identical length.
    """
    keys = list(return_series.keys())
    if not keys:
        return {}
    lengths = {len(s) for s in return_series.values()}
    if len(lengths) > 1:
        raise ValueError(f"all return series must have equal length; got {sorted(lengths)}")
    out: dict[str, dict[str, float]] = {k: {} for k in keys}
    for i, a in enumerate(keys):
        for j, b in enumerate(keys):
            if i == j:
                xs = return_series[a]
                if len(xs) >= 2 and len(set(xs)) > 1:
                    out[a][b] = 1.0
                else:
                    out[a][b] = 0.0
            elif j < i:
                out[a][b] = out[b][a]
            else:
                out[a][b] = _pearson(return_series[a], return_series[b])
    return out


def position_overlap(
    portfolios: Iterable[PortfolioView],
) -> dict[str, dict[str, float]]:
    """Find symbols held by 2+ portfolios.

    Returns ``symbol → {portfolio_id: quantity}`` containing only
    symbols held in more than one portfolio. Caller is responsible for
    deduping within a single portfolio before passing in.
    """
    by_symbol: dict[str, dict[str, float]] = {}
    for p in portfolios:
        for symbol, qty in p.positions.items():
            by_symbol.setdefault(symbol, {})[p.portfolio_id] = qty
    return {sym: holders for sym, holders in by_symbol.items() if len(holders) >= 2}


def aggregate_exposure(
    portfolios: Iterable[PortfolioView],
    classifier: Mapping[str, str],
    *,
    default_bucket: str = "unknown",
) -> dict[str, float]:
    """Roll up notional exposure across portfolios by classifier bucket.

    ``classifier`` maps ``symbol → bucket`` (e.g. asset class, sector,
    geography). Symbols missing from the classifier go to
    ``default_bucket``. Returns ``bucket → total_notional``.
    """
    out: dict[str, float] = {}
    for p in portfolios:
        for symbol, qty in p.positions.items():
            bucket = classifier.get(symbol, default_bucket)
            out[bucket] = out.get(bucket, 0.0) + qty
    return out


def filter_by_tags(
    portfolios: Iterable[PortfolioView],
    tags: Iterable[str],
    *,
    match: str = "any",
) -> list[PortfolioView]:
    """Filter portfolios by tag set.

    ``match='any'`` returns portfolios that have at least one requested
    tag. ``match='all'`` returns portfolios that have every requested
    tag. ``match='none'`` returns portfolios that have none of the
    requested tags. Empty ``tags`` returns the full input unchanged.
    """
    tag_set = frozenset(tags)
    if not tag_set:
        return list(portfolios)
    if match == "any":
        return [p for p in portfolios if p.tags & tag_set]
    if match == "all":
        return [p for p in portfolios if tag_set <= p.tags]
    if match == "none":
        return [p for p in portfolios if not (p.tags & tag_set)]
    raise ValueError(f"match must be 'any', 'all', or 'none'; got {match!r}")


__all__ = [
    "PortfolioView",
    "aggregate_exposure",
    "combined_equity_curve",
    "combined_nav",
    "correlation_matrix",
    "filter_by_tags",
    "position_overlap",
]
