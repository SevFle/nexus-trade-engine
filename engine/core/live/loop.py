"""Live-trading loop driver (gh#109 follow-up).

Wires the OMS state machine, the risk gate, and a broker adapter into
the smallest unit of "submit an order, then react to broker events".

Flow
----
1. Caller hands :meth:`LiveLoop.submit` an :class:`Order` plus an
   optional reference price.
2. The driver runs the :class:`RiskGate`. On Reject, the order
   transitions to ``REJECTED`` via a :class:`RejectEvent` and the
   broker is never called.
3. On Approve, the driver calls :meth:`BrokerAdapter.submit`. The
   resulting ``broker_order_id`` is recorded so subsequent events can
   correlate. The Order itself is held in an in-memory registry keyed
   by both the OMS id and the broker id.
4. The caller invokes :meth:`apply_broker_event` for each broker
   event consumed from ``broker.events()`` — the loop looks up the
   originating Order, applies the event via the state machine, and
   notifies the operator-supplied ``persister`` callback.

Error policy
------------
- :class:`BrokerAuthError` — engage the kill-switch (this is a
  systemic condition; we cannot trust the engine to keep submitting
  orders) and re-raise.
- :class:`BrokerRejectError` — apply :class:`RejectEvent` to the order
  with the broker's reason and broker_code. Does NOT engage the
  kill-switch.
- :class:`BrokerConnectionError` — log and re-raise. The caller wraps
  ``submit`` in retry-with-backoff.

What's NOT here (explicit follow-ups):
- Reconciliation on startup (read open orders from broker, walk
  events forward).
- Recovery from a half-submitted order (broker received but engine
  crashed before persisting the broker_order_id).
- Multi-broker routing.
- Fill-handler hooks for downstream tax-lot updates.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog

from engine.core.brokers.base import (
    BrokerAuthError,
    BrokerConnectionError,
    BrokerRejectError,
)
from engine.core.live.kill_switch import KillSwitch, get_kill_switch
from engine.core.oms.events import RejectEvent, SubmitEvent
from engine.core.oms.risk import Reject, RiskGate
from engine.observability.metrics import MetricsBackend, get_metrics

if TYPE_CHECKING:
    from engine.core.brokers.base import BrokerAdapter
    from engine.core.oms.events import OrderEvent
    from engine.core.oms.order import Order


logger = structlog.get_logger()


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


# Operator-supplied persister: called after every successful state
# transition with the new (post-event) Order.
Persister = Callable[["Order"], None]


class LiveLoopError(Exception):
    """Base for live-loop bookkeeping errors."""


class UnknownOrderError(LiveLoopError):
    """A broker event referenced an order id we don't know about."""


class LiveLoop:
    """Submit-and-consume orchestrator.

    The driver is intentionally small. Add features by composing
    rather than extending — risk policies live on the gate, broker
    behaviour on the adapter, persistence in the persister.
    """

    def __init__(
        self,
        *,
        broker: BrokerAdapter,
        risk: RiskGate,
        persister: Persister | None = None,
        kill_switch: KillSwitch | None = None,
        metrics: MetricsBackend | None = None,
    ) -> None:
        self._broker = broker
        self._risk = risk
        self._persister = persister
        self._kill_switch = kill_switch or get_kill_switch()
        self._metrics = metrics
        # OMS id -> Order
        self._by_oms_id: dict[uuid.UUID, Order] = {}
        # Broker id -> OMS id (for event correlation)
        self._broker_to_oms: dict[str, uuid.UUID] = {}

    @property
    def metrics(self) -> MetricsBackend:
        """Resolve the metrics backend lazily so tests can swap the
        process-wide singleton via :func:`set_metrics` after construction."""
        return self._metrics if self._metrics is not None else get_metrics()

    # ------------------------------------------------------------------
    # Submit path
    # ------------------------------------------------------------------

    async def submit(
        self,
        order: Order,
        *,
        reference_price: Decimal | None = None,
    ) -> Order:
        """Run risk + broker submit, register the order, persist.

        Returns the Order (possibly transitioned to REJECTED). Raises
        :class:`BrokerAuthError` / :class:`BrokerConnectionError` for
        the caller to handle.
        """
        metrics = self.metrics
        base_tags = {"symbol": order.symbol, "side": order.side.value}
        metrics.counter("oms.submit.attempted", tags=base_tags)

        gate_result = self._risk.evaluate(order, reference_price=reference_price)
        if isinstance(gate_result, Reject):
            updated = order.apply_event(
                RejectEvent(occurred_at=_utcnow(), reason=gate_result.reason)
            )
            self._track(updated)
            metrics.counter(
                "oms.submit.outcome",
                tags={**base_tags, "outcome": "risk_rejected"},
            )
            return updated

        try:
            submitted = await self._broker.submit(order)
        except BrokerAuthError:
            metrics.counter(
                "oms.submit.outcome",
                tags={**base_tags, "outcome": "broker_auth_error"},
            )
            self._kill_switch.engage(
                reason="broker_auth_error", actor="live_loop"
            )
            raise
        except BrokerRejectError as exc:
            updated = order.apply_event(
                RejectEvent(
                    occurred_at=_utcnow(),
                    reason=f"broker rejected: {exc} (code={exc.broker_code})",
                )
            )
            self._track(updated)
            metrics.counter(
                "oms.submit.outcome",
                tags={**base_tags, "outcome": "broker_rejected"},
            )
            return updated
        except BrokerConnectionError:
            metrics.counter(
                "oms.submit.outcome",
                tags={**base_tags, "outcome": "broker_connection_error"},
            )
            logger.warning(
                "live_loop.broker_connection_error",
                order_id=str(order.id),
                symbol=order.symbol,
            )
            raise

        updated = order.apply_event(
            SubmitEvent(
                occurred_at=_utcnow(),
                broker_order_id=submitted.broker_order_id,
            )
        )
        self._track(updated)
        self._broker_to_oms[submitted.broker_order_id] = updated.id
        metrics.counter(
            "oms.submit.outcome",
            tags={**base_tags, "outcome": "submitted"},
        )
        return updated

    # ------------------------------------------------------------------
    # Event-consumption path
    # ------------------------------------------------------------------

    async def apply_broker_event(
        self,
        event: OrderEvent,
        *,
        broker_order_id: str,
    ) -> Order:
        """Apply a single broker-emitted event to the corresponding order.

        Raises :class:`UnknownOrderError` if ``broker_order_id`` is
        not in this loop's registry.
        """
        oms_id = self._broker_to_oms.get(broker_order_id)
        if oms_id is None:
            raise UnknownOrderError(
                f"no order tracked for broker id {broker_order_id!r}"
            )
        order = self._by_oms_id[oms_id]
        updated = order.apply_event(event)
        self._track(updated)
        self.metrics.counter(
            "oms.event.applied",
            tags={
                "event_type": type(event).__name__,
                "status": updated.status.value,
                "symbol": updated.symbol,
            },
        )
        return updated

    # ------------------------------------------------------------------
    # Lookup helpers
    # ------------------------------------------------------------------

    def get(self, oms_id: uuid.UUID) -> Order | None:
        return self._by_oms_id.get(oms_id)

    def open_orders(self) -> list[Order]:
        return [o for o in self._by_oms_id.values() if not o.is_terminal]

    def __len__(self) -> int:
        return len(self._by_oms_id)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _track(self, order: Order) -> None:
        self._by_oms_id[order.id] = order
        self.metrics.gauge(
            "oms.open_orders",
            float(sum(1 for o in self._by_oms_id.values() if not o.is_terminal)),
        )
        if self._persister is not None:
            try:
                self._persister(order)
            except Exception as exc:  # noqa: BLE001 - persister failures must not break the loop
                logger.warning(
                    "live_loop.persister_failed",
                    order_id=str(order.id),
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:200],
                )
