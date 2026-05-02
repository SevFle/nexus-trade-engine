"""Order vocabulary + lifecycle transitions (gh#111).

Lifecycle
---------
Initial state: ``NEW`` (the engine has minted an Order; nothing has
been told to the broker yet).

Normal flow: ``NEW → SUBMITTED → ACKNOWLEDGED → PARTIALLY_FILLED* → FILLED``.
Terminal at ``FILLED``, ``CANCELLED``, ``REJECTED``, ``EXPIRED``.

States and transitions are documented as ``VALID_TRANSITIONS`` so
both the OMS itself *and* monitoring code can validate broker-emitted
events without re-reading the implementation.
"""

from __future__ import annotations

from enum import Enum


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class OrderStatus(str, Enum):
    NEW = "new"  # minted in the engine; not yet sent to broker
    SUBMITTED = "submitted"  # sent to broker, waiting for ack
    ACKNOWLEDGED = "acknowledged"  # broker accepted; resting on the book
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_REQUESTED = "cancel_requested"  # we asked to cancel; awaiting confirmation
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


_TERMINAL: frozenset[OrderStatus] = frozenset(
    {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED}
)


def is_terminal(status: OrderStatus) -> bool:
    return status in _TERMINAL


# Allowed forward transitions. Reverse transitions are intentionally
# excluded — once an order has been ``CANCELLED`` it does not become
# ``ACKNOWLEDGED`` again. If you need to retry, mint a new order.
VALID_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.NEW: frozenset(
        {OrderStatus.SUBMITTED, OrderStatus.REJECTED, OrderStatus.CANCELLED}
    ),
    OrderStatus.SUBMITTED: frozenset(
        {
            OrderStatus.ACKNOWLEDGED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.REJECTED,
            OrderStatus.CANCEL_REQUESTED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
        }
    ),
    OrderStatus.ACKNOWLEDGED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCEL_REQUESTED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
            OrderStatus.REJECTED,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,  # repeated partial fills
            OrderStatus.FILLED,
            OrderStatus.CANCEL_REQUESTED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
        }
    ),
    OrderStatus.CANCEL_REQUESTED: frozenset(
        {
            OrderStatus.CANCELLED,
            OrderStatus.PARTIALLY_FILLED,  # broker filled before cancel landed
            OrderStatus.FILLED,            # broker fully filled before cancel landed
            OrderStatus.REJECTED,          # cancel itself rejected; back in flight
        }
    ),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
    OrderStatus.EXPIRED: frozenset(),
}


def can_transition(from_status: OrderStatus, to_status: OrderStatus) -> bool:
    return to_status in VALID_TRANSITIONS.get(from_status, frozenset())
