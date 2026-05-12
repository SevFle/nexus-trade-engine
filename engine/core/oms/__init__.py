"""Order Management System (gh#111).

Today this exposes the core order state machine and the event types
that drive transitions. Broker wiring, persistence, risk checks, and
the live execution loop (gh#109) consume these primitives but live
in their own modules so the state machine can be unit-tested in
isolation.

Public surface:

- :class:`OrderSide`, :class:`OrderType`, :class:`OrderStatus` — the
  vocabulary brokers expect plus the lifecycle states the engine
  models.
- :class:`Order` — single in-flight or terminal order. Immutable
  per state — :meth:`Order.apply_event` returns a *new* Order.
- :class:`OrderEvent` and friends — strongly-typed transitions.
- :func:`is_terminal` — convenience predicate for the end states.
- :data:`VALID_TRANSITIONS` — exposed so tests / monitoring can
  validate broker-emitted events before applying them.
"""

from engine.core.oms.events import (
    AckEvent,
    CancelEvent,
    FillEvent,
    OrderEvent,
    PartialFillEvent,
    RejectEvent,
    SubmitEvent,
)
from engine.core.oms.order import Order
from engine.core.oms.states import (
    VALID_TRANSITIONS,
    OrderSide,
    OrderStatus,
    OrderType,
    is_terminal,
)

__all__ = [
    "VALID_TRANSITIONS",
    "AckEvent",
    "CancelEvent",
    "FillEvent",
    "Order",
    "OrderEvent",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "PartialFillEvent",
    "RejectEvent",
    "SubmitEvent",
    "is_terminal",
]
