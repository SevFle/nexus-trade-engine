"""BrokerAdapter Protocol + error vocabulary (gh#136).

The Protocol is intentionally minimal so per-broker adapters can stay
thin and the live-loop driver can be tested against a fake. Three
methods:

- ``submit`` — send an order to the broker. Returns a
  :class:`SubmittedOrder` with the broker's order id.
- ``cancel`` — request cancellation of an existing broker order.
- ``events`` — async iterator of broker events (acks, fills,
  cancellations). The OMS converts each event to its own internal
  event type via the live-loop driver (gh#109).

Errors are typed so the loop driver can react differently to a
permission failure vs. a network blip:

- :class:`BrokerAuthError` — credentials rejected. Don't retry; surface
  to the operator and engage the kill-switch.
- :class:`BrokerConnectionError` — transient network / disconnect.
  Retry with backoff.
- :class:`BrokerRejectError` — broker accepted the request but rejected
  the order itself (margin, restricted-list, etc.). Treat as a
  per-order rejection; do not engage the kill-switch.

Concrete adapters live under their own subpackages
(``engine/core/brokers/alpaca/``, etc.) and are added in their own PRs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    import uuid
    from collections.abc import AsyncIterator

    from engine.core.oms.events import OrderEvent
    from engine.core.oms.order import Order


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BrokerError(Exception):
    """Base for every failure mode an adapter can surface."""


class BrokerAuthError(BrokerError):
    """Credentials rejected. Permanent until operator intervenes."""


class BrokerConnectionError(BrokerError):
    """Transient network / broker-side disconnect. Retryable."""


class BrokerRejectError(BrokerError):
    """Broker accepted the request but rejected the *order*.

    Per-order failure (insufficient margin, restricted symbol, bad
    price, etc.). Do not treat as a systemic problem.
    """

    def __init__(self, message: str, *, broker_code: str | None = None) -> None:
        super().__init__(message)
        self.broker_code = broker_code


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SubmittedOrder:
    """Result of a successful :meth:`BrokerAdapter.submit` call.

    The OMS uses ``broker_order_id`` to correlate subsequent broker
    events back to the originating in-process order.
    """

    order_id: uuid.UUID  # OMS-side order id (the input)
    broker_order_id: str  # broker's id for the same order


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class BrokerAdapter(Protocol):
    """Per-broker integration contract."""

    @property
    def name(self) -> str:
        """Stable lower-case identifier (e.g. ``"alpaca"``, ``"ibkr"``)."""
        ...

    async def submit(self, order: Order) -> SubmittedOrder:
        """Send ``order`` to the broker. Raises :class:`BrokerError` on failure."""
        ...

    async def cancel(self, *, order_id: uuid.UUID, broker_order_id: str) -> None:
        """Request cancellation. Raises :class:`BrokerError` on failure.

        The broker may not honour the cancel (filled-before-cancel race);
        in that case the next ``events`` yield carries a fill, and the
        OMS state machine does the right thing.
        """
        ...

    def events(self) -> AsyncIterator[OrderEvent]:
        """Async iterator of broker-emitted OMS events.

        Adapter implementations are responsible for translating
        broker-native event shapes into ``engine.core.oms.events``
        types (``AckEvent``, ``PartialFillEvent``, etc.). The live-
        loop consumes this iterator and feeds each event to the
        order's ``apply_event`` to evolve its state.
        """
        ...
