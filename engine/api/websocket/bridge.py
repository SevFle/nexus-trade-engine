"""EventBus → ConnectionManager bridge (gh#7 follow-up).

Subscribes to domain events on the engine's :class:`EventBus` and
forwards them to the per-user :class:`ConnectionManager` that backs
the WebSocket route. The bridge maps an :class:`EventType` to a
:class:`Topic` and a per-event ``user_id`` so each connection only
sees the events it asked for *and* is entitled to.

Routing rules
-------------
- ``order.*``        → Topic.ORDER       — requires ``user_id`` in event data.
- ``portfolio.*``    → Topic.PORTFOLIO   — requires ``user_id`` in event data.
- ``backtest.*``     → Topic.BACKTEST    — requires ``user_id`` in event data.
- ``alert.*``        → Topic.ALERT       — requires ``user_id`` in event data.

Events without a ``user_id`` are dropped at the bridge with a warning;
they likely belong on a system-wide channel that ``Topic`` does not
yet model. Adding one is a follow-up.

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


# Prefix-based mapping. The first matching prefix wins.
_PREFIX_MAP: tuple[tuple[str, Topic], ...] = (
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
        carries ``event_type``, ``data``, ``source``, ``timestamp``.
        """
        et_raw = payload.get("event_type") or payload.get("type")
        if not et_raw:
            logger.warning("ws_bridge.event_missing_type", payload_keys=list(payload.keys()))
            return
        topic = topic_for_event_type(et_raw)
        if topic is None:
            logger.debug("ws_bridge.event_unrouted", event_type=et_raw)
            return
        user_id = extract_user_id(payload.get("data"))
        if user_id is None:
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
        )
        logger.debug(
            "ws_bridge.broadcast",
            event_type=et_raw,
            topic=topic.value,
            user_id=str(user_id),
            recipients=recipients,
        )


__all__ = [
    "EventToWebSocketBridge",
    "extract_user_id",
    "topic_for_event_type",
]
