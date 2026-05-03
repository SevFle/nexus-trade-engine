"""Drawdown duration + recovery analytics (gh#97 follow-up).

Pure-function helpers operating on equity curves. Complements the
class-bound helpers in ``engine.core.metrics.PerformanceMetrics`` by
exposing the same primitives as testable module-level functions and
adding episode enumeration that the existing report layer doesn't have.

Coverage (numbers from gh#97 taxonomy):

- 18  Average drawdown        — derivable from ``drawdown_episodes``
- 19  Max drawdown duration   — ``max_drawdown_duration``
- 24  Recovery time           — ``time_to_recovery``
- 25  Underwater curve        — ``underwater_curve``
- 33  Drawdown count          — ``len(drawdown_episodes(...))``

A drawdown *episode* runs from one all-time-high (the peak) down to a
trough and back to the next all-time-high (the recovery). An episode
is *open* when the equity curve has not yet recovered to its peak by
the end of the input — its ``recovery_idx`` is ``None``.

Out of scope:
- Calendar-time conversion (these helpers count *periods*, i.e. list
  indices). The caller knows whether a period is one day, one bar,
  etc.
- Conditional drawdown at risk (CDaR) — separate skewness slice.
- Drawdown-duration distribution histograms (caller bins themselves).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True)
class DrawdownEpisode:
    """One peak → trough → (recovery | open) drawdown episode.

    ``peak_idx`` — index of the all-time high that started the episode.
    ``trough_idx`` — index of the lowest point during the episode.
    ``recovery_idx`` — index of the bar that returned to ``peak`` value;
    ``None`` if the curve has not yet recovered by the last input.
    ``depth_pct`` — peak-to-trough decline as a positive fraction
    (e.g. ``0.10`` = 10 % drawdown).
    """

    peak_idx: int
    trough_idx: int
    recovery_idx: int | None
    depth_pct: float

    @property
    def duration(self) -> int:
        """Bars from peak to recovery (or to trough if open)."""
        end = self.recovery_idx if self.recovery_idx is not None else self.trough_idx
        return end - self.peak_idx

    @property
    def time_to_trough(self) -> int:
        return self.trough_idx - self.peak_idx

    @property
    def is_open(self) -> bool:
        return self.recovery_idx is None


def underwater_curve(equity: Sequence[float]) -> list[float]:
    """Per-bar percent-below-all-time-high (a non-positive series).

    ``0.0`` when the bar matches the running peak. Empty input → empty
    output. Non-positive peaks (zero or negative) yield ``0.0`` for that
    bar by convention.
    """
    if not equity:
        return []
    out: list[float] = []
    peak = equity[0]
    for v in equity:
        peak = max(peak, v)
        if peak <= 0:
            out.append(0.0)
        else:
            out.append((v - peak) / peak)
    return out


def drawdown_episodes(equity: Sequence[float]) -> list[DrawdownEpisode]:
    """Enumerate every drawdown episode in chronological order.

    Returns an empty list for fewer than two inputs or a strictly
    monotonically increasing series. The final episode may be open
    (``recovery_idx is None``) when the curve has not recovered by
    the last input.
    """
    if len(equity) < 2:  # noqa: PLR2004
        return []
    episodes: list[DrawdownEpisode] = []
    peak_value = equity[0]
    peak_idx = 0
    trough_idx = 0
    trough_value = equity[0]
    in_drawdown = False
    for i in range(1, len(equity)):
        v = equity[i]
        if not in_drawdown:
            if v < peak_value:
                in_drawdown = True
                trough_value = v
                trough_idx = i
            elif v > peak_value:
                peak_value = v
                peak_idx = i
        else:
            if v < trough_value:
                trough_value = v
                trough_idx = i
            if v >= peak_value:
                depth = (peak_value - trough_value) / peak_value if peak_value > 0 else 0.0
                episodes.append(
                    DrawdownEpisode(
                        peak_idx=peak_idx,
                        trough_idx=trough_idx,
                        recovery_idx=i,
                        depth_pct=depth,
                    )
                )
                in_drawdown = False
                peak_value = v
                peak_idx = i
    if in_drawdown:
        depth = (peak_value - trough_value) / peak_value if peak_value > 0 else 0.0
        episodes.append(
            DrawdownEpisode(
                peak_idx=peak_idx,
                trough_idx=trough_idx,
                recovery_idx=None,
                depth_pct=depth,
            )
        )
    return episodes


def max_drawdown_duration(equity: Sequence[float]) -> int:
    """Longest peak-to-recovery (or peak-to-trough for open) span.

    Returns ``0`` for empty / single-point / monotonically increasing
    inputs.
    """
    eps = drawdown_episodes(equity)
    return max((e.duration for e in eps), default=0)


def time_to_recovery(equity: Sequence[float]) -> int | None:
    """Bars from the deepest trough to recovery.

    ``None`` if the deepest drawdown has not recovered by the last
    input. ``0`` for empty / monotonically increasing inputs (no
    drawdown). When multiple episodes share the maximum depth, the
    earliest one is used.
    """
    eps = drawdown_episodes(equity)
    if not eps:
        return 0
    deepest = max(eps, key=lambda e: e.depth_pct)
    if deepest.recovery_idx is None:
        return None
    return deepest.recovery_idx - deepest.trough_idx


def average_drawdown(equity: Sequence[float]) -> float:
    """Mean depth of all drawdown episodes (positive fraction).

    Returns ``0.0`` for inputs with no drawdowns. Open episodes count
    at their current depth.
    """
    eps = drawdown_episodes(equity)
    if not eps:
        return 0.0
    return sum(e.depth_pct for e in eps) / len(eps)


def current_drawdown_pct(equity: Sequence[float]) -> float:
    """Last-bar drawdown as a positive fraction below the running peak.

    ``0.0`` for empty input or for a bar that matches the running peak.
    """
    if not equity:
        return 0.0
    peak = max(equity)
    if peak <= 0:
        return 0.0
    return max((peak - equity[-1]) / peak, 0.0)


__all__ = [
    "DrawdownEpisode",
    "average_drawdown",
    "current_drawdown_pct",
    "drawdown_episodes",
    "max_drawdown_duration",
    "time_to_recovery",
    "underwater_curve",
]
