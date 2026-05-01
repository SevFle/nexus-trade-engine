"""Multi-strategy signal aggregation (gh#21).

When multiple strategies run on the same portfolio they may emit
conflicting signals on the same symbol (BUY vs. SELL). The aggregator
collapses each strategy's :class:`SignalBatch` into a final per-symbol
:class:`Signal` according to a configurable :class:`AggregationMethod`.

HOLD semantics: ``Side.HOLD`` is treated as "no opinion" rather than as
an active vote. A strategy that emits HOLD for a symbol does not block
unanimous consensus among the strategies that did take a position, and
does not count toward majority-vote denominators. If every strategy
emits HOLD on a symbol, the aggregator emits a single HOLD signal so
downstream code has a record that the symbol was considered.

Aggregation methods
-------------------

UNANIMOUS
    Every strategy that voted (BUY or SELL) must agree. Disagreement
    emits HOLD.

MAJORITY
    Side with strictly more than half of the BUY-vs-SELL votes wins.
    Tie emits HOLD.

WEIGHTED
    Same as MAJORITY but each strategy's vote is multiplied by its
    weight from ``strategy_weights`` (default 1.0 for unknown
    strategies). Side with strictly higher weighted total wins.

PRIORITY
    Strategy with the highest weight that voted on the symbol wins
    outright. ``strategy_weights`` here is interpreted as priority
    (higher number = higher priority).

INDEPENDENT
    No aggregation. Concatenate every strategy's signals as-is and
    return them. The downstream consumer is expected to apply its own
    conflict resolution (typical use: each strategy targets a disjoint
    sub-portfolio).
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Iterable
from enum import Enum

from engine.core.signal import Side, Signal, SignalBatch


class AggregationMethod(str, Enum):
    UNANIMOUS = "unanimous"
    MAJORITY = "majority"
    WEIGHTED = "weighted"
    PRIORITY = "priority"
    INDEPENDENT = "independent"


class SignalAggregatorError(ValueError):
    """Bad aggregator configuration (unknown method, invalid weight)."""


_AGGREGATED_STRATEGY_ID = "aggregated"


class SignalAggregator:
    """Stateless aggregator. Construct once, reuse across cycles."""

    def __init__(
        self,
        method: AggregationMethod,
        strategy_weights: dict[str, float] | None = None,
    ) -> None:
        # Allow a string for ergonomics; reject anything outside the enum.
        try:
            self.method = AggregationMethod(method)
        except ValueError as exc:
            valid = ", ".join(m.value for m in AggregationMethod)
            raise SignalAggregatorError(
                f"unknown aggregation method {method!r}; expected one of {valid}"
            ) from exc

        weights = dict(strategy_weights or {})
        for sid, w in weights.items():
            if not math.isfinite(w):
                raise SignalAggregatorError(
                    f"strategy_weights[{sid!r}] must be finite, got {w!r}"
                )
            if w < 0:
                raise SignalAggregatorError(
                    f"strategy_weights[{sid!r}] must be non-negative, got {w!r}"
                )
        self.weights = weights

    def aggregate(self, batches: Iterable[SignalBatch]) -> list[Signal]:
        if self.method is AggregationMethod.INDEPENDENT:
            return [s for b in batches for s in b.signals]

        # Group signals by symbol. We deliberately keep one signal per
        # (strategy, symbol); if a strategy emitted multiple signals on
        # the same symbol within a single batch, the LAST one wins -
        # that matches how a strategy's own internal tie-break should
        # already be reflected in the final emitted batch.
        per_symbol: dict[str, dict[str, Signal]] = defaultdict(dict)
        for batch in batches:
            for sig in batch.signals:
                per_symbol[sig.symbol][batch.strategy_id] = sig

        out: list[Signal] = []
        for _symbol, by_strategy in per_symbol.items():
            decision = self._decide(by_strategy)
            if decision is None:
                continue
            out.append(decision)
        return out

    # --- per-method decision functions ---------------------------------

    def _decide(self, by_strategy: dict[str, Signal]) -> Signal | None:
        if self.method is AggregationMethod.UNANIMOUS:
            return self._decide_unanimous(by_strategy)
        if self.method is AggregationMethod.MAJORITY:
            return self._decide_majority(by_strategy)
        if self.method is AggregationMethod.WEIGHTED:
            return self._decide_weighted(by_strategy)
        if self.method is AggregationMethod.PRIORITY:
            return self._decide_priority(by_strategy)
        # INDEPENDENT was handled in aggregate(); reaching here is a bug.
        raise SignalAggregatorError(  # pragma: no cover - defensive
            f"unhandled aggregation method {self.method!r}"
        )

    def _decide_unanimous(
        self, by_strategy: dict[str, Signal]
    ) -> Signal | None:
        active = [s for s in by_strategy.values() if s.side != Side.HOLD]
        if not active:
            template = next(iter(by_strategy.values()))
            return _hold_like(template)
        sides = {s.side for s in active}
        if len(sides) == 1:
            template = active[0]
            return _aggregated(template, template.side)
        return _hold_like(active[0])

    def _decide_majority(
        self, by_strategy: dict[str, Signal]
    ) -> Signal | None:
        return self._decide_weighted_with(
            by_strategy, weight_for=lambda _sid: 1.0
        )

    def _decide_weighted(
        self, by_strategy: dict[str, Signal]
    ) -> Signal | None:
        return self._decide_weighted_with(
            by_strategy, weight_for=lambda sid: self.weights.get(sid, 1.0)
        )

    def _decide_weighted_with(
        self,
        by_strategy: dict[str, Signal],
        *,
        weight_for: Callable[[str], float],
    ) -> Signal | None:
        totals: dict[Side, float] = defaultdict(float)
        for sid, sig in by_strategy.items():
            if sig.side == Side.HOLD:
                continue
            totals[sig.side] += weight_for(sid)

        if not totals:
            return _hold_like(next(iter(by_strategy.values())))

        ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
        if len(ranked) == 1 or ranked[0][1] > ranked[1][1]:
            winning_side = ranked[0][0]
            template = next(
                s for s in by_strategy.values() if s.side == winning_side
            )
            return _aggregated(template, winning_side)
        return _hold_like(next(iter(by_strategy.values())))

    def _decide_priority(
        self, by_strategy: dict[str, Signal]
    ) -> Signal | None:
        # Highest priority wins. Strategies absent from `weights` are
        # treated as priority 0, so a configured strategy always beats
        # an unconfigured one.
        active = [
            (self.weights.get(sid, 0.0), sid, sig)
            for sid, sig in by_strategy.items()
            if sig.side != Side.HOLD
        ]
        if not active:
            return _hold_like(next(iter(by_strategy.values())))
        active.sort(key=lambda t: t[0], reverse=True)
        _prio, _sid, sig = active[0]
        return _aggregated(sig, sig.side)


def _aggregated(template: Signal, side: Side) -> Signal:
    """Build a Signal whose side is the aggregator's decision but whose
    other metadata mirrors a representative input. ``strategy_id`` is
    overwritten so the audit log shows the aggregator, not the original
    strategy."""
    return template.model_copy(
        update={"side": side, "strategy_id": _AGGREGATED_STRATEGY_ID}
    )


def _hold_like(template: Signal) -> Signal:
    return _aggregated(template, Side.HOLD)


__all__ = [
    "AggregationMethod",
    "SignalAggregator",
    "SignalAggregatorError",
]
