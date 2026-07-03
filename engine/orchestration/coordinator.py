"""Multi-strategy coordination: register, evaluate, aggregate.

A single, self-contained entry point that combines a strategy registry
(with optional per-strategy weights) and a signal aggregator into one
conflict-free decision set.

Pipeline::

    orchestrator.evaluate_all(market_data, cost_model, policy)
        1. run every registered strategy's ``evaluate()`` against the
           SAME ``market_data`` and a SHARED ``cost_model`` (each gets
           its own deep copy so a misbehaving plugin can't mutate a
           sibling's inputs);
        2. collect the emitted :class:`~engine.core.signal.Signal`
           objects into one flat list;
        3. hand them to :class:`SignalAggregator`, which collapses the
           per-symbol signals into a single decision per symbol using
           one of three policies.

Scope is deliberately tight: signal aggregation only. There is **no**
capital allocation, position sizing, or rebalancing here — those are
later-stage concerns. This module answers exactly one question: "given
what every strategy said, what is the single intent per symbol?"

Aggregation policies
--------------------

``MAJORITY_VOTE`` (default)
    Each non-HOLD signal casts one equal vote. A side wins only with a
    *strict* majority (> half of the BUY-vs-SELL votes); a tie emits
    HOLD. ``HOLD`` abstains and never counts toward the denominator.

``WEIGHTED_AVERAGE``
    Each signal's vote is multiplied by its strategy's registered
    weight (default 1.0). The side with the strictly higher weighted
    total wins; a tie emits HOLD. The resolved ``weight`` reflects the
    net signed conviction (sum of buy weights minus sum of sell
    weights) clamped to ``[0, 1]``.

``PRIORITY``
    The strategy with the highest registered weight that expressed a
    non-HOLD opinion wins outright. Two strategies tied for the top
    priority on opposing sides resolve to HOLD (stalemate → no action).

Relationship to the rest of the codebase
    :mod:`engine.core.signal_aggregator` solves the same per-symbol
    voting problem but operates on :class:`~engine.core.signal.SignalBatch`
    objects. This module owns the *flat-signal* variant plus the
    registry/evaluation driver, so callers that work with a plain
    ``list[Signal]`` have one cohesive component instead of two.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import math
from collections import defaultdict
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import structlog

from engine.core.signal import Side, Signal

if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import Protocol

    from engine.core.cost_model import ICostModel

    class StrategyLike(Protocol):
        """Structural contract for anything the coordinator can run.

        ``id`` may be a string attribute or a no-arg callable/property.
        ``evaluate`` may be sync OR async; the coordinator awaits
        awaitable results transparently.
        """

        @property
        def id(self) -> str: ...

        def evaluate(
            self, market_data: Any, cost_model: Any
        ) -> list[Signal] | Any: ...


logger = structlog.get_logger()


class CoordinatorError(ValueError):
    """Bad coordinator configuration: unknown policy, invalid weight,
    duplicate registration, or a strategy missing its ``id`` /
    ``evaluate``."""


class AggregationPolicy(StrEnum):
    """How opposing signals on the same symbol are reconciled."""

    MAJORITY_VOTE = "majority_vote"
    WEIGHTED_AVERAGE = "weighted_average"
    PRIORITY = "priority"


# Stamped onto aggregated output so audit code can distinguish a
# coordinator-level decision from a raw per-strategy signal.
_COORDINATOR_STRATEGY_ID = "coordinator"

_DEFAULT_WEIGHT = 1.0
# Net-position / weighted-tie dead band. Float dust such as
# ``1.0 - 0.5 - 0.5 == 1.1e-16`` must not flip a HOLD into a phantom
# signal, so any |margin| below this counts as an exact tie.
_EPSILON = 1e-9


def _strategy_id(strategy: StrategyLike) -> str:
    """Resolve a strategy's id (string attribute or no-arg callable) and
    verify it exposes a callable ``evaluate``."""
    sid = getattr(strategy, "id", None)
    if callable(sid):
        sid = sid()
    if not isinstance(sid, str) or not sid:
        raise CoordinatorError(
            f"strategy must expose a non-empty string `id`, got {sid!r}"
        )
    if not callable(getattr(strategy, "evaluate", None)):
        raise CoordinatorError(
            f"strategy {sid!r} must expose a callable `evaluate` method"
        )
    return sid


def _validate_weight(weight: float, strategy_id: str) -> float:
    try:
        value = float(weight)
    except (TypeError, ValueError) as exc:
        raise CoordinatorError(
            f"weight for strategy {strategy_id!r} must be a number, got {weight!r}"
        ) from exc
    if not math.isfinite(value):
        raise CoordinatorError(
            f"weight for strategy {strategy_id!r} must be finite, got {weight!r}"
        )
    if value < 0:
        raise CoordinatorError(
            f"weight for strategy {strategy_id!r} must be non-negative, got {value!r}"
        )
    return value


def _resolve_policy(policy: object) -> AggregationPolicy:
    try:
        return AggregationPolicy(policy)
    except ValueError as exc:
        valid = ", ".join(sorted(p.value for p in AggregationPolicy))
        raise CoordinatorError(
            f"unknown aggregation policy {policy!r}; expected one of {valid}"
        ) from exc


class SignalAggregator:
    """Merge a flat ``list[Signal]`` into one decision per symbol.

    Stateless apart from its policy + weights: construct once and reuse
    across evaluation cycles. ``weights`` maps ``strategy_id`` → weight
    and is only consulted by ``WEIGHTED_AVERAGE`` and ``PRIORITY``;
    strategies absent from the map default to
    :data:`_DEFAULT_WEIGHT` (1.0).
    """

    def __init__(
        self,
        policy: str | AggregationPolicy = AggregationPolicy.MAJORITY_VOTE,
        weights: dict[str, float] | None = None,
    ) -> None:
        self.policy = _resolve_policy(policy)
        validated: dict[str, float] = {}
        for sid, w in (weights or {}).items():
            validated[sid] = _validate_weight(w, sid)
        self.weights = validated

    def aggregate(self, signals: Iterable[Signal]) -> list[Signal]:
        """Collapse ``signals`` to a single decision per symbol.

        Returns a list with one :class:`Signal` per symbol (in first-seen
        order). Each output signal's ``strategy_id`` is stamped
        ``"coordinator"`` and its ``metadata`` records the contributing
        strategy ids under ``"coordinator_sources"`` so the decision
        stays auditable.
        """
        per_symbol: dict[str, list[Signal]] = defaultdict(list)
        for sig in signals:
            per_symbol[sig.symbol].append(sig)

        return [self._resolve(group) for group in per_symbol.values()]

    # -- per-policy decision functions --------------------------------

    def _resolve(self, group: list[Signal]) -> Signal:
        active = [s for s in group if s.side != Side.HOLD]
        # No strategy took a position → HOLD. We still emit a signal so
        # downstream consumers have a record that the symbol was seen.
        if not active:
            return _resolved(group[0], Side.HOLD, active)
        if self.policy is AggregationPolicy.MAJORITY_VOTE:
            return self._majority(active)
        if self.policy is AggregationPolicy.WEIGHTED_AVERAGE:
            return self._weighted(active)
        return self._priority(active)

    def _majority(self, active: list[Signal]) -> Signal:
        counts: dict[Side, int] = defaultdict(int)
        for sig in active:
            counts[sig.side] += 1
        total = sum(counts.values())
        # Strict majority: strictly more than half of the cast votes.
        # A 1-vs-1 tie (or any non-strict plurality) therefore HOLDs.
        winning_side = max(counts, key=lambda side: counts[side])
        if counts[winning_side] > total / 2:
            template = next(s for s in active if s.side == winning_side)
            return _resolved(template, winning_side, active)
        return _resolved(active[0], Side.HOLD, active)

    def _weighted(self, active: list[Signal]) -> Signal:
        totals: dict[Side, float] = defaultdict(float)
        for sig in active:
            totals[sig.side] += self.weights.get(sig.strategy_id, _DEFAULT_WEIGHT)
        ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
        if len(ranked) == 1 or ranked[0][1] > ranked[1][1] + _EPSILON:
            winning_side = ranked[0][0]
            template = next(s for s in active if s.side == winning_side)
            # Net signed conviction: BUY contributes positively, SELL
            # negatively. Clamp to the [0, 1] allocation band the Signal
            # schema enforces.
            net = totals.get(Side.BUY, 0.0) - totals.get(Side.SELL, 0.0)
            return _resolved(template, winning_side, active, weight=min(abs(net), 1.0))
        return _resolved(active[0], Side.HOLD, active)

    def _priority(self, active: list[Signal]) -> Signal:
        # Highest-weight strategy wins outright. Strategies absent from
        # ``weights`` default to 1.0, so an explicitly weighted strategy
        # can outrank an unconfigured one. Two strategies tied for the
        # top weight on opposing sides HOLD — relying on dict iteration
        # order for a tie-break would be non-deterministic.
        top = max(self.weights.get(s.strategy_id, _DEFAULT_WEIGHT) for s in active)
        winners = [s for s in active if self.weights.get(s.strategy_id, _DEFAULT_WEIGHT) == top]
        if len({s.side for s in winners}) > 1:
            return _resolved(winners[0], Side.HOLD, active)
        winner = winners[0]
        return _resolved(winner, winner.side, active)


def _resolved(
    template: Signal,
    side: Side,
    active: Iterable[Signal] = (),
    *,
    weight: float | None = None,
) -> Signal:
    """Build a coordinator Signal: ``side`` / ``weight`` reflect the
    decision, other fields mirror ``template``. ``strategy_id`` is
    overwritten so the audit trail shows the coordinator rather than a
    single source strategy, and ``metadata`` is deep-copied (nested
    dicts/lists included) so downstream mutation cannot leak back into
    the original strategy's signal object. ``coordinator_sources``
    records the contributing (non-HOLD) strategy ids for traceability.
    """
    metadata = dict(copy.deepcopy(template.metadata) or {})
    metadata["coordinator_sources"] = [s.strategy_id for s in active]
    return template.model_copy(
        update={
            "side": side,
            "weight": template.weight if weight is None else weight,
            "strategy_id": _COORDINATOR_STRATEGY_ID,
            "metadata": metadata,
        }
    )


class StrategyOrchestrator:
    """Owns a weighted set of strategies and merges their signals.

    Construct once, :meth:`register` the strategies you want to run
    (each with an optional ``weight``), then call :meth:`evaluate_all`
    each evaluation cycle with the shared market data and cost model.

    A single failing or timed-out strategy is **isolated**: its error is
    recorded (:attr:`last_errors`) and logged, and the remaining
    strategies still contribute to the aggregated decision. One bad
    plugin can never abort the whole cycle.
    """

    def __init__(self, eval_timeout: float = 30.0) -> None:
        # Insertion-ordered → deterministic evaluate_all iteration.
        self._strategies: dict[str, StrategyLike] = {}
        self._weights: dict[str, float] = {}
        # Per-strategy ``evaluate()`` wall-clock cap. A strategy whose
        # async result exceeds it is cancelled, recorded in
        # ``last_errors``, and skipped — never stalling the cycle. The
        # synchronous call frame is never bounded: it has already
        # returned by the time the cap could apply to its awaitable.
        try:
            value = float(eval_timeout)
        except (TypeError, ValueError) as exc:
            raise CoordinatorError(
                f"eval_timeout must be a number, got {eval_timeout!r}"
            ) from exc
        if not math.isfinite(value) or value <= 0:
            raise CoordinatorError(
                f"eval_timeout must be a finite, positive number, got {eval_timeout!r}"
            )
        self._eval_timeout = value
        # Most recent evaluate_all() outputs (for inspection / testing).
        self._last_signals: list[Signal] = []
        self._last_errors: dict[str, str] = {}

    # -- registry introspection ---------------------------------------

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

    @property
    def last_signals(self) -> list[Signal]:
        """Unified signal set returned by the most recent
        :meth:`evaluate_all` (empty before the first call)."""
        return list(self._last_signals)

    @property
    def last_errors(self) -> dict[str, str]:
        """``{strategy_id: error_message}`` from the most recent
        :meth:`evaluate_all`. Empty when every strategy succeeded."""
        return dict(self._last_errors)

    # -- registration -------------------------------------------------

    def register(self, strategy: StrategyLike, weight: float = _DEFAULT_WEIGHT) -> None:
        """Register ``strategy`` with the given ``weight`` (default 1.0).

        Re-registering an id updates both the strategy instance and its
        weight (a warning is logged so the overwrite is never silent).
        ``weight`` must be a finite, non-negative number.
        """
        sid = _strategy_id(strategy)
        value = _validate_weight(weight, sid)
        if sid in self._strategies:
            logger.warning(
                "coordinator.reregister",
                strategy_id=sid,
                old_weight=self._weights.get(sid),
                new_weight=value,
            )
        self._strategies[sid] = strategy
        self._weights[sid] = value
        logger.info("coordinator.registered", strategy_id=sid, weight=value)

    def unregister(self, strategy_id: str) -> bool:
        """Remove ``strategy_id``. Returns True if it was present."""
        existed = self._strategies.pop(strategy_id, None) is not None
        self._weights.pop(strategy_id, None)
        if existed:
            logger.info("coordinator.unregistered", strategy_id=strategy_id)
        return existed

    # -- evaluation ---------------------------------------------------

    async def evaluate_all(
        self,
        market_data: Any,
        cost_model: ICostModel,
        policy: str | AggregationPolicy = AggregationPolicy.MAJORITY_VOTE,
    ) -> list[Signal]:
        """Evaluate every registered strategy against the same
        ``market_data`` and ``cost_model`` and return the unified,
        conflict-free signal set.

        Parameters
        ----------
        market_data, cost_model:
            Forwarded to each strategy's ``evaluate``. Each strategy
            receives its own deep copy so one plugin mutating them
            cannot leak to its siblings or the caller's originals.
        policy:
            Aggregation policy for the per-symbol merge. One of
            ``majority_vote`` (default), ``weighted_average``, or
            ``priority``.

        Returns
        -------
        list[Signal]
            One decision per symbol (see :class:`SignalAggregator`).
            An empty registry — or every strategy emitting no signals —
            yields an empty list.

        Raises
        ------
        CoordinatorError
            If ``policy`` is not a known :class:`AggregationPolicy`.
        """
        resolved_policy = _resolve_policy(policy)

        # Empty registry → no-op short-circuit before any work.
        if not self._strategies:
            self._last_signals = []
            self._last_errors = {}
            return []

        collected: list[Signal] = []
        errors: dict[str, str] = {}
        # Snapshot the registry before iterating: a strategy that
        # registers/unregisters a sibling mid-cycle (e.g. from inside
        # its own evaluate) must not mutate the dict we walk, which
        # would otherwise raise "dictionary changed size during
        # iteration". Late additions run on the *next* cycle.
        for sid, strategy in list(self._strategies.items()):
            try:
                # Deep copy INSIDE the per-strategy try block so an
                # unpicklable ``market_data``/``cost_model`` is reported
                # as a single strategy error (and skipped) rather than
                # crashing the whole cycle.
                md = copy.deepcopy(market_data)
                cm = copy.deepcopy(cost_model)
                raw = strategy.evaluate(md, cm)
            except Exception as exc:  # isolate any plugin failure
                logger.exception("coordinator.strategy_failed", strategy_id=sid)
                errors[sid] = f"{type(exc).__name__}: {exc}"
                continue
            try:
                # Support sync (returns list) and async (returns a
                # coroutine) strategies transparently. Only the async
                # path is bounded by the per-strategy timeout. The
                # ``TimeoutError`` catch is narrowed to wrap ONLY
                # ``asyncio.wait_for``: ``wait_for`` raises it (alias of
                # the builtin ``TimeoutError`` on 3.11+) when its
                # wall-clock deadline elapses, the one case we treat as
                # a genuine timeout. A sync strategy that raises the
                # builtin itself was already handled above.
                if inspect.isawaitable(raw):
                    raw = await asyncio.wait_for(raw, timeout=self._eval_timeout)
            except TimeoutError:
                logger.warning(
                    "coordinator.strategy_timeout",
                    strategy_id=sid,
                    timeout=self._eval_timeout,
                )
                errors[sid] = f"TimeoutError: evaluate exceeded {self._eval_timeout}s timeout"
                continue
            if raw:
                collected.extend(raw)

        aggregator = SignalAggregator(resolved_policy, self._weights)
        unified = aggregator.aggregate(collected)
        self._last_signals = unified
        self._last_errors = errors
        return unified


__all__ = [
    "AggregationPolicy",
    "CoordinatorError",
    "SignalAggregator",
    "StrategyOrchestrator",
]
