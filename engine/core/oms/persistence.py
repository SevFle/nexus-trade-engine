"""OMS → DB projection (gh#111 follow-up).

Projects the immutable :class:`engine.core.oms.order.Order` state-
machine entity into a dict shape suitable for ``Order(**d)`` against
the existing :class:`engine.db.models.Order` ORM row.

Why a projection
----------------
The OMS dataclass and the DB row are intentionally different
abstractions:

- The dataclass is event-sourced and immutable per state. Apply an
  event, get a new dataclass.
- The DB row is a snapshot indexed for portfolio queries.

Keeping them at arm's length means the SM stays pure (no SQLAlchemy
import), and the DB schema can evolve without re-shaping the SM.
This module is the only place that knows how to map between them.

Field gaps (explicit follow-ups)
--------------------------------
The current ORM row at ``engine/db/models.py:90`` only carries:
``id``, ``portfolio_id``, ``symbol``, ``side``, ``order_type``,
``quantity``, ``price``, ``status``, ``filled_at``, ``created_at``.

The OMS dataclass also tracks:
- ``filled_quantity`` and ``average_fill_price`` (partial-fill state).
- ``broker_order_id``.
- ``stop_price``.
- ``reject_reason``.
- ``updated_at``.

These are dropped on projection today. Extending the ORM is a
separate migration; this projection's contract is "fit the columns
that exist". A follow-up will add the missing columns and remove
the lossy projection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from engine.core.oms.states import OrderStatus

if TYPE_CHECKING:
    import uuid

    from engine.core.oms.order import Order


# Map state-machine OrderStatus values to the loose strings the ORM's
# ``status`` column has historically used. ``"pending"`` is the ORM's
# default; we coerce NEW/SUBMITTED to it because the DB doesn't model
# the broker-submission distinction yet.
_STATUS_PROJECTION: dict[OrderStatus, str] = {
    OrderStatus.NEW: "pending",
    OrderStatus.SUBMITTED: "pending",
    OrderStatus.ACKNOWLEDGED: "open",
    OrderStatus.PARTIALLY_FILLED: "partially_filled",
    OrderStatus.FILLED: "filled",
    OrderStatus.CANCEL_REQUESTED: "cancel_pending",
    OrderStatus.CANCELLED: "cancelled",
    OrderStatus.REJECTED: "rejected",
    OrderStatus.EXPIRED: "expired",
}


def to_orm_dict(order: Order, *, portfolio_id: uuid.UUID) -> dict[str, Any]:
    """Return a dict suitable for ``Order(**d).save()``.

    The OMS doesn't model portfolio ownership — the caller supplies
    ``portfolio_id`` (typically from the strategy / order-source row
    that originated the request).
    """
    status = _STATUS_PROJECTION.get(order.status, order.status.value)

    # Effective price for the ORM's loose ``price`` column.
    # Limit/stop-limit orders carry a limit_price; market orders have
    # an average fill price once filled, otherwise None.
    price = order.limit_price
    if price is None and order.average_fill_price is not None:
        price = order.average_fill_price

    # filled_at is the moment the order reached a fully-filled
    # terminal state. Use updated_at (which is the apply_event time
    # of the FillEvent / final PartialFillEvent).
    filled_at = order.updated_at if order.status == OrderStatus.FILLED else None

    return {
        "id": order.id,
        "portfolio_id": portfolio_id,
        "symbol": order.symbol,
        "side": order.side.value,
        "order_type": order.order_type.value,
        "quantity": order.quantity,
        "price": price,
        "status": status,
        "filled_at": filled_at,
        "created_at": order.created_at,
    }
