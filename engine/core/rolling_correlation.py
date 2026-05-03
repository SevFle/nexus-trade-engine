"""Rolling correlation analytics (gh#89 follow-up).

Pairwise rolling Pearson correlation across portfolio return series.
Complements the static ``correlation_matrix`` in
``engine.core.portfolio_aggregator`` (#339) which computes a single
matrix over the full sample — these helpers compute the *time series*
of pairwise correlations so callers can plot regime shifts.

Output convention follows ``engine.core.rolling_metrics`` (#343):
same-length output with ``None`` for the first ``window - 1`` indices.

Coverage:

- ``rolling_correlation`` — single-pair time series.
- ``rolling_correlation_matrix`` — all pairs as a dict of lists.
- ``mean_pairwise_correlation`` — single rolling series of mean
  off-diagonal correlation across the group (the "diversification
  loss" indicator).

Out of scope:
- Spearman / Kendall rank correlations.
- Dynamic-conditional-correlation (DCC) GARCH.
- Eigenvalue-spectrum tracking (Marchenko-Pastur cleanup) — separate slice.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence


def _validate_window(window: int) -> None:
    if window < 2:
        raise ValueError("window must be >= 2")


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Pearson correlation. Returns ``0.0`` on length mismatch or zero variance."""
    n = len(xs)
    if n != len(ys) or n < 2:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0.0 or dy == 0.0:
        return 0.0
    return num / (dx * dy)


def rolling_correlation(
    a: Sequence[float], b: Sequence[float], window: int
) -> list[float | None]:
    """Pearson correlation of ``a`` and ``b`` over each trailing window.

    ``None`` for the first ``window - 1`` indices. Both inputs must be
    the same length; mismatch raises ``ValueError``. Series shorter
    than ``window`` return all-``None``.
    """
    _validate_window(window)
    if len(a) != len(b):
        raise ValueError(
            f"series length mismatch: {len(a)} vs {len(b)}"
        )
    n = len(a)
    out: list[float | None] = [None] * n
    if n < window:
        return out
    for i in range(window - 1, n):
        out[i] = _pearson(
            a[i - window + 1 : i + 1], b[i - window + 1 : i + 1]
        )
    return out


def rolling_correlation_matrix(
    return_series: Mapping[str, Sequence[float]], window: int
) -> dict[str, dict[str, list[float | None]]]:
    """All-pairs rolling correlation matrix.

    Returns ``{key_a: {key_b: [rolling_corr_at_each_bar, ...]}}``.
    Diagonal entries are ``1.0`` (after the first full window — earlier
    indices remain ``None``; constant-window slices yield ``0.0`` since
    correlation is undefined). Off-diagonal entries are symmetric. All
    series must have identical length.
    """
    _validate_window(window)
    keys = list(return_series.keys())
    if not keys:
        return {}
    lengths = {len(s) for s in return_series.values()}
    if len(lengths) > 1:
        raise ValueError(
            f"all return series must have equal length; got {sorted(lengths)}"
        )
    n = next(iter(lengths))
    out: dict[str, dict[str, list[float | None]]] = {k: {} for k in keys}
    for i, a in enumerate(keys):
        for j, b in enumerate(keys):
            if i == j:
                series_a = return_series[a]
                row: list[float | None] = [None] * n
                for idx in range(window - 1, n):
                    win = series_a[idx - window + 1 : idx + 1]
                    row[idx] = 1.0 if len(set(win)) > 1 else 0.0
                out[a][b] = row
            elif j < i:
                out[a][b] = list(out[b][a])
            else:
                out[a][b] = rolling_correlation(
                    return_series[a], return_series[b], window
                )
    return out


def mean_pairwise_correlation(
    return_series: Mapping[str, Sequence[float]], window: int
) -> list[float | None]:
    """Average off-diagonal correlation across the group, per bar.

    Returns a list of length ``len(returns)`` (the common series
    length) with ``None`` for the first ``window - 1`` indices.
    Returns ``[]`` for fewer than two series (no pairs to average).
    """
    _validate_window(window)
    keys = list(return_series.keys())
    if len(keys) < 2:
        return []
    lengths = {len(s) for s in return_series.values()}
    if len(lengths) > 1:
        raise ValueError(
            f"all return series must have equal length; got {sorted(lengths)}"
        )
    n = next(iter(lengths))
    if n < window:
        return [None] * n
    pair_series: list[list[float | None]] = []
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            pair_series.append(
                rolling_correlation(
                    return_series[keys[i]], return_series[keys[j]], window
                )
            )
    if not pair_series:
        return [None] * n
    out: list[float | None] = [None] * n
    for idx in range(window - 1, n):
        values = [s[idx] for s in pair_series if s[idx] is not None]
        if values:
            total = sum(values)  # type: ignore[arg-type]
            out[idx] = total / len(values)
    return out


__all__ = [
    "mean_pairwise_correlation",
    "rolling_correlation",
    "rolling_correlation_matrix",
]
