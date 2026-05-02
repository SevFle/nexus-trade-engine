"""Order entity + state-machine application (gh#111).

The :class:`Order` is **immutable per state**. ``apply_event`` returns
a new Order; the old one is left untouched so callers can keep a
history without copying.

The OMS does *not* persist Orders here — that's the caller's job.
This module just owns the rules of how an Order's status, filled
quantity, average fill price, and broker id evolve as events arrive.

Note: ``engine/db/models.py`` defines a SQLAlchemy ``Order`` row for
persisted state. That is a different abstraction — the row is the
*projection* of an event-sourced :class:`Order` into a snapshot the
DB can index. Keep them at arm's length.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal

from engine.core.oms.events import (
    AckEvent,
    CancelEvent,
    ExpireEvent,
    FillEvent,
    OrderEvent,
    PartialFillEvent,
    RejectEvent,
    SubmitEvent,
)
from engine.core.oms.states import (
    OrderSide,
    OrderStatus,
    OrderType,
    can_transition,
    is_terminal,
)


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


class OMSError(Exception):
    """Base for OMS rule violations."""


class IllegalTransitionError(OMSError):
    """Raised when an event would move the order to a status that the
    state machine forbids from the current status."""


class OverFillError(OMSError):
    """Raised when applying a fill would exceed the order's quantity."""


@dataclass(frozen=True)
class Order:
    """In-flight or terminal order.

    Fields with sensible defaults are populated by the engine when it
    mints the order; broker-controlled fields are populated by events.
    """

    id: uuid.UUID = field(default_factory=uuid.uuid4)
    symbol: str = ""
    side: OrderSide = OrderSide.BUY
    order_type: OrderType = OrderType.MARKET
    quantity: Decimal = Decimal("0")
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    status: OrderStatus = OrderStatus.NEW
    filled_quantity: Decimal = Decimal("0")
    average_fill_price: Decimal | None = None
    broker_order_id: str | None = None
    reject_reason: str | None = None
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT):
            if self.limit_price is None or self.limit_price <= 0:
                raise ValueError(
                    f"{self.order_type.value} order requires positive limit_price"
                )
        if self.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
            if self.stop_price is None or self.stop_price <= 0:
                raise ValueError(
                    f"{self.order_type.value} order requires positive stop_price"
                )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def remaining_quantity(self) -> Decimal:
        return self.quantity - self.filled_quantity

    @property
    def is_terminal(self) -> bool:
        return is_terminal(self.status)

    # ------------------------------------------------------------------
    # State-machine application
    # ------------------------------------------------------------------

    def apply_event(self, event: OrderEvent) -> Order:
        """Return a new Order reflecting ``event``.

        Raises :class:`IllegalTransitionError` if ``event`` is not
        valid from the current status, or :class:`OverFillError` if a
        fill would exceed the order's total quantity.
        """
        if isinstance(event, SubmitEvent):
            return self._with_status(
                OrderStatus.SUBMITTED,
                event,
                broker_order_id=event.broker_order_id or self.broker_order_id,
            )
        if isinstance(event, AckEvent):
            return self._with_status(
                OrderStatus.ACKNOWLEDGED,
                event,
                broker_order_id=event.broker_order_id or self.broker_order_id,
            )
        if isinstance(event, PartialFillEvent):
            new_filled = self.filled_quantity + event.fill_quantity
            self._guard_fill(new_filled)
            new_status = (
                OrderStatus.FILLED
                if new_filled == self.quantity
                else OrderStatus.PARTIALLY_FILLED
            )
            return self._with_status(
                new_status,
                event,
                filled_quantity=new_filled,
                average_fill_price=self._next_avg(event.fill_quantity, event.fill_price),
            )
        if isinstance(event, FillEvent):
            new_filled = self.filled_quantity + event.fill_quantity
            self._guard_fill(new_filled)
            if new_filled != self.quantity:
                raise OverFillError(
                    f"FillEvent leaves {new_filled} filled of {self.quantity}; "
                    "use PartialFillEvent for non-final fills"
                )
            return self._with_status(
                OrderStatus.FILLED,
                event,
                filled_quantity=new_filled,
                average_fill_price=self._next_avg(event.fill_quantity, event.fill_price),
            )
        if isinstance(event, CancelEvent):
            target = (
                OrderStatus.CANCEL_REQUESTED if event.requested else OrderStatus.CANCELLED
            )
            return self._with_status(target, event)
        if isinstance(event, RejectEvent):
            return self._with_status(
                OrderStatus.REJECTED, event, reject_reason=event.reason
            )
        if isinstance(event, ExpireEvent):
            return self._with_status(OrderStatus.EXPIRED, event)
        raise TypeError(f"unknown event type: {type(event).__name__}")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _with_status(
        self,
        new_status: OrderStatus,
        event: OrderEvent,
        **changes,
    ) -> Order:
        if not can_transition(self.status, new_status):
            raise IllegalTransitionError(
                f"{self.status.value} -> {new_status.value} is not a legal transition "
                f"(via {type(event).__name__})"
            )
        return replace(
            self,
            status=new_status,
            updated_at=event.occurred_at,
            **changes,
        )

    def _guard_fill(self, new_filled: Decimal) -> None:
        if new_filled > self.quantity:
            raise OverFillError(
                f"fill would push filled_quantity to {new_filled}, "
                f"exceeding order quantity {self.quantity}"
            )

    def _next_avg(self, fill_qty: Decimal, fill_price: Decimal) -> Decimal:
        """Volume-weighted average fill price after this partial / final fill."""
        prior_qty = self.filled_quantity
        prior_avg = self.average_fill_price or Decimal("0")
        new_qty = prior_qty + fill_qty
        if new_qty == 0:
            return Decimal("0")
        return ((prior_avg * prior_qty) + (fill_price * fill_qty)) / new_qty
