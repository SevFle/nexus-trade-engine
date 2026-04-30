"""Strategy lifecycle and promotion flow.

Models a strategy's path from a fresh idea to live trading as a small
state machine: ``draft → backtest → paper → live``, with ``retired``
reachable from any non-draft stage. Each transition is gated by an
:class:`LifecycleEvidence` payload — promotion to ``paper`` requires a
backtest id and minimum Sharpe; promotion to ``live`` requires a paper
window and minimum live-paper Sharpe.

Pairs with :mod:`engine.core.strategy_versioning`:
:class:`StrategyVersionService` controls *what* code runs;
:class:`StrategyLifecycleService` controls *which stage* it's allowed
to run in.

The gate thresholds are conservative defaults — operators that want
stricter or looser thresholds can subclass or replace the gate
functions in a follow-up. The key invariant this module enforces is
**no skipping** — a strategy cannot jump straight from draft to live.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


class LifecycleStage(StrEnum):
    DRAFT = "draft"
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"
    RETIRED = "retired"


@dataclass(frozen=True)
class LifecycleEvidence:
    """Payload that accompanies a promotion request."""

    backtest_id: str | None = None
    sharpe: float | None = None
    max_drawdown_pct: float | None = None
    paper_days: int | None = None
    paper_sharpe: float | None = None


@dataclass(frozen=True)
class LifecycleTransition:
    """One recorded promotion event."""

    strategy_id: str
    target: LifecycleStage
    evidence: LifecycleEvidence
    at_epoch: float


class InvalidTransitionError(Exception):
    """Raised when a promotion is rejected by the state machine or a gate."""


_MIN_SHARPE_FOR_PAPER = 0.5
_MAX_DRAWDOWN_PCT_FOR_PAPER = 25.0
_MIN_PAPER_DAYS = 7
_MIN_PAPER_SHARPE = 0.5


_VALID_TRANSITIONS: dict[LifecycleStage, set[LifecycleStage]] = {
    LifecycleStage.DRAFT: {LifecycleStage.BACKTEST, LifecycleStage.RETIRED},
    LifecycleStage.BACKTEST: {LifecycleStage.PAPER, LifecycleStage.RETIRED},
    LifecycleStage.PAPER: {LifecycleStage.LIVE, LifecycleStage.RETIRED},
    LifecycleStage.LIVE: {LifecycleStage.RETIRED},
    LifecycleStage.RETIRED: set(),
}


def _gate_backtest_to_paper(evidence: LifecycleEvidence) -> None:
    if not evidence.backtest_id:
        msg = "BACKTEST -> PAPER requires evidence.backtest_id"
        raise InvalidTransitionError(msg)
    if evidence.sharpe is None or evidence.sharpe < _MIN_SHARPE_FOR_PAPER:
        msg = (
            f"BACKTEST -> PAPER requires sharpe >= {_MIN_SHARPE_FOR_PAPER} "
            f"(got {evidence.sharpe})"
        )
        raise InvalidTransitionError(msg)
    if (
        evidence.max_drawdown_pct is None
        or evidence.max_drawdown_pct > _MAX_DRAWDOWN_PCT_FOR_PAPER
    ):
        msg = (
            f"BACKTEST -> PAPER requires max_drawdown_pct <= "
            f"{_MAX_DRAWDOWN_PCT_FOR_PAPER} (got {evidence.max_drawdown_pct})"
        )
        raise InvalidTransitionError(msg)


def _gate_paper_to_live(evidence: LifecycleEvidence) -> None:
    if evidence.paper_days is None or evidence.paper_days < _MIN_PAPER_DAYS:
        msg = (
            f"PAPER -> LIVE requires paper_days >= {_MIN_PAPER_DAYS} "
            f"(got {evidence.paper_days})"
        )
        raise InvalidTransitionError(msg)
    if (
        evidence.paper_sharpe is None
        or evidence.paper_sharpe < _MIN_PAPER_SHARPE
    ):
        msg = (
            f"PAPER -> LIVE requires paper_sharpe >= {_MIN_PAPER_SHARPE} "
            f"(got {evidence.paper_sharpe})"
        )
        raise InvalidTransitionError(msg)


_GATES: dict[
    tuple[LifecycleStage, LifecycleStage],
    Callable[[LifecycleEvidence], None],
] = {
    (LifecycleStage.BACKTEST, LifecycleStage.PAPER): _gate_backtest_to_paper,
    (LifecycleStage.PAPER, LifecycleStage.LIVE): _gate_paper_to_live,
}


class StrategyLifecycleService:
    """In-memory promotion state machine with per-strategy locking."""

    def __init__(self) -> None:
        self._stage: dict[str, LifecycleStage] = {}
        self._history: dict[str, list[LifecycleTransition]] = defaultdict(list)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def _lock(self, strategy_id: str) -> asyncio.Lock:
        return self._locks[strategy_id]

    async def set_stage(
        self, strategy_id: str, stage: LifecycleStage
    ) -> LifecycleTransition:
        """Bootstrap or override a strategy's stage without running gates.

        Use only at registration time / for tests; production callers
        should always go through :meth:`promote`.
        """
        async with self._lock(strategy_id):
            self._stage[strategy_id] = stage
            t = LifecycleTransition(
                strategy_id=strategy_id,
                target=stage,
                evidence=LifecycleEvidence(),
                at_epoch=time.time(),
            )
            self._history[strategy_id].append(t)
            return t

    async def promote(
        self,
        strategy_id: str,
        *,
        target: LifecycleStage,
        evidence: LifecycleEvidence,
    ) -> LifecycleTransition:
        """Move a strategy to ``target``, running the configured gate."""
        async with self._lock(strategy_id):
            current = self._stage.get(strategy_id)
            if current is None:
                msg = f"strategy {strategy_id} has no current stage"
                raise InvalidTransitionError(msg)
            allowed = _VALID_TRANSITIONS.get(current, set())
            if target not in allowed:
                msg = (
                    f"transition {current.value} -> {target.value} is not "
                    f"allowed (valid: {sorted(s.value for s in allowed)})"
                )
                raise InvalidTransitionError(msg)
            gate = _GATES.get((current, target))
            if gate is not None:
                gate(evidence)
            self._stage[strategy_id] = target
            t = LifecycleTransition(
                strategy_id=strategy_id,
                target=target,
                evidence=evidence,
                at_epoch=time.time(),
            )
            self._history[strategy_id].append(t)
            return t

    async def current_stage(
        self, strategy_id: str
    ) -> LifecycleStage | None:
        return self._stage.get(strategy_id)

    async def history(
        self, strategy_id: str
    ) -> list[LifecycleTransition]:
        return list(self._history.get(strategy_id, ()))


__all__ = [
    "InvalidTransitionError",
    "LifecycleEvidence",
    "LifecycleStage",
    "LifecycleTransition",
    "StrategyLifecycleService",
]
