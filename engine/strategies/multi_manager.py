"""Multi-strategy manager with per-strategy capital allocation.

A :class:`MultiStrategyManager` owns four concerns that the lower-level
:mod:`engine.core.signal_aggregator` (a pure per-symbol voter) and
:mod:`engine.core.strategy_orchestrator` (a capital-free weighted voter)
deliberately do not:

1. **Registration with explicit ids** — strategies are registered under a
   caller-supplied ``strategy_id`` together with a per-strategy
   ``allocation_pct`` (a share of the manager's ``total_capital``). The
   id is the *source of truth* for provenance: every signal a strategy
   emits is re-tagged with its registered id, so a strategy that
   mislabels its own ``Signal.strategy_id`` can never impersonate a
   sibling or hide its origin.
2. **Per-strategy capital budgets** — each registration carries a dollar
   cap = ``total_capital * allocation_pct / 100``. The manager exposes
   these budgets for risk/UI consumers.
3. **Allocation-cap enforcement** — when a strategy emits signals whose
   collective target weight exceeds its allocation fraction
   (``allocation_pct / 100``), the manager scales *that strategy's*
   active (BUY/SELL) signal weights down proportionally so the cap is
   respected, HOLD signals abstaining. The original weights are never
   silently lost: the adjustment is recorded on the evaluation result.
4. **Provenance-preserving aggregation** — every emitted signal keeps its
   registered ``strategy_id`` (it is *not* collapsed to an
   ``"aggregated"`` sentinel), so downstream consumers can attribute
   each signal to the strategy that produced it.

The class owns no I/O and no global state — it is a synchronous registry
with one async entry point (:meth:`MultiStrategyManager.evaluate_all`).

Relationship to siblings
------------------------
:mod:`engine.core.strategy_orchestrator`
    Capital-free; merges to *one* decision per symbol via majority /
    weighted voting. The manager does *not* merge conflicting symbols —
    it forwards every strategy's signals, just allocation-capped and
    re-tagged. Use the orchestrator downstream if symbol-level conflict
    resolution is required.
:mod:`engine.portfolio.multi_strategy`
    Also capital-aware, but treats ``capital_weight`` as a *relative*
    share (normalised at lookup) and nets dollar exposure per symbol into
    a single merged position. The manager instead treats ``allocation_pct``
    as an *absolute* budget per strategy and preserves per-strategy
    provenance, which is the contract multi-strategy attribution and
    per-strategy risk limits need.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import math
from dataclasses import dataclass, field
from typing import Any

import structlog

from engine.core.signal import Side, Signal

logger = structlog.get_logger()


class MultiStrategyManagerError(ValueError):
    """Bad manager configuration: empty / non-string id, duplicate
    registration, non-callable ``evaluate``, a non-finite or out-of-range
    ``allocation_pct``, a non-finite ``total_capital`` / ``eval_timeout``,
    or an ``allocation_pct`` sum that would exceed 100%."""


#: Upper bound for ``allocation_pct`` (a percentage in ``[0, 100]``).
_MAX_PCT = 100.0

#: Stamped onto every re-tagged signal's ``metadata`` so consumers can
#: read the capital budget the strategy was operating under.
_META_CAP_PCT = "allocation_cap_pct"
_META_CAP_DOLLARS = "allocation_cap_dollars"
_META_CAPPED = "allocation_capped"
_META_ORIG_WEIGHT = "allocation_original_weight"


@dataclass(frozen=True)
class StrategyRegistration:
    """The per-strategy bookkeeping the manager wraps around each
    registered :class:`~sdk.nexus_sdk.strategy.IStrategy` instance.

    ``allocation_pct`` is the raw, caller-supplied percentage of
    ``total_capital``; ``allocation_cap`` is the derived dollar budget
    (``total_capital * allocation_pct / 100``).
    """

    strategy_id: str
    strategy: Any
    allocation_pct: float
    allocation_cap: float


@dataclass(frozen=True)
class MultiStrategyEvaluation:
    """Outcome of one :meth:`MultiStrategyManager.evaluate_all` cycle.

    ``signals`` holds *every* emitted signal (allocation-capped and
    re-tagged with its source strategy id); ``per_strategy_signals``
    preserves full per-strategy provenance; ``allocation_adjustments``
    reports which strategies had their weights scaled down to respect a
    cap; ``errors`` maps any failed strategy id (raised *or* timed out)
    to its error message so a misbehaving plugin can never silently
    disappear from the record.
    """

    signals: list[Signal] = field(default_factory=list)
    per_strategy_signals: dict[str, list[Signal]] = field(default_factory=dict)
    total_capital: float = 0.0
    allocation_caps: dict[str, float] = field(default_factory=dict)
    allocation_adjustments: dict[str, float] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def trade_signals(self) -> list[Signal]:
        """Emitted signals that express a non-HOLD intent."""
        return [s for s in self.signals if s.side != Side.HOLD]

    @property
    def is_noop(self) -> bool:
        """True when no signal was produced (empty registry, every
        strategy returned nothing, or every strategy failed)."""
        return not self.signals

    @property
    def strategy_count(self) -> int:
        """Number of strategies that contributed to this evaluation."""
        return len(self.per_strategy_signals)


# --------------------------------------------------------------------- #
# Validation helpers (module-private)
# --------------------------------------------------------------------- #


def _validate_strategy_id(strategy_id: Any) -> str:
    """Require a non-empty string id. Unlike the orchestrator, the id is
    *not* derived from ``strategy.id`` — it is an explicit registration
    parameter, so provenance is controlled by the caller, not the plugin.
    """
    if not isinstance(strategy_id, str):
        raise MultiStrategyManagerError(
            f"strategy_id must be a string, got {type(strategy_id).__name__}"
        )
    if not strategy_id.strip():
        raise MultiStrategyManagerError("strategy_id must be a non-empty string")
    return strategy_id


def _validate_strategy(strategy_id: str, strategy: Any) -> None:
    """Require a callable ``evaluate`` on ``strategy``."""
    if strategy is None:
        raise MultiStrategyManagerError(f"strategy {strategy_id!r} must not be None")
    if not callable(getattr(strategy, "evaluate", None)):
        raise MultiStrategyManagerError(
            f"strategy {strategy_id!r} must expose a callable `evaluate` method"
        )


def _finite(value: float, label: str) -> float:
    """Coerce ``value`` to ``float`` and require it to be a finite number.

    Numeric strings (e.g. ``"20"``) are rejected outright so a caller
    cannot smuggle in a value that ``float()`` would silently parse; only
    genuine ``int``/``float`` values are accepted. ``bool`` is rejected
    explicitly (it subclasses ``int`` and would otherwise pass through as
    ``1``/``0``).
    """
    if isinstance(value, bool):
        raise MultiStrategyManagerError(f"{label} must be a number, got bool {value!r}")
    if isinstance(value, str):
        raise MultiStrategyManagerError(f"{label} must be a number, got {value!r}")
    try:
        num = float(value)
    except (TypeError, ValueError) as exc:
        raise MultiStrategyManagerError(f"{label} must be a number, got {value!r}") from exc
    if not math.isfinite(num):
        raise MultiStrategyManagerError(f"{label} must be finite, got {value!r}")
    return num


def _validate_allocation_pct(allocation_pct: Any, strategy_id: str) -> float:
    """Validate ``allocation_pct``: a finite number in ``[0, 100]``.

    A percentage semantics (rather than a raw ``[0, 1]`` fraction) matches
    the parameter name and is the natural unit for risk-limit UIs. ``0``
    is allowed — it registers a strategy that is intentionally capital-
    starved (e.g. paused) while keeping its code path warm.
    """
    pct = _finite(allocation_pct, f"allocation_pct[{strategy_id!r}]")
    if pct < 0:
        raise MultiStrategyManagerError(
            f"allocation_pct for {strategy_id!r} must be non-negative, got {pct!r}"
        )
    if pct > _MAX_PCT:
        raise MultiStrategyManagerError(
            f"allocation_pct for {strategy_id!r} must be <= {_MAX_PCT}, got {pct!r}"
        )
    return pct


# --------------------------------------------------------------------- #
# The manager
# --------------------------------------------------------------------- #


class MultiStrategyManager:
    """A capital-aware, provenance-preserving registry of strategies.

    Construct once with a ``total_capital`` budget, :meth:`register` the
    strategies you want to run (each with an explicit ``strategy_id`` and
    an ``allocation_pct`` share of the budget), then call
    :meth:`evaluate_all` each cycle with the shared market data and cost
    model.

    Parameters
    ----------
    total_capital:
        Dollar budget backing the whole manager. Each strategy's cap is
        ``total_capital * allocation_pct / 100``. Must be finite and
        non-negative; ``0`` is allowed (every cap is then ``$0`` but
        allocation *fractions* are still enforced).
    eval_timeout:
        Per-strategy ``evaluate()`` wall-clock cap. A strategy that
        exceeds it is recorded as an error result rather than stalling
        the whole cycle. Must be finite and positive.
    max_strategies:
        Hard ceiling on the number of registered strategies. Guards
        against an unbounded registry silently degrading cycle latency.
    """

    def __init__(
        self,
        total_capital: float = 0.0,
        *,
        eval_timeout: float = 30.0,
        max_strategies: int = 50,
    ) -> None:
        self._total_capital = _finite(total_capital, "total_capital")
        if self._total_capital < 0:
            raise MultiStrategyManagerError(
                f"total_capital must be non-negative, got {self._total_capital!r}"
            )

        timeout = _finite(eval_timeout, "eval_timeout")
        if timeout <= 0:
            raise MultiStrategyManagerError(
                f"eval_timeout must be a finite, positive number, got {eval_timeout!r}"
            )
        self._eval_timeout = timeout

        max_n = int(max_strategies)
        if max_n < 1:
            raise MultiStrategyManagerError(f"max_strategies must be >= 1, got {max_strategies!r}")
        self._max_strategies = max_n

        # Insertion-ordered so evaluate_all iterates deterministically.
        self._registrations: dict[str, StrategyRegistration] = {}

    # -- introspection -------------------------------------------------

    def __len__(self) -> int:
        return len(self._registrations)

    def __contains__(self, strategy_id: object) -> bool:
        return strategy_id in self._registrations

    @property
    def total_capital(self) -> float:
        """Total dollar budget backing this manager."""
        return self._total_capital

    @property
    def strategy_ids(self) -> list[str]:
        """Registered strategy ids in registration order."""
        return list(self._registrations)

    @property
    def registrations(self) -> dict[str, StrategyRegistration]:
        """Snapshot of the per-strategy registration metadata."""
        return dict(self._registrations)

    @property
    def allocation_pcts(self) -> dict[str, float]:
        """Snapshot of the per-strategy allocation percentages."""
        return {sid: r.allocation_pct for sid, r in self._registrations.items()}

    @property
    def total_allocation_pct(self) -> float:
        """Sum of all registered ``allocation_pct`` values.

        May exceed 100 only transiently; :meth:`register` rejects any new
        registration whose addition would push the sum over 100, so after
        a successful registration the sum is always ``<= 100``.
        """
        return float(sum(r.allocation_pct for r in self._registrations.values()))

    def get_allocation_pct(self, strategy_id: str) -> float | None:
        """``allocation_pct`` for ``strategy_id``, or ``None`` if unregistered."""
        reg = self._registrations.get(strategy_id)
        return reg.allocation_pct if reg is not None else None

    def allocation_cap(self, strategy_id: str) -> float:
        """Dollar capital budget for ``strategy_id``.

        Equals ``total_capital * allocation_pct / 100``. Returns ``0.0``
        for unknown strategies, for a strategy registered with ``pct=0``,
        or when ``total_capital`` is ``0``.
        """
        reg = self._registrations.get(strategy_id)
        if reg is None:
            return 0.0
        return reg.allocation_cap

    def allocations(self) -> dict[str, float]:
        """Dollar capital budget for every registered strategy, keyed by id."""
        return {sid: r.allocation_cap for sid, r in self._registrations.items()}

    # -- registration --------------------------------------------------

    def register(
        self,
        strategy_id: str,
        strategy: Any,
        allocation_pct: float,
    ) -> StrategyRegistration:
        """Register ``strategy`` under ``strategy_id`` with ``allocation_pct``.

        Parameters
        ----------
        strategy_id:
            Caller-supplied unique key. It is the source of truth for
            provenance: emitted signals are re-tagged with this id.
        strategy:
            Any object exposing a callable ``evaluate`` (sync *or*
            async). The manager never derives the id from ``strategy.id``.
        allocation_pct:
            Share of ``total_capital`` this strategy may deploy, as a
            percentage in ``[0, 100]``. The sum of all registered pcts
            must not exceed 100.

        Returns
        -------
        StrategyRegistration
            The bookkeeping record created for the strategy.

        Raises
        ------
        MultiStrategyManagerError
            On a non-string / empty id, a duplicate id, a strategy
            without ``evaluate``, a non-finite / out-of-range
            ``allocation_pct``, an ``allocation_pct`` sum that would
            exceed 100, or a full registry.
        """
        sid = _validate_strategy_id(strategy_id)
        _validate_strategy(sid, strategy)
        pct = _validate_allocation_pct(allocation_pct, sid)

        if sid in self._registrations:
            raise MultiStrategyManagerError(f"strategy {sid!r} already registered")

        if len(self._registrations) >= self._max_strategies:
            raise MultiStrategyManagerError(
                f"max_strategies ({self._max_strategies}) reached; "
                f"unregister a strategy before adding {sid!r}"
            )

        # Reject an allocation that would push the cumulative budget over
        # 100%: allowing it would let two strategies claim overlapping
        # capital and silently over-commit the book.
        projected = self.total_allocation_pct + pct
        if projected > _MAX_PCT + 1e-9:
            raise MultiStrategyManagerError(
                f"registering {sid!r} at {pct}% would raise total allocation "
                f"to {projected}% (cap {_MAX_PCT}%); reduce an existing "
                f"allocation before registering"
            )

        cap = self._total_capital * pct / _MAX_PCT
        registration = StrategyRegistration(
            strategy_id=sid,
            strategy=strategy,
            allocation_pct=pct,
            allocation_cap=cap,
        )
        self._registrations[sid] = registration
        logger.info(
            "multi_manager.registered",
            strategy_id=sid,
            allocation_pct=pct,
            allocation_cap=cap,
        )
        return registration

    def unregister(self, strategy_id: str) -> bool:
        """Remove ``strategy_id``. Returns True if it was present.

        Idempotent: removing an unknown id is a no-op returning False.
        """
        existed = self._registrations.pop(strategy_id, None) is not None
        if existed:
            logger.info("multi_manager.unregistered", strategy_id=strategy_id)
        return existed

    # -- evaluation ----------------------------------------------------

    async def evaluate_all(
        self,
        market_data: Any,
        cost_model: Any,
    ) -> MultiStrategyEvaluation:
        """Evaluate every registered strategy against the same
        ``market_data`` and ``cost_model``, re-tag and allocation-cap the
        emitted signals, and aggregate them.

        Each strategy receives an independent deep copy of the inputs, so
        one strategy mutating them cannot leak to its siblings or the
        caller's originals. A single failing / timed-out strategy is
        isolated: its error is recorded and the remaining strategies
        still contribute.

        Every emitted signal is:

        * re-tagged with its registered ``strategy_id`` (provenance), and
        * annotated in ``metadata`` with its capital budget; and
        * allocation-capped: if the strategy's active (BUY/SELL) signal
          weights sum to more than its allocation fraction
          (``allocation_pct / 100``), they are scaled down
          proportionally so the cap is respected.

        Returns
        -------
        MultiStrategyEvaluation
            ``signals`` holds all (capped, tagged) signals across every
            strategy; ``per_strategy_signals`` groups them by source id;
            ``allocation_adjustments`` records each strategy whose weights
            were scaled; ``errors`` maps any failed strategy id to its
            error message.
        """
        # Empty registry -> clean no-op. Short-circuit before any
        # strategy runs so callers get a consistent empty result.
        if not self._registrations:
            return MultiStrategyEvaluation(
                signals=[],
                per_strategy_signals={},
                total_capital=self._total_capital,
                allocation_caps={},
                allocation_adjustments={},
                errors={},
            )

        per_strategy_signals: dict[str, list[Signal]] = {}
        allocation_adjustments: dict[str, float] = {}
        errors: dict[str, str] = {}

        # Snapshot the registry before iterating. A strategy that
        # registers / unregisters a sibling mid-cycle must not mutate the
        # dict we are walking (which would otherwise raise "dictionary
        # changed size during iteration"). Strategies added during this
        # cycle are intentionally excluded — they run on the next one.
        for sid, registration in list(self._registrations.items()):
            strategy = registration.strategy
            # Independent deep copies keep cross-strategy comparisons
            # apples-to-apples even if a plugin mutates its inputs.
            md = copy.deepcopy(market_data)
            cm = copy.deepcopy(cost_model)
            try:
                raw = strategy.evaluate(md, cm)
            except Exception as exc:
                # The synchronous call frame is never bounded by the
                # timeout — it has already returned (or raised) before
                # the cap could apply. A strategy that raises the builtin
                # ``TimeoutError`` itself lands here and is reported as
                # ``strategy_failed``, never masquerading as a timeout.
                logger.exception("multi_manager.strategy_failed", strategy_id=sid)
                errors[sid] = f"{type(exc).__name__}: {exc}"
                continue
            # Support both sync (returns list) and async (returns
            # coroutine) strategies transparently. Only the awaitable
            # result is bounded by the per-strategy timeout, and the
            # guard is kept tight around ``wait_for`` alone so a builtin
            # ``TimeoutError`` raised anywhere else is not misclassified
            # as a deadline expiry.
            if inspect.isawaitable(raw):
                try:
                    raw = await asyncio.wait_for(raw, timeout=self._eval_timeout)
                except TimeoutError:
                    logger.warning(
                        "multi_manager.strategy_timeout",
                        strategy_id=sid,
                        timeout=self._eval_timeout,
                    )
                    errors[sid] = f"TimeoutError: evaluate exceeded {self._eval_timeout}s timeout"
                    continue
                except Exception as exc:
                    logger.exception("multi_manager.strategy_failed", strategy_id=sid)
                    errors[sid] = f"{type(exc).__name__}: {exc}"
                    continue
            signals = list(raw) if raw else []
            tagged = self._tag(sid, registration, signals)
            capped, scale = self._enforce_cap(registration, tagged)
            if scale is not None:
                allocation_adjustments[sid] = scale
            per_strategy_signals[sid] = capped

        aggregated: list[Signal] = [
            sig for signals in per_strategy_signals.values() for sig in signals
        ]

        return MultiStrategyEvaluation(
            signals=aggregated,
            per_strategy_signals=per_strategy_signals,
            total_capital=self._total_capital,
            allocation_caps=self.allocations(),
            allocation_adjustments=allocation_adjustments,
            errors=errors,
        )

    # -- signal post-processing ----------------------------------------

    @staticmethod
    def _tag(
        strategy_id: str,
        registration: StrategyRegistration,
        signals: list[Signal],
    ) -> list[Signal]:
        """Re-tag every signal with its registered ``strategy_id`` (the
        source of truth for provenance) and annotate its capital budget.

        ``model_copy`` is used so the strategy's original signal objects
        are never mutated. The metadata dict is shallow-copied so
        downstream mutation of a tagged signal cannot leak back into the
        source.
        """
        tagged: list[Signal] = []
        for sig in signals:
            metadata = dict(sig.metadata or {})
            metadata[_META_CAP_PCT] = registration.allocation_pct
            metadata[_META_CAP_DOLLARS] = registration.allocation_cap
            tagged.append(
                sig.model_copy(
                    update={
                        "strategy_id": strategy_id,
                        "metadata": metadata,
                    }
                )
            )
        return tagged

    def _enforce_cap(
        self,
        registration: StrategyRegistration,
        signals: list[Signal],
    ) -> tuple[list[Signal], float | None]:
        """Scale a strategy's active signal weights down so their sum
        respects the strategy's allocation fraction.

        The allocation fraction is ``allocation_pct / 100``. A strategy's
        active (BUY/SELL) target weights are interpreted as fractions of
        the *whole* book; if they sum to more than the strategy's
        allocation fraction they are scaled down proportionally so the
        sum equals the fraction exactly. HOLD signals abstain and are
        passed through unchanged.

        Returns ``(capped_signals, scale)`` where ``scale`` is ``None``
        when no adjustment was needed, otherwise the factor applied
        (``0 < scale < 1``).
        """
        if registration.allocation_pct <= 0:
            # A capital-starved strategy may not commit any weight. Zero
            # out its active signals rather than dropping them, so the
            # strategy still appears in the audit trail with its intent
            # preserved (side + symbol) but deploys no capital.
            return self._zero_active_weights(signals), 0.0

        fraction = registration.allocation_pct / _MAX_PCT
        active_weight = 0.0
        for sig in signals:
            if sig.side == Side.HOLD:
                continue
            weight = sig.weight if math.isfinite(sig.weight) else 0.0
            active_weight += max(weight, 0.0)

        if active_weight <= fraction + 1e-12:
            # Within budget — nothing to scale. (The tiny epsilon keeps
            # float dust like 0.2 + 0.0000000001 from triggering a
            # spurious rescale.)
            return signals, None

        # Proportional scale-down so the active weights sum to exactly
        # the strategy's allocation fraction. Each weight keeps its
        # relative shape; only the magnitude is reduced.
        scale = fraction / active_weight if active_weight > 0 else 0.0
        capped: list[Signal] = []
        for sig in signals:
            if sig.side == Side.HOLD or not math.isfinite(sig.weight) or sig.weight <= 0:
                capped.append(sig)
                continue
            original = sig.weight
            new_weight = max(min(sig.weight * scale, 1.0), 0.0)
            metadata = dict(sig.metadata or {})
            metadata[_META_CAPPED] = True
            metadata[_META_ORIG_WEIGHT] = original
            capped.append(sig.model_copy(update={"weight": new_weight, "metadata": metadata}))
        return capped, scale

    @staticmethod
    def _zero_active_weights(signals: list[Signal]) -> list[Signal]:
        """Zero out the weight of every active signal (for a
        capital-starved strategy) while preserving its intent."""
        out: list[Signal] = []
        for sig in signals:
            if sig.side == Side.HOLD or not math.isfinite(sig.weight) or sig.weight <= 0:
                out.append(sig)
                continue
            metadata = dict(sig.metadata or {})
            metadata[_META_CAPPED] = True
            metadata[_META_ORIG_WEIGHT] = sig.weight
            out.append(sig.model_copy(update={"weight": 0.0, "metadata": metadata}))
        return out


__all__ = [
    "MultiStrategyEvaluation",
    "MultiStrategyManager",
    "MultiStrategyManagerError",
    "StrategyRegistration",
]
