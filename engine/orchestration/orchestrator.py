"""Multi-strategy orchestration — register, run, and resolve conflicts.

Runs a registered set of strategy plugins against identical market data and
collapses their emitted :class:`~engine.core.signal.Signal` objects into a
single, conflict-free decision set via a two-step pipeline:
``await orch.run_all(market)`` → ``orch.aggregate_signals()``.

Conflict resolution (opposing signals on the same symbol):

``ConflictResolution.PRIORITY`` (default)
    The highest-priority strategy that expressed a non-HOLD opinion wins.
    Strategies tied for top priority on opposing sides resolve to HOLD
    (stalemate → no action). HOLD signals abstain.

``ConflictResolution.NET_POSITION``
    BUY = +weight, SELL = -weight are summed. Positive net → BUY, negative
    → SELL, zero → HOLD. The resolved weight is the net magnitude clamped
    to ``[0, 1]``, so conviction (weight) can override a bare headcount.

This is the lighter-weight counterpart to the async
:mod:`engine.core.strategy_orchestrator` (majority/weighted voter with
per-strategy timeout isolation); NET_POSITION aggregation is unique here.
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

    from engine.core.cost_model import ICostModel

logger = structlog.get_logger()


class StrategyOrchestratorError(ValueError):
    """Bad orchestrator configuration (bad strategy, duplicate registration,
    unknown conflict mode, priority for unknown strategy, non-finite value)."""


class ConflictResolution(StrEnum):
    """How opposing signals on the same symbol are reconciled."""

    PRIORITY = "priority"
    NET_POSITION = "net_position"


# Stamped onto aggregated output so audit code can tell orchestrator
# decisions apart from raw per-strategy signals.
_AGGREGATED_STRATEGY_ID = "orchestrator"
# Net-position dead band: |net| below this is a tie → HOLD, so float dust
# (e.g. 1.0 - 0.5 - 0.5 == 1.1e-16) can't flip a HOLD into a phantom signal.
_NET_EPSILON = 1e-9
_DEFAULT_PRIORITY = 0.0


def _strategy_id(strategy: Any) -> str:
    """Resolve a strategy's id (string attribute or no-arg callable) and
    verify it exposes a callable ``evaluate``."""
    sid = getattr(strategy, "id", None)
    if callable(sid):
        sid = sid()
    if not isinstance(sid, str) or not sid:
        raise StrategyOrchestratorError(
            f"strategy must expose a non-empty string `id`, got {sid!r}"
        )
    if not callable(getattr(strategy, "evaluate", None)):
        raise StrategyOrchestratorError(
            f"strategy {sid!r} must expose a callable `evaluate` method"
        )
    return sid


def _finite(value: float, label: str) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError) as exc:
        raise StrategyOrchestratorError(f"{label} must be a number, got {value!r}") from exc
    if not math.isfinite(num):
        raise StrategyOrchestratorError(f"{label} must be finite, got {value!r}")
    return num


class StrategyOrchestrator:
    """Register strategies, run them on shared data, resolve conflicts."""

    def __init__(
        self,
        strategies: Iterable[Any],
        cost_model: ICostModel,
        *,
        conflict_resolution: str | ConflictResolution = ConflictResolution.PRIORITY,
        priorities: dict[str, float] | None = None,
    ) -> None:
        try:
            self._mode = ConflictResolution(conflict_resolution)
        except ValueError as exc:
            valid = ", ".join(sorted(m.value for m in ConflictResolution))
            raise StrategyOrchestratorError(
                f"unknown conflict resolution {conflict_resolution!r}; expected one of {valid}"
            ) from exc
        self._cost_model = cost_model
        self._strategies: dict[str, Any] = {}
        self._priorities: dict[str, float] = {}
        for strategy in strategies:  # insertion-ordered → deterministic run_all
            self.register(strategy)
        # Apply explicit overrides last so they win over register() defaults
        # and so we can reject unknown ids.
        for sid, prio in (priorities or {}).items():
            if sid not in self._strategies:
                raise StrategyOrchestratorError(f"priority given for unknown strategy {sid!r}")
            self._priorities[sid] = _finite(prio, f"priorities[{sid!r}]")
        self._last_signals: list[Signal] = []  # most recent run_all() output

    def __len__(self) -> int:
        return len(self._strategies)

    def __contains__(self, strategy_id: object) -> bool:
        return strategy_id in self._strategies

    @property
    def strategy_ids(self) -> list[str]:
        """Registered strategy ids in registration order."""
        return list(self._strategies)

    def get_priority(self, strategy_id: str) -> float:
        """Priority for ``strategy_id`` (0.0 if unset / unregistered)."""
        return self._priorities.get(strategy_id, _DEFAULT_PRIORITY)

    def register(self, strategy: Any, priority: float = _DEFAULT_PRIORITY) -> None:
        """Register ``strategy`` with the given ``priority`` (default 0.0)."""
        sid = _strategy_id(strategy)
        if sid in self._strategies:
            raise StrategyOrchestratorError(f"strategy {sid!r} already registered")
        self._strategies[sid] = strategy
        self._priorities[sid] = _finite(priority, f"priority[{sid!r}]")

    def unregister(self, strategy_id: str) -> bool:
        """Remove ``strategy_id``. Returns True if it was present."""
        self._priorities.pop(strategy_id, None)
        return self._strategies.pop(strategy_id, None) is not None

    async def run_all(
        self,
        market_data: Any,
        *,
        timeout_seconds: float | None = None,
    ) -> list[Signal]:
        """Invoke every registered strategy's ``evaluate`` with the shared
        ``market_data`` and the orchestrator's cost model.

        Returns flattened raw signals (conflicts still present). Sync and
        async ``evaluate`` are both supported. A raising or timed-out
        strategy is isolated — logged and skipped — so one bad plugin can't
        abort the whole cycle; its signals are simply absent.

        Parameters
        ----------
        timeout_seconds:
            Per-strategy wall-clock cap applied to the *awaitable* result of
            ``evaluate``. ``None`` (default) applies no timeout. A strategy
            whose async result exceeds the cap is cancelled and skipped
            (logged as ``orchestrator.strategy_timeout``). The synchronous
            call frame itself is never bounded — it has already returned by
            the time the cap could apply.
        """
        collected: list[Signal] = []
        for sid, strategy in list(self._strategies.items()):
            try:
                raw = strategy.evaluate(market_data, self._cost_model)
                if inspect.isawaitable(raw):
                    if timeout_seconds is not None:
                        # Narrow the timeout guard to ONLY the
                        # ``asyncio.wait_for`` cap. ``wait_for`` raises
                        # ``asyncio.TimeoutError`` (an alias of the builtin
                        # ``TimeoutError`` on 3.11+) when its wall-clock
                        # deadline elapses; that is the one case we treat as
                        # a genuine timeout. By keeping the handler tight
                        # around ``wait_for`` alone, a strategy that raises
                        # the builtin ``TimeoutError`` itself — from the
                        # synchronous call frame above or from a bare
                        # ``await raw`` below — falls through to the generic
                        # handler and is reported as ``strategy_failed``
                        # rather than masquerading as a timeout.
                        try:
                            raw = await asyncio.wait_for(raw, timeout=timeout_seconds)
                        except TimeoutError:
                            logger.warning(
                                "orchestrator.strategy_timeout",
                                strategy_id=sid,
                                timeout=timeout_seconds,
                            )
                            continue
                    else:
                        raw = await raw
            except Exception:
                logger.exception("orchestrator.strategy_failed", strategy_id=sid)
                continue
            if raw:
                collected.extend(raw)
        self._last_signals = collected
        return collected

    def aggregate_signals(self, signals: Iterable[Signal] | None = None) -> list[Signal]:
        """Collapse ``signals`` to one decision per symbol. Defaults to the
        most recent :meth:`run_all` output."""
        source = list(signals) if signals is not None else list(self._last_signals)
        per_symbol: dict[str, list[Signal]] = defaultdict(list)
        for sig in source:
            per_symbol[sig.symbol].append(sig)
        return [self._resolve(group) for group in per_symbol.values()]

    def _resolve(self, group: list[Signal]) -> Signal:
        if self._mode is ConflictResolution.NET_POSITION:
            return self._net_position(group)
        return self._priority(group)

    def _priority(self, group: list[Signal]) -> Signal:
        active = [s for s in group if s.side != Side.HOLD]
        if not active:
            return self._resolved(group[0], Side.HOLD, active)
        top = max(self._priorities.get(s.strategy_id, _DEFAULT_PRIORITY) for s in active)
        winners = [
            s for s in active if self._priorities.get(s.strategy_id, _DEFAULT_PRIORITY) == top
        ]
        if len({s.side for s in winners}) > 1:  # top-priority stalemate → HOLD
            return self._resolved(winners[0], Side.HOLD, active)
        winner = winners[0]
        return self._resolved(winner, winner.side, active)

    def _net_position(self, group: list[Signal]) -> Signal:
        net = 0.0
        active: list[Signal] = []
        for sig in group:
            if sig.side == Side.BUY:
                # A non-finite weight (nan/inf) can't vote a magnitude:
                # treat it as an abstention so it can't poison the sum.
                if not math.isfinite(sig.weight):
                    continue
                net += sig.weight
                active.append(sig)
            elif sig.side == Side.SELL:
                if not math.isfinite(sig.weight):
                    continue  # non-finite weight → abstention
                net -= sig.weight
                active.append(sig)
            elif sig.side == Side.HOLD:
                # HOLD abstains and never contributes to the net position.
                continue
            else:
                raise StrategyOrchestratorError(
                    f"unsupported side {sig.side!r} in net_position aggregation"
                )
        if net > _NET_EPSILON:
            return self._resolved(group[0], Side.BUY, active, weight=min(net, 1.0))
        if net < -_NET_EPSILON:
            return self._resolved(group[0], Side.SELL, active, weight=min(abs(net), 1.0))
        return self._resolved(group[0], Side.HOLD, active, weight=0.0)

    @staticmethod
    def _resolved(
        template: Signal,
        side: Side,
        active: Iterable[Signal] = (),
        *,
        weight: float | None = None,
    ) -> Signal:
        """Build a resolved Signal: ``side``/``weight`` reflect the decision,
        other fields mirror ``template``. ``strategy_id`` is overwritten so
        the audit trail shows the orchestrator; ``metadata`` is deep-copied
        so downstream mutation can't leak back to the source (nested dicts/
        lists included); and ``orchestrator_sources`` records the contributing
        strategy ids (the non-HOLD, finite-weight voters) so the decision
        stays auditable."""
        metadata = dict(copy.deepcopy(template.metadata) or {})
        metadata["orchestrator_sources"] = [s.strategy_id for s in active]
        return template.model_copy(
            update={
                "side": side,
                "weight": template.weight if weight is None else weight,
                "strategy_id": _AGGREGATED_STRATEGY_ID,
                "metadata": metadata,
            }
        )


__all__ = ["ConflictResolution", "StrategyOrchestrator", "StrategyOrchestratorError"]
