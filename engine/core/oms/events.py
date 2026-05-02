"""OMS event types (gh#111).

Each event represents one external (broker-emitted or operator-issued)
fact about an order. Events are frozen dataclasses so they serialise
cleanly to JSON and survive a database round-trip without bespoke
wiring.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class _BaseEvent:
    occurred_at: datetime


@dataclass(frozen=True)
class SubmitEvent(_BaseEvent):
    """Engine sent the order to the broker."""

    broker_order_id: str | None = None


@dataclass(frozen=True)
class AckEvent(_BaseEvent):
    """Broker accepted the order; it's resting on the book."""

    broker_order_id: str = ""


@dataclass(frozen=True)
class PartialFillEvent(_BaseEvent):
    """Some quantity filled; more remains."""

    fill_quantity: Decimal = Decimal("0")
    fill_price: Decimal = Decimal("0")
    fill_id: str = ""


@dataclass(frozen=True)
class FillEvent(_BaseEvent):
    """Final fill — order is fully filled."""

    fill_quantity: Decimal = Decimal("0")
    fill_price: Decimal = Decimal("0")
    fill_id: str = ""


@dataclass(frozen=True)
class CancelEvent(_BaseEvent):
    """Either we requested the cancel (``requested=True``) or the
    broker confirmed it (``requested=False``). The OMS uses the flag
    to distinguish ``CANCEL_REQUESTED`` from ``CANCELLED``."""

    requested: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class RejectEvent(_BaseEvent):
    """Broker rejected the order (or a cancel attempt)."""

    reason: str = ""


@dataclass(frozen=True)
class ExpireEvent(_BaseEvent):
    """Broker expired the order (TIF reached)."""


# Algebraic-data-type alias — every concrete event in the system.
OrderEvent = (
    SubmitEvent
    | AckEvent
    | PartialFillEvent
    | FillEvent
    | CancelEvent
    | RejectEvent
    | ExpireEvent
)


__all__ = [
    "AckEvent",
    "CancelEvent",
    "ExpireEvent",
    "FillEvent",
    "OrderEvent",
    "PartialFillEvent",
    "RejectEvent",
    "SubmitEvent",
]
