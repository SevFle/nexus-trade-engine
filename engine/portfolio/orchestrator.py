"""Portfolio strategy orchestrator (lightweight weighted-vote variant).

Aggregates signals from several :class:`IStrategy` plugins — each evaluated
against one *shared* market context — into a single unified
:class:`SignalSet`.

Conflict resolution: when strategies disagree on a symbol (e.g. BUY vs
SELL on ``AAPL``) the conflict is resolved by **net weighted vote** — each
BUY casts ``+weight``, each SELL casts ``-weight``, HOLD abstains; the side
with the strictly greater net weight wins, and an exact tie resolves to
HOLD. A high-conviction strategy can therefore outvote a numerical
majority ("strongest net weight wins").

This is the small, obvious counterpart to the async
:mod:`engine.core.strategy_orchestrator` (majority/weighted voter with
timeouts) and :mod:`engine.orchestration.orchestrator` (priority/net
two-step pipeline): register once, call :meth:`evaluate` per cycle.
"""

from __future__ import annotations

import inspect
import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import structlog

from engine.core.signal import Side, Signal

if TYPE_CHECKING:
    from collections.abc import Awaitable

logger = structlog.get_logger()

#: ``strategy_id`` stamped on every resolved signal so audit code can tell
#: orchestrator decisions apart from raw per-strategy signals.
ORCHESTRATED_STRATEGY_ID = "orchestrated"


class StrategyOrchestratorError(ValueError):
    """Bad configuration: invalid weight, malformed strategy, duplicate id."""


@runtime_checkable
class IStrategy(Protocol):
    """Structural contract for any strategy the orchestrator can run.

    ``evaluate`` may be sync (return a list) or async (return a coroutine);
    awaitable results are awaited transparently.
    """

    id: str

    def evaluate(
        self, market_context: Any
    ) -> list[Signal] | Awaitable[list[Signal]]: ...


@dataclass(frozen=True)
class SignalSet:
    """Unified output of one evaluation cycle.

    ``signals``  - per-symbol resolved decision (one per considered symbol);
    ``breakdown`` - per-symbol provenance (net weight + voters) for auditing;
    ``errors``   - maps any strategy that raised during ``evaluate`` to its
                  message so a failing plugin never silently vanishes.
    """

    signals: list[Signal] = field(default_factory=list)
    strategy_count: int = 0
    breakdown: dict[str, dict[str, Any]] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def trade_signals(self) -> list[Signal]:
        """Resolved signals expressing a non-HOLD intent."""
        return [s for s in self.signals if s.side != Side.HOLD]

    @property
    def is_empty(self) -> bool:
        """True when no symbol was considered (empty registry / no output)."""
        return not self.signals


def _strategy_id(strategy: Any) -> str:
    sid = getattr(strategy, "id", None)
    if callable(sid):  # tolerate callable/property forms
        sid = sid()
    if not isinstance(sid, str) or not sid:
        raise StrategyOrchestratorError(
            f"strategy must expose a non-empty string `id`, got {sid!r}"
        )
    return sid


def _validate_weight(weight: float, sid: str) -> float:
    try:
        value = float(weight)
    except (TypeError, ValueError) as exc:
        raise StrategyOrchestratorError(
            f"weight for strategy {sid!r} must be a number, got {weight!r}"
        ) from exc
    if not math.isfinite(value) or value < 0:
        raise StrategyOrchestratorError(
            f"weight for strategy {sid!r} must be finite & >=0, got {value!r}"
        )
    return value


class StrategyOrchestrator:
    """Aggregates weighted signals from many strategies into one SignalSet.

    Construct with a list of ``(strategy, weight)`` tuples; call
    :meth:`evaluate` each cycle with the shared market context.
    """

    def __init__(self, strategies: list[tuple[IStrategy, float]]) -> None:
        if not isinstance(strategies, list):
            raise StrategyOrchestratorError("`strategies` must be a list")
        self._strategies: dict[str, IStrategy] = {}
        self._weights: dict[str, float] = {}
        for entry in strategies:
            try:
                strategy, weight = entry
            except (TypeError, ValueError) as exc:
                raise StrategyOrchestratorError(
                    f"each entry must be a (strategy, weight) tuple, got {entry!r}"
                ) from exc
            sid = _strategy_id(strategy)
            if not callable(getattr(strategy, "evaluate", None)):
                raise StrategyOrchestratorError(
                    f"strategy {sid!r} must expose a callable `evaluate`"
                )
            value = _validate_weight(weight, sid)
            if sid in self._strategies:
                raise StrategyOrchestratorError(f"duplicate strategy id {sid!r}")
            self._strategies[sid] = strategy
            self._weights[sid] = value

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
        """Snapshot of the per-strategy capital-allocation weights."""
        return dict(self._weights)

    async def evaluate(self, market_context: Any) -> SignalSet:
        """Run every strategy on ``market_context`` and merge the results.

        Each strategy receives the **same** context object so cross-strategy
        comparisons are apples-to-apples. A strategy that raises is isolated:
        its error is recorded in ``SignalSet.errors`` and the rest still vote.
        """
        per_strategy: dict[str, list[Signal]] = {}
        errors: dict[str, str] = {}
        for sid, strategy in self._strategies.items():
            try:
                raw = strategy.evaluate(market_context)
                if inspect.isawaitable(raw):  # transparent async support
                    raw = await raw
            except Exception as exc:
                logger.exception(
                    "portfolio_orchestrator.strategy_failed", strategy_id=sid
                )
                errors[sid] = f"{type(exc).__name__}: {exc}"
                continue
            per_strategy[sid] = list(raw) if raw else []

        signals, breakdown = self._aggregate(per_strategy)
        return SignalSet(
            signals=signals,
            strategy_count=len(self._strategies),
            breakdown=breakdown,
            errors=errors,
        )

    def _aggregate(
        self, per_strategy: dict[str, list[Signal]]
    ) -> tuple[list[Signal], dict[str, dict[str, Any]]]:
        """Collapse per-strategy signals into per-symbol net-weight decisions."""
        net: dict[str, float] = defaultdict(float)
        symbols: set[str] = set()
        voters: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
        for sid, signals in per_strategy.items():
            weight = self._weights[sid]
            for sig in signals:
                symbols.add(sig.symbol)
                if sig.side == Side.BUY:
                    net[sig.symbol] += weight
                    voters[sig.symbol].append((sid, "buy", weight))
                elif sig.side == Side.SELL:
                    net[sig.symbol] -= weight
                    voters[sig.symbol].append((sid, "sell", weight))
                # HOLD: abstains — symbol still tracked.

        resolved: list[Signal] = []
        breakdown: dict[str, dict[str, Any]] = {}
        for symbol in sorted(symbols):  # deterministic order
            n = net[symbol]
            side = Side.BUY if n > 0 else Side.SELL if n < 0 else Side.HOLD
            resolved.append(
                Signal(
                    symbol=symbol,
                    side=side,
                    strategy_id=ORCHESTRATED_STRATEGY_ID,
                    weight=min(abs(n), 1.0),  # Signal.weight ∈ [0, 1]
                    reason=f"net weighted vote: {n:+.4f}",
                )
            )
            breakdown[symbol] = {
                "net": n,
                "side": side.value,
                "voters": [list(v) for v in voters[symbol]],
            }
        return resolved, breakdown


__all__ = [
    "ORCHESTRATED_STRATEGY_ID",
    "IStrategy",
    "SignalSet",
    "StrategyOrchestrator",
    "StrategyOrchestratorError",
]
