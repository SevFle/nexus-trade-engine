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
# Per-strategy eval budget. Matches the sibling strategy_orchestrator's
# default (30s) so a strategy that runs fine in either harness behaves
# the same, and so the documented default is identical across modules.
_DEFAULT_EVAL_TIMEOUT = 30.0
# Valid Side values, used by _resolve's defensive check (model_copy /
# model_construct skip validation, so a stray side must be caught here).
# Built from the enum so it stays in sync if Side ever gains a member.
_SIDE_VALUES = frozenset(member.value for member in Side)


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
        timeout: float = _DEFAULT_EVAL_TIMEOUT,
    ) -> None:
        try:
            self._mode = ConflictResolution(conflict_resolution)
        except ValueError as exc:
            valid = ", ".join(sorted(m.value for m in ConflictResolution))
            raise StrategyOrchestratorError(
                f"unknown conflict resolution {conflict_resolution!r}; expected one of {valid}"
            ) from exc
        eval_timeout = _finite(timeout, "timeout")
        if eval_timeout <= 0:
            raise StrategyOrchestratorError(
                f"timeout must be a positive number, got {timeout!r}"
            )
        self._eval_timeout = eval_timeout
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

    async def run_all(self, market_data: Any) -> list[Signal]:
        """Invoke every registered strategy's ``evaluate`` with the shared
        ``market_data`` and the orchestrator's cost model.

        Returns flattened raw signals (conflicts still present). Sync and
        async ``evaluate`` are both supported. A raising strategy is
        isolated — logged and skipped — so one bad plugin can't abort the
        whole cycle; its signals are simply absent. An async strategy that
        exceeds ``timeout`` is likewise isolated.
        """
        collected: list[Signal] = []
        for sid, strategy in list(self._strategies.items()):
            try:
                raw = strategy.evaluate(market_data, self._cost_model)
                if inspect.isawaitable(raw):
                    # Bound the async path so one hanging strategy can't
                    # stall the whole cycle. Scoped narrowly around the
                    # await: a sync call has already completed by here, and
                    # ``asyncio.wait_for`` raises ``asyncio.TimeoutError``
                    # (NOT the builtin OSError-alias TimeoutError), so we
                    # catch it explicitly and report it distinctly.
                    try:
                        raw = await asyncio.wait_for(raw, timeout=self._eval_timeout)
                    except TimeoutError:
                        logger.warning(
                            "orchestrator.strategy_timeout",
                            strategy_id=sid,
                            timeout=self._eval_timeout,
                        )
                        continue
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
        for sig in group:
            # Defensive: pydantic constrains Side at construction, but
            # ``model_copy`` / ``model_construct`` skip validation. An
            # unexpected side would otherwise be silently mis-counted
            # (NET_POSITION treats it as an abstain; PRIORITY could even
            # elect it), so fail loudly with the orchestrator's own error.
            side_value = getattr(sig.side, "value", sig.side)
            if side_value not in _SIDE_VALUES:
                raise StrategyOrchestratorError(
                    f"unknown side {sig.side!r} for symbol {sig.symbol!r}"
                )
        if self._mode is ConflictResolution.NET_POSITION:
            return self._net_position(group)
        return self._priority(group)

    def _priority(self, group: list[Signal]) -> Signal:
        active = [s for s in group if s.side != Side.HOLD]
        if not active:
            return self._resolved(group[0], Side.HOLD)
        top = max(self._priorities.get(s.strategy_id, _DEFAULT_PRIORITY) for s in active)
        winners = [
            s for s in active if self._priorities.get(s.strategy_id, _DEFAULT_PRIORITY) == top
        ]
        if len({s.side for s in winners}) > 1:  # top-priority stalemate → HOLD
            return self._resolved(winners[0], Side.HOLD)
        winner = winners[0]
        return self._resolved(winner, winner.side)

    def _net_position(self, group: list[Signal]) -> Signal:
        net = 0.0
        for sig in group:
            if sig.side == Side.BUY:
                net += sig.weight
            elif sig.side == Side.SELL:
                net -= sig.weight
        if net > _NET_EPSILON:
            return self._resolved(group[0], Side.BUY, weight=min(net, 1.0))
        if net < -_NET_EPSILON:
            return self._resolved(group[0], Side.SELL, weight=min(abs(net), 1.0))
        return self._resolved(group[0], Side.HOLD, weight=0.0)

    @staticmethod
    def _resolved(template: Signal, side: Side, *, weight: float | None = None) -> Signal:
        """Build a resolved Signal: ``side``/``weight`` reflect the decision,
        other fields mirror ``template``. ``strategy_id`` is overwritten so
        the audit trail shows the orchestrator, and ``metadata`` is copied
        so downstream mutation can't leak back to the source."""
        return template.model_copy(
            update={
                "side": side,
                "weight": template.weight if weight is None else weight,
                "strategy_id": _AGGREGATED_STRATEGY_ID,
                # Deep-copy so nested mutable values can't leak back to
                # the source signal on downstream mutation, and coalesce
                # ``None`` (a caller can null metadata via model_copy) to
                # ``{}`` so dict() never receives None.
                "metadata": dict(copy.deepcopy(template.metadata) or {}),
            }
        )


__all__ = ["ConflictResolution", "StrategyOrchestrator", "StrategyOrchestratorError"]
