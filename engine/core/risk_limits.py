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

Comparison semantics (intentional, asymmetric):
- circuit breaker:        ``daily_pnl <= -max_daily_loss``  — tripping AT the
  threshold is the conservative choice for a kill-switch.
- order / concentration:  ``projected > cap`` — an order whose projected value
  is exactly equal to the cap is permitted; this matches the natural English
  "exceeds cap" reading and stays consistent across all four order-side caps.

Numeric inputs (notional, total_value, daily_pnl, exposure dict values) must
be finite — NaN / inf inputs raise :class:`RiskLimitsError` at construction so a
misconfigured upstream cannot silently bypass the gate.
"""

from __future__ import annotations

import math
import threading
import time
import types
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field


class RiskLimitsError(ValueError):
    """Invalid risk inputs (negative notional, unknown side, NaN, etc.)."""


_VALID_SIDES = frozenset({"buy", "sell"})


def _require_finite(value: float, label: str) -> None:
    if not math.isfinite(value):
        raise RiskLimitsError(f"{label} must be finite, got {value!r}")


def _freeze_mapping(name: str, src: Mapping[str, float]) -> Mapping[str, float]:
    """Defensively copy + wrap a caller-supplied dict so subsequent mutation
    of the original cannot poison gate state. Validates each value is finite."""
    snap: dict[str, float] = {}
    for k, v in src.items():
        f = float(v)
        _require_finite(f, f"{name}[{k}]")
        snap[k] = f
    return types.MappingProxyType(snap)


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: str
    notional: float
    sector: str
    asset_class: str

    def __post_init__(self) -> None:
        _require_finite(self.notional, "OrderIntent.notional")
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
    exposures: Mapping[str, float]
    sector_exposures: Mapping[str, float]
    asset_class_exposures: Mapping[str, float]

    def __post_init__(self) -> None:
        _require_finite(self.cash, "AccountState.cash")
        _require_finite(self.total_value, "AccountState.total_value")
        _require_finite(self.daily_pnl, "AccountState.daily_pnl")
        if self.total_value < 0:
            raise RiskLimitsError(
                f"AccountState.total_value must be >= 0, got {self.total_value}"
            )
        # Deep-copy + freeze the exposure dicts so post-construction mutation
        # of the caller's source dict cannot change what the gate sees.
        object.__setattr__(
            self, "exposures", _freeze_mapping("exposures", self.exposures)
        )
        object.__setattr__(
            self,
            "sector_exposures",
            _freeze_mapping("sector_exposures", self.sector_exposures),
        )
        object.__setattr__(
            self,
            "asset_class_exposures",
            _freeze_mapping("asset_class_exposures", self.asset_class_exposures),
        )


@dataclass(frozen=True)
class RiskLimits:
    max_single_order_notional: float | None = None
    max_position_notional: Mapping[str, float] = field(default_factory=dict)
    max_sector_concentration_pct: Mapping[str, float] = field(default_factory=dict)
    max_asset_class_concentration_pct: Mapping[str, float] = field(
        default_factory=dict
    )
    max_orders_per_window: int | None = None
    velocity_window_seconds: float = 60.0
    max_daily_loss: float | None = None

    def __post_init__(self) -> None:
        if self.max_single_order_notional is not None:
            _require_finite(
                self.max_single_order_notional, "max_single_order_notional"
            )
        if self.max_daily_loss is not None:
            _require_finite(self.max_daily_loss, "max_daily_loss")
        _require_finite(self.velocity_window_seconds, "velocity_window_seconds")
        # Same defensive freeze as AccountState — limits should not change
        # mid-flight via aliased dicts the caller still holds.
        object.__setattr__(
            self,
            "max_position_notional",
            _freeze_mapping("max_position_notional", self.max_position_notional),
        )
        object.__setattr__(
            self,
            "max_sector_concentration_pct",
            _freeze_mapping(
                "max_sector_concentration_pct", self.max_sector_concentration_pct
            ),
        )
        object.__setattr__(
            self,
            "max_asset_class_concentration_pct",
            _freeze_mapping(
                "max_asset_class_concentration_pct",
                self.max_asset_class_concentration_pct,
            ),
        )


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    breached_limits: tuple[str, ...]
    warnings: tuple[str, ...]


class RiskGate:
    """Pre-trade risk gate. Aggregates all breaches per check.

    Thread-safe: :meth:`check` and :meth:`reset_circuit_breaker` are protected
    by an internal :class:`threading.Lock` so concurrent callers cannot corrupt
    the rolling-velocity buffer or the circuit-breaker flag.
    """

    def __init__(
        self,
        limits: RiskLimits,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.limits = limits
        self._clock: Callable[[], float] = clock or time.monotonic
        self._order_timestamps: list[float] = []
        self._circuit_breaker_tripped: bool = False
        self._lock = threading.Lock()

    def reset_circuit_breaker(self) -> None:
        with self._lock:
            self._circuit_breaker_tripped = False

    def check(self, intent: OrderIntent, state: AccountState) -> RiskDecision:
        with self._lock:
            return self._check_locked(intent, state)

    def _check_locked(
        self, intent: OrderIntent, state: AccountState
    ) -> RiskDecision:
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

        # Cache `now` once so the window-start filter and the appended
        # timestamp for an approved order share the same instant — avoids
        # subtle ordering drift when the injected clock is non-monotonic.
        if self.limits.max_orders_per_window is not None:
            now = self._clock()
            window_start = now - self.limits.velocity_window_seconds
            self._order_timestamps = [
                t for t in self._order_timestamps if t >= window_start
            ]
            if len(self._order_timestamps) >= self.limits.max_orders_per_window:
                breaches.append("velocity")

            approved = not breaches
            if approved:
                self._order_timestamps.append(now)
        else:
            approved = not breaches

        return RiskDecision(
            approved=approved,
            breached_limits=tuple(breaches),
            warnings=(),
        )


__all__ = [
    "AccountState",
    "OrderIntent",
    "RiskDecision",
    "RiskGate",
    "RiskLimits",
    "RiskLimitsError",
]
