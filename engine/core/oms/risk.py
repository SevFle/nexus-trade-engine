"""OMS pre-flight risk checks (gh#111 follow-up).

A :class:`RiskGate` runs an ordered list of :class:`RiskCheck`
implementations against an :class:`Order` *before* the OMS submits
it to a broker. Any check that returns :class:`Reject` short-
circuits the gate; the OMS must then transition the order via
:class:`engine.core.oms.events.RejectEvent`.

Why pre-flight
--------------
The state machine in :mod:`engine.core.oms.order` is correct but it
trusts every event it sees. Risk policy is a separate concern:
"would I send this thing to the broker at all?". Keeping it in its
own module keeps the state machine pure and lets operators compose
their own check chain at startup.

Built-in checks
---------------
- :class:`KillSwitchCheck` — refuses to submit when the global
  :func:`engine.core.live.get_kill_switch` switch is engaged.
- :class:`MaxOrderQuantity` — refuses orders whose ``quantity``
  exceeds an operator-configured ceiling.
- :class:`MaxOrderNotional` — refuses orders whose ``quantity *
  reference_price`` exceeds a notional ceiling. The reference
  price is supplied by the caller (typically the last trade /
  mid-quote of the symbol).

What's NOT here (explicit follow-ups):
- Per-symbol position caps (needs portfolio state).
- Per-strategy max-drawdown (needs PnL state).
- Buying-power / margin pre-flight (needs broker account state).
- Restricted-list enforcement (needs compliance feed).
- Crypto-specific 24h-trade caps.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import structlog

from engine.observability.metrics import MetricsBackend, get_metrics

if TYPE_CHECKING:
    from decimal import Decimal

    from engine.core.live.kill_switch import KillSwitch
    from engine.core.oms.order import Order


logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Approve:
    """The check approves the order; the gate continues to the next check."""


@dataclass(frozen=True)
class Reject:
    """The check rejects the order. ``reason`` is surfaced to the
    operator and to the eventual ``RejectEvent`` on the OMS."""

    reason: str


CheckResult = Approve | Reject


@runtime_checkable
class RiskCheck(Protocol):
    """Anything callable with ``(Order, *, reference_price)`` is a check."""

    def __call__(self, order: Order, *, reference_price: Decimal | None = None) -> CheckResult: ...


# ---------------------------------------------------------------------------
# Built-in checks
# ---------------------------------------------------------------------------


class KillSwitchCheck:
    """Refuses to submit when the global kill-switch is engaged."""

    def __init__(self, switch: KillSwitch | None = None) -> None:
        # Late import to avoid a hard dependency for tests that pass
        # their own switch in.
        if switch is None:
            from engine.core.live import get_kill_switch  # noqa: PLC0415

            switch = get_kill_switch()
        self._switch = switch

    def __call__(self, order: Order, *, reference_price: Decimal | None = None) -> CheckResult:  # noqa: ARG002
        if self._switch.is_engaged():
            snap = self._switch.snapshot()
            return Reject(reason=f"kill-switch engaged: {snap.reason or 'no reason recorded'}")
        return Approve()


@dataclass(frozen=True)
class MaxOrderQuantity:
    """Refuses orders whose ``quantity`` exceeds ``limit``."""

    limit: Decimal

    def __post_init__(self) -> None:
        if self.limit <= 0:
            raise ValueError("MaxOrderQuantity.limit must be positive")

    def __call__(self, order: Order, *, reference_price: Decimal | None = None) -> CheckResult:  # noqa: ARG002
        if order.quantity > self.limit:
            return Reject(
                reason=(
                    f"order quantity {order.quantity} exceeds max {self.limit} for {order.symbol}"
                )
            )
        return Approve()


@dataclass(frozen=True)
class MaxOrderNotional:
    """Refuses orders whose ``quantity × reference_price`` exceeds ``limit``.

    The check skips silently when the caller cannot supply a
    reference price (returns :class:`Approve`). Operators who want
    "no price = block" should chain their own check.
    """

    limit: Decimal

    def __post_init__(self) -> None:
        if self.limit <= 0:
            raise ValueError("MaxOrderNotional.limit must be positive")

    def __call__(self, order: Order, *, reference_price: Decimal | None = None) -> CheckResult:
        if reference_price is None or reference_price <= 0:
            return Approve()
        notional = order.quantity * reference_price
        if notional > self.limit:
            return Reject(
                reason=(
                    f"order notional {notional} exceeds max {self.limit} "
                    f"for {order.symbol} at price {reference_price}"
                )
            )
        return Approve()


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


class RiskGate:
    """Runs an ordered list of checks. First Reject wins."""

    def __init__(
        self,
        checks: list[RiskCheck],
        *,
        metrics: MetricsBackend | None = None,
    ) -> None:
        self._checks = list(checks)
        self._metrics = metrics

    @property
    def metrics(self) -> MetricsBackend:
        """Resolve the metrics backend lazily so tests can swap the
        process singleton via :func:`set_metrics` after construction."""
        return self._metrics if self._metrics is not None else get_metrics()

    def evaluate(
        self,
        order: Order,
        *,
        reference_price: Decimal | None = None,
    ) -> CheckResult:
        metrics = self.metrics
        for check in self._checks:
            check_name = type(check).__name__
            result = check(order, reference_price=reference_price)
            if isinstance(result, Reject):
                metrics.counter(
                    "oms.risk.check",
                    tags={
                        "check": check_name,
                        "symbol": order.symbol,
                        "outcome": "reject",
                    },
                )
                metrics.counter(
                    "oms.risk.rejected",
                    tags={"check": check_name, "symbol": order.symbol},
                )
                logger.warning(
                    "oms.risk_rejected",
                    check=check_name,
                    order_id=str(order.id),
                    symbol=order.symbol,
                    quantity=str(order.quantity),
                    reason=result.reason,
                )
                return result
            metrics.counter(
                "oms.risk.check",
                tags={
                    "check": check_name,
                    "symbol": order.symbol,
                    "outcome": "approve",
                },
            )
        metrics.counter(
            "oms.risk.approved",
            tags={"symbol": order.symbol},
        )
        return Approve()
