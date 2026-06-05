"""EventBus → ConnectionManager bridge (gh#7 + SEV-275).

Subscribes to domain events on the engine's :class:`EventBus` and
forwards them to the per-user :class:`ConnectionManager` that backs
the WebSocket route. The bridge maps an :class:`EventType` to a
:class:`Topic` and a per-event ``user_id`` so each connection only
sees the events it asked for *and* is entitled to.

SEV-275 changes
---------------
- Propagates an optional ``correlation_id`` from the event ``data``
  dict into the outbound envelope so a client's request id survives
  the round trip.
- Forwards the canonical event type as the envelope's ``event`` field
  (previously the envelope dropped the event type into ``data``).

Routing rules
-------------
- ``order.*``        → Topic.ORDER       — requires ``user_id`` in event data.
- ``portfolio.*``    → Topic.PORTFOLIO   — requires ``user_id`` in event data.
- ``backtest.*``     → Topic.BACKTEST    — requires ``user_id`` in event data.
- ``alert.*``        → Topic.ALERT       — requires ``user_id`` in event data.
- ``market.data.*``  → Topic.MARKET_DATA — optional ``user_id`` (broadcasts
                                            to every subscribed user when
                                            absent — system-wide market
                                            updates).

Events without a ``user_id`` are dropped at the bridge for the per-
user topics with a warning; they likely belong on a system-wide
channel that ``Topic`` does not yet model.

Single-process today
--------------------
The bridge is in-process. Multi-replica fan-out (Redis pubsub) lives
under the same gh#7 follow-up tracker as the manager itself.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import structlog

from engine.api.websocket.manager import Topic

if TYPE_CHECKING:
    from engine.api.websocket.manager import ConnectionManager
    from engine.events.bus import EventBus, EventType


logger = structlog.get_logger()


# Prefix-based mapping. The first matching prefix wins. ``market.data.``
# is *first* so the more-specific prefix wins over any future generic
# matcher.
_PREFIX_MAP: tuple[tuple[str, Topic], ...] = (
    ("market.data.", Topic.MARKET_DATA),
    ("order.", Topic.ORDER),
    ("portfolio.", Topic.PORTFOLIO),
    ("backtest.", Topic.BACKTEST),
    ("alert.", Topic.ALERT),
)


def topic_for_event_type(event_type: str | EventType) -> Topic | None:
    """Return the :class:`Topic` an event type maps to, or ``None``.

    ``None`` means the bridge does not route this event type. The
    caller logs and drops it.
    """
    raw = event_type.value if hasattr(event_type, "value") else str(event_type)
    for prefix, topic in _PREFIX_MAP:
        if raw.startswith(prefix):
            return topic
    return None


def extract_user_id(event_data: dict[str, Any] | None) -> uuid.UUID | None:
    """Pull the addressee user_id from an event's ``data`` dict.

    Accepts both ``"user_id"`` and ``"userId"`` for forward-
    compatibility with frontends that pre-camelCase. Returns ``None``
    if the value is missing or unparseable.
    """
    if not event_data:
        return None
    raw = event_data.get("user_id") or event_data.get("userId")
    if raw is None:
        return None
    if isinstance(raw, uuid.UUID):
        return raw
    try:
        return uuid.UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None


def extract_correlation_id(event_payload: dict[str, Any]) -> str | None:
    """Pull a correlation id out of either the envelope or the data dict.

    The EventBus carries correlation ids at two layers:

    - The outer event envelope (``payload["correlation_id"]``) — preferred.
    - The inner ``data`` dict (``payload["data"]["correlation_id"]``) —
      used by older emitters that pre-date the envelope field.

    Both accept ``correlationId`` (camelCase) for symmetry with
    :func:`extract_user_id`.
    """
    outer = event_payload.get("correlation_id") or event_payload.get(
        "correlationId"
    )
    if isinstance(outer, str) and outer:
        return outer
    data = event_payload.get("data") or {}
    inner = data.get("correlation_id") or data.get("correlationId")
    if isinstance(inner, str) and inner:
        return inner
    return None


class EventToWebSocketBridge:
    """Subscribes to an :class:`EventBus` and broadcasts to a
    :class:`ConnectionManager`.

    The bridge does not own the bus or the manager — it just wires
    them. Lifespan management (start/stop) is the caller's job.
    """

    def __init__(
        self,
        *,
        bus: EventBus,
        manager: ConnectionManager,
    ) -> None:
        self._bus = bus
        self._manager = manager
        self._registered: list[EventType] = []
        # Cache the bound method once. Each access of ``self._handle`` is
        # a fresh wrapper object, so subscribe/unsubscribe must use the
        # same reference for the bus's identity-based bookkeeping.
        self._handler = self._handle

    def attach(self, event_types: list[EventType]) -> None:
        """Subscribe ``handler`` to each ``EventType`` on the bus."""
        for et in event_types:
            self._bus.subscribe(et, self._handler)
            self._registered.append(et)

    def detach(self) -> None:
        """Unsubscribe from every event type previously attached."""
        for et in self._registered:
            try:
                self._bus.unsubscribe(et, self._handler)
            except Exception as exc:
                logger.warning(
                    "ws_bridge.unsubscribe_failed",
                    event_type=getattr(et, "value", str(et)),
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:200],
                )
        self._registered.clear()

    async def _handle(self, payload: dict[str, Any]) -> None:
        """Single bus-handler entry point.

        ``payload`` is the dict produced by ``Event.to_dict()`` — it
        carries ``event_type`` (or legacy ``type``), ``data``,
        ``source``, ``timestamp``.
        """
        et_raw = payload.get("event_type") or payload.get("type")
        if not et_raw:
            logger.warning(
                "ws_bridge.event_missing_type", payload_keys=list(payload.keys())
            )
            return
        topic = topic_for_event_type(et_raw)
        if topic is None:
            logger.debug("ws_bridge.event_unrouted", event_type=et_raw)
            return

        user_id = extract_user_id(payload.get("data"))
        cid = extract_correlation_id(payload)

        if user_id is None:
            # Market data is the one channel that's fan-out-to-everyone;
            # for per-user channels a missing user_id is a wiring bug.
            if topic == Topic.MARKET_DATA:
                await self._broadcast_to_all(
                    topic=topic, event=et_raw, payload=payload, correlation_id=cid
                )
                return
            logger.warning(
                "ws_bridge.event_no_user_id",
                event_type=et_raw,
                data_keys=list((payload.get("data") or {}).keys()),
            )
            return

        recipients = await self._manager.broadcast(
            user_id=user_id,
            topic=topic.value,
            payload=payload,
            event=et_raw,
            correlation_id=cid,
        )
        logger.debug(
            "ws_bridge.broadcast",
            event_type=et_raw,
            topic=topic.value,
            user_id=str(user_id),
            recipients=recipients,
        )

    async def _broadcast_to_all(
        self,
        *,
        topic: Topic,
        event: str,
        payload: dict[str, Any],
        correlation_id: str | None,
    ) -> None:
        """Fan a market-data event out to every user that's listening."""
        # ``_conns`` is the manager's user-indexed registry; we walk it
        # without taking the async lock because iteration over a dict
        # is safe under CPython for the duration of a single bytecode
        # and ``broadcast`` takes its own lock for the per-user fan-out.
        total = 0
        for user_id in list(self._manager._conns.keys()):  # noqa: SLF001
            total += await self._manager.broadcast(
                user_id=user_id,
                topic=topic.value,
                payload=payload,
                event=event,
                correlation_id=correlation_id,
            )
        logger.debug(
            "ws_bridge.broadcast_market_data",
            event_type=event,
            recipients=total,
        )


__all__ = [
    "EventToWebSocketBridge",
    "extract_correlation_id",
    "extract_user_id",
    "topic_for_event_type",
]
