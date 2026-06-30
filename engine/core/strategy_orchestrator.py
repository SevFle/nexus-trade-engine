"""Strategy orchestrator.

Run many strategies against identical inputs and merge their emitted
signals into a single decision set.

The orchestrator owns three responsibilities that the lower-level
``SignalAggregator`` (gh#21) deliberately does not:

1. Registry - strategies are registered with a per-strategy ``weight``
   that the ``weighted`` aggregation mode consults.
2. Evaluation - every registered strategy is invoked with the *same*
   ``market_data`` and ``cost_model`` so cross-strategy comparisons are
   apples-to-apples. A single failing strategy is isolated: its error is
   recorded and the remaining strategies still contribute to the vote.
3. Aggregation dispatch - the collected per-strategy ``SignalBatch``
   objects are handed to ``SignalAggregator``, which already implements
   the per-symbol majority/weighted resolution logic with HOLD-as-abstain
   semantics. Reusing it keeps a single source of truth for tie handling.

Aggregation modes
-----------------

majority / majority_vote
    Each strategy that takes a position (BUY or SELL) casts one equal
    vote. A side wins only if it takes strictly more than half of the
    BUY-vs-SELL votes; a tie (e.g. 1 BUY vs 1 SELL) emits HOLD. HOLD
    signals abstain and do not count toward the denominator.

weighted
    Each strategy's vote is multiplied by its registered weight
    (default 1.0). The side with the strictly higher total weight wins;
    a tie emits HOLD. This lets a high-conviction strategy override a
    numerical majority.

Both modes return HOLD for a symbol when no strategy expresses an
opinion beyond HOLD, so downstream consumers always get a record that
the symbol was considered.

Relationship to SignalAggregator
    The orchestrator builds one ``SignalBatch`` per strategy from its
    raw signals, then delegates. It never reimplements voting math.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import structlog

from engine.core.signal import Side, Signal, SignalBatch
from engine.core.signal_aggregator import (
    AggregationMethod,
    SignalAggregator,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from typing import Protocol

    class StrategyLike(Protocol):
        """Structural contract for anything the orchestrator can run.

        ``id`` may be a string attribute or a no-arg callable/property.
        ``evaluate`` may be sync OR async; the orchestrator awaits
        awaitable results transparently.
        """

        @property
        def id(self) -> str: ...

        def evaluate(
            self, market_data: Any, cost_model: Any
        ) -> list[Signal] | Awaitable[list[Signal]]: ...


logger = structlog.get_logger()


class StrategyOrchestratorError(ValueError):
    """Bad orchestrator configuration: unknown aggregation mode,
    invalid weight, or a strategy missing its ``id`` / ``evaluate``."""


class AggregationMode(StrEnum):
    """Orchestrator-level aggregation selectors.

    ``MAJORITY`` and ``MAJORITY_VOTE`` are intentional aliases - the
    former reads naturally as the default, the latter matches the mode
    name used in design docs.
    """

    MAJORITY = "majority"
    MAJORITY_VOTE = "majority_vote"
    WEIGHTED = "weighted"


# Map orchestrator-level mode -> underlying SignalAggregator method.
# Both majority aliases collapse to the same one-vote-per-strategy
# majority; they exist purely for ergonomic call sites.
_MODE_TO_METHOD: dict[AggregationMode, AggregationMethod] = {
    AggregationMode.MAJORITY: AggregationMethod.MAJORITY,
    AggregationMode.MAJORITY_VOTE: AggregationMethod.MAJORITY,
    AggregationMode.WEIGHTED: AggregationMethod.WEIGHTED,
}


@dataclass(frozen=True)
class OrchestrationResult:
    """Outcome of one ``evaluate_all`` cycle.

    The result is intentionally richer than a bare ``list[Signal]``:
    ``batches`` gives full per-strategy provenance for audit/traceability,
    and ``errors`` reports any strategy that raised so a misbehaving
    plugin can never silently disappear from the record.
    """

    signals: list[Signal] = field(default_factory=list)
    batches: list[SignalBatch] = field(default_factory=list)
    aggregation: str = ""
    strategy_count: int = 0
    weights: dict[str, float] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def trade_signals(self) -> list[Signal]:
        """Aggregated signals that express a non-HOLD intent."""
        return [s for s in self.signals if s.side != Side.HOLD]

    @property
    def is_noop(self) -> bool:
        """True when no aggregated signal was produced at all (e.g. an
        empty registry, or every strategy returned no signals)."""
        return len(self.signals) == 0


def _strategy_id(strategy: StrategyLike) -> str:
    """Resolve a strategy's identifier, accepting either a string
    attribute or a no-arg callable/property."""
    sid = getattr(strategy, "id", None)
    if callable(sid):
        sid = sid()
    if not isinstance(sid, str) or not sid:
        raise StrategyOrchestratorError(
            f"strategy must expose a non-empty string `id`, got {sid!r}"
        )
    return sid


def _validate_weight(weight: float, strategy_id: str) -> float:
    try:
        value = float(weight)
    except (TypeError, ValueError) as exc:
        raise StrategyOrchestratorError(
            f"weight for strategy {strategy_id!r} must be a number, got {weight!r}"
        ) from exc
    if not math.isfinite(value):
        raise StrategyOrchestratorError(
            f"weight for strategy {strategy_id!r} must be finite, got {weight!r}"
        )
    if value < 0:
        raise StrategyOrchestratorError(
            f"weight for strategy {strategy_id!r} must be non-negative, got {value!r}"
        )
    return value


def _resolve_mode(aggregation: object) -> AggregationMode:
    try:
        return AggregationMode(aggregation)
    except ValueError as exc:
        valid = ", ".join(sorted(m.value for m in AggregationMode))
        raise StrategyOrchestratorError(
            f"unknown aggregation mode {aggregation!r}; expected one of {valid}"
        ) from exc


class StrategyOrchestrator:
    """Owns a weighted set of strategies and merges their signals.

    Construct once, :meth:`register` the strategies you want to run, then
    call :meth:`evaluate_all` each evaluation cycle with the shared
    market data and cost model.
    """

    def __init__(self, eval_timeout: float = 30.0) -> None:
        # Insertion-ordered so evaluate_all iterates deterministically.
        self._strategies: dict[str, StrategyLike] = {}
        self._weights: dict[str, float] = {}
        # Per-strategy ``evaluate()`` wall-clock cap. A strategy that
        # exceeds it is recorded as an error result rather than stalling
        # the whole cycle. 30s matches the platform's overall strategy
        # SLA; tighten via the constructor for latency-sensitive paths.
        try:
            value = float(eval_timeout)
        except (TypeError, ValueError) as exc:
            raise StrategyOrchestratorError(
                f"eval_timeout must be a number, got {eval_timeout!r}"
            ) from exc
        if not math.isfinite(value) or value <= 0:
            raise StrategyOrchestratorError(
                f"eval_timeout must be a finite, positive number, got {eval_timeout!r}"
            )
        self._eval_timeout = value

    # -- registry introspection ----------------------------------------

    def __len__(self) -> int:
        return len(self._strategies)

    def __contains__(self, strategy_id: object) -> bool:
        return strategy_id in self._strategies

    @property
    def strategy_ids(self) -> list[str]:
        """Registered strategy ids in registration order."""
        return list(self._strategies)

    @property
    def weights(self) -> dict[str, float]:
        """Snapshot of the per-strategy weights."""
        return dict(self._weights)

    def get_weight(self, strategy_id: str) -> float | None:
        """Weight for ``strategy_id``, or ``None`` if unregistered."""
        return self._weights.get(strategy_id)

    # -- registration --------------------------------------------------

    def register(self, strategy: StrategyLike, weight: float = 1.0) -> None:
        """Register ``strategy`` with the given ``weight`` (default 1.0).

        Re-registering an id updates both the strategy instance and its
        weight (a warning is logged so the overwrite is never silent).
        ``weight`` must be a finite, non-negative number.
        """
        sid = _strategy_id(strategy)
        if not callable(getattr(strategy, "evaluate", None)):
            raise StrategyOrchestratorError(
                f"strategy {sid!r} must expose a callable `evaluate` method"
            )
        value = _validate_weight(weight, sid)
        if sid in self._strategies:
            logger.warning(
                "orchestrator.reregister",
                strategy_id=sid,
                old_weight=self._weights.get(sid),
                new_weight=value,
            )
        self._strategies[sid] = strategy
        self._weights[sid] = value
        logger.info("orchestrator.registered", strategy_id=sid, weight=value)

    def unregister(self, strategy_id: str) -> bool:
        """Remove ``strategy_id``. Returns True if it was present."""
        existed = self._strategies.pop(strategy_id, None) is not None
        self._weights.pop(strategy_id, None)
        if existed:
            logger.info("orchestrator.unregistered", strategy_id=strategy_id)
        return existed

    # -- evaluation ----------------------------------------------------

    async def evaluate_all(
        self,
        market_data: Any,
        cost_model: Any,
        aggregation: str = AggregationMode.MAJORITY.value,
    ) -> OrchestrationResult:
        """Evaluate every registered strategy against the same
        ``market_data`` and ``cost_model`` and aggregate the results.

        Parameters
        ----------
        market_data, cost_model:
            Forwarded to each strategy's ``evaluate`` as an independent
            deep copy, so one strategy mutating them cannot leak to its
            siblings or the caller's originals.
        aggregation:
            One of ``majority`` (default), ``majority_vote`` (alias), or
            ``weighted``.

        Returns
        -------
        OrchestrationResult
            ``signals`` holds the aggregated decisions; ``batches`` the
            raw per-strategy signals; ``errors`` maps any failed
            strategy id (raised *or* timed out) to its error message.
            A strategy that exceeds the configured ``eval_timeout`` is
            reported as a ``TimeoutError`` entry rather than stalling
            the cycle.

        Raises
        ------
        StrategyOrchestratorError
            If ``aggregation`` is not a known mode.
        SignalAggregatorError
            If ``weighted`` is used with all-zero weights (a genuine
            misconfiguration surfaced from the aggregator).
        """
        mode = _resolve_mode(aggregation)

        # Empty registry -> no-op. Short-circuit before building an
        # aggregator so callers get a clean, empty result.
        if not self._strategies:
            return OrchestrationResult(
                signals=[],
                batches=[],
                aggregation=mode.value,
                strategy_count=0,
                weights={},
                errors={},
            )

        batches: list[SignalBatch] = []
        errors: dict[str, str] = {}
        # Snapshot the registry before iterating. A strategy that
        # registers / unregisters a sibling mid-cycle (e.g. from inside
        # its own evaluate) must not mutate the dict we are walking,
        # which would otherwise raise "dictionary changed size during
        # iteration". Strategies added during this cycle are
        # intentionally excluded from it - they will run on the next.
        for sid, strategy in list(self._strategies.items()):
            # Hand each strategy its own deep copy so a misbehaving
            # plugin that mutates market_data / cost_model cannot poison
            # its siblings or the caller's originals. The copies are
            # recreated per strategy so cross-strategy comparisons stay
            # apples-to-apples.
            md = copy.deepcopy(market_data)
            cm = copy.deepcopy(cost_model)
            try:
                raw = strategy.evaluate(md, cm)
                # Support both sync (returns list) and async (returns
                # coroutine) strategies transparently. Only the async
                # path is bounded by the per-strategy timeout - a sync
                # call has already completed by the time we get here.
                if inspect.isawaitable(raw):
                    raw = await asyncio.wait_for(raw, timeout=self._eval_timeout)
            except TimeoutError as exc:
                # Catch TimeoutError before the generic handler below:
                # it is a subclass of OSError/Exception and must be
                # reported distinctly as a timeout, not a crash.
                logger.warning(
                    "orchestrator.strategy_timeout",
                    strategy_id=sid,
                    timeout=self._eval_timeout,
                )
                errors[sid] = (
                    f"{type(exc).__name__}: evaluate exceeded {self._eval_timeout}s timeout"
                )
                continue
            except Exception as exc:
                logger.exception("orchestrator.strategy_failed", strategy_id=sid)
                errors[sid] = f"{type(exc).__name__}: {exc}"
                continue
            signals = list(raw) if raw else []
            batches.append(SignalBatch(strategy_id=sid, signals=signals))

        aggregator = SignalAggregator(_MODE_TO_METHOD[mode], self._weights)
        aggregated = aggregator.aggregate(batches)
        return OrchestrationResult(
            signals=aggregated,
            batches=batches,
            aggregation=mode.value,
            strategy_count=len(self._strategies),
            weights=dict(self._weights),
            errors=errors,
        )


__all__ = [
    "AggregationMode",
    "OrchestrationResult",
    "StrategyLike",
    "StrategyOrchestrator",
    "StrategyOrchestratorError",
]
