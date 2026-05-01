"""Pre-trade risk gate (gh#110).

Decoupled from order/portfolio concrete types — operates on plain immutable
dataclasses so the gate can be reused by paper trading, live trading, and
backtests without dragging in the broker stack.

The gate checks an :class:`OrderIntent` against an :class:`AccountState` under
configured :class:`RiskLimits` and returns a :class:`RiskDecision`. All breaches
in a single check are reported together (not short-circuited) so callers can
log the full picture.

Stateful policies tracked on the gate instance:
- velocity:  rolling timestamps of approved orders within ``velocity_window_seconds``
- circuit_breaker: tripped when ``daily_pnl <= -max_daily_loss``; sticks until
  :meth:`RiskGate.reset_circuit_breaker` is called.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field


class RiskLimitsError(ValueError):
    """Invalid risk inputs (negative notional, unknown side, etc.)."""


_VALID_SIDES = frozenset({"buy", "sell"})


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: str
    notional: float
    sector: str
    asset_class: str

    def __post_init__(self) -> None:
        if self.notional < 0:
            raise RiskLimitsError(
                f"OrderIntent.notional must be >= 0, got {self.notional}"
            )
        if self.side not in _VALID_SIDES:
            raise RiskLimitsError(
                f"OrderIntent.side must be one of {sorted(_VALID_SIDES)}, "
                f"got {self.side!r}"
            )


@dataclass(frozen=True)
class AccountState:
    cash: float
    total_value: float
    daily_pnl: float
    exposures: dict[str, float]
    sector_exposures: dict[str, float]
    asset_class_exposures: dict[str, float]

    def __post_init__(self) -> None:
        if self.total_value < 0:
            raise RiskLimitsError(
                f"AccountState.total_value must be >= 0, got {self.total_value}"
            )


@dataclass(frozen=True)
class RiskLimits:
    max_single_order_notional: float | None = None
    max_position_notional: dict[str, float] = field(default_factory=dict)
    max_sector_concentration_pct: dict[str, float] = field(default_factory=dict)
    max_asset_class_concentration_pct: dict[str, float] = field(default_factory=dict)
    max_orders_per_window: int | None = None
    velocity_window_seconds: float = 60.0
    max_daily_loss: float | None = None


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    breached_limits: list[str]
    warnings: list[str]


class RiskGate:
    """Pre-trade risk gate. Aggregates all breaches per check."""

    def __init__(
        self,
        limits: RiskLimits,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.limits = limits
        self._clock: Callable[[], float] = clock or time.monotonic
        self._order_timestamps: list[float] = []
        self._circuit_breaker_tripped: bool = False

    def reset_circuit_breaker(self) -> None:
        self._circuit_breaker_tripped = False

    def check(self, intent: OrderIntent, state: AccountState) -> RiskDecision:
        breaches: list[str] = []

        if self._circuit_breaker_tripped:
            breaches.append("circuit_breaker")
        elif (
            self.limits.max_daily_loss is not None
            and state.daily_pnl <= -self.limits.max_daily_loss
        ):
            self._circuit_breaker_tripped = True
            breaches.append("daily_loss")

        if (
            self.limits.max_single_order_notional is not None
            and intent.notional > self.limits.max_single_order_notional
        ):
            breaches.append("single_order_notional")

        if intent.side == "buy":
            sym_cap = self.limits.max_position_notional.get(intent.symbol)
            if sym_cap is not None:
                projected = state.exposures.get(intent.symbol, 0.0) + intent.notional
                if projected > sym_cap:
                    breaches.append(f"position_notional[{intent.symbol}]")

        if state.total_value > 0:
            sector_cap = self.limits.max_sector_concentration_pct.get(intent.sector)
            if sector_cap is not None:
                projected = (
                    state.sector_exposures.get(intent.sector, 0.0) + intent.notional
                )
                if projected / state.total_value > sector_cap:
                    breaches.append(f"sector_concentration[{intent.sector}]")

            ac_cap = self.limits.max_asset_class_concentration_pct.get(
                intent.asset_class
            )
            if ac_cap is not None:
                projected = (
                    state.asset_class_exposures.get(intent.asset_class, 0.0)
                    + intent.notional
                )
                if projected / state.total_value > ac_cap:
                    breaches.append(
                        f"asset_class_concentration[{intent.asset_class}]"
                    )

        if self.limits.max_orders_per_window is not None:
            now = self._clock()
            window_start = now - self.limits.velocity_window_seconds
            self._order_timestamps = [
                t for t in self._order_timestamps if t >= window_start
            ]
            if len(self._order_timestamps) >= self.limits.max_orders_per_window:
                breaches.append("velocity")

        approved = not breaches

        if approved and self.limits.max_orders_per_window is not None:
            self._order_timestamps.append(self._clock())

        return RiskDecision(
            approved=approved,
            breached_limits=breaches,
            warnings=[],
        )


__all__ = [
    "AccountState",
    "OrderIntent",
    "RiskDecision",
    "RiskGate",
    "RiskLimits",
    "RiskLimitsError",
]
