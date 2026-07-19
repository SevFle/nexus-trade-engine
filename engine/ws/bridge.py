"""EventBus → WebSocket bridge for order, trade, and signal events.

This is the focused, dependency-light counterpart to the broader
:class:`~engine.api.ws.event_bridge.EventBusBridge`. It subscribes to a
small, well-defined set of :class:`~engine.events.bus.EventType`
members (order fills / cancels and generated signals), serializes each
event's payload to a JSON-safe dict, and forwards it to a
``ConnectionManager.broadcast(channel, message)`` call keyed by the
event type's channel.

Scope
-----
Per the design brief for this cycle:

- **In:** subscribe → serialize → broadcast, lifecycle hooks, unit
  tests with mock bus + manager.
- **Out:** client-side filtering (rooms, scopes), auth checks,
  per-user routing. Those concerns live in the route / permission
  layer; the bridge intentionally stays agnostic of them.

The bridge is duck-typed against its dependencies. It expects:

- ``bus.subscribe(event_type, handler)`` / ``bus.unsubscribe(event_type, handler)``
- ``await manager.broadcast(channel, message) -> int``

…so tests can substitute light fakes without touching the real
EventBus or ConnectionManager.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import structlog

from engine.events.bus import EventType

if TYPE_CHECKING:
    from collections.abc import Iterable

    from engine.api.ws.connection_manager import ConnectionManager
    from engine.events.bus import EventBus


logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Default event-type → channel mapping.
#
# This is the set of event types the brief calls out explicitly. The
# ``orders`` channel covers all order lifecycle terminations (full fill,
# partial fill, cancel); the ``signals`` channel covers generated
# signals that drive trading decisions. Both channel names are plain
# strings — the bridge does not validate them against any channel
# registry, by design (no client-side filtering this cycle).
# ---------------------------------------------------------------------------
DEFAULT_EVENT_CHANNELS: dict[EventType, str] = {
    EventType.ORDER_FILLED: "orders",
    EventType.ORDER_PARTIALLY_FILLED: "orders",
    EventType.ORDER_CANCELLED: "orders",
    EventType.SIGNAL_GENERATED: "signals",
}

#: The default list of event types the bridge subscribes to. Exposed as
#: a tuple so callers can't mutate the bridge's defaults by accident.
DEFAULT_EVENT_TYPES: tuple[EventType, ...] = tuple(DEFAULT_EVENT_CHANNELS.keys())


def _json_safe(value: Any) -> Any:
    """Recursively coerce ``value`` into a JSON-serializable structure.

    The EventBus payload is an arbitrary dict — values may include
    :class:`~datetime.datetime`, :class:`~uuid.UUID`, :class:`~enum.Enum`,
    sets, decimals, or Pydantic models. WebSocket clients receive JSON,
    so every value must be normalized before it is handed to
    :meth:`ConnectionManager.broadcast`.

    The conversion is deliberately permissive: anything ``json.dumps``
    already understands passes through unchanged, and anything it does
    not is stringified via ``default=str``. This keeps the bridge
    resilient to schema drift in event producers (a new non-JSON field
    on an event must not break the broadcast path).
    """
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, set):
        # ``set`` is unordered and not JSON-native; sort for determinism
        # so tests and downstream consumers see a stable payload.
        try:
            items = sorted(value)
        except TypeError:
            # Unsortable elements (mixed types) — fall back to insertion
            # order rather than raising.
            items = value
        return [_json_safe(v) for v in items]
    # Catch-all: datetimes, UUIDs, Decimals, Enums, Pydantic models, etc.
    # ``default=str`` produces a stable, lossy-but-readable string form.
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return str(value)
    else:
        return value


def serialize_event(payload: dict[str, Any]) -> dict[str, Any]:
    """Build the JSON-safe broadcast envelope for an event payload.

    The envelope preserves the EventBus' ``Event.to_dict()`` shape
    (``type`` / ``data`` / ``source`` / ``timestamp``) and additionally
    stamps the resolved ``channel`` so connected clients can route the
    message client-side without re-parsing the event type. Every nested
    value is run through :func:`_json_safe` first so the result is
    guaranteed :func:`json.dumps`-serializable.
    """
    return {
        "type": "event",
        "event_type": _json_safe(payload.get("type") or payload.get("event_type")),
        "channel": None,  # filled in by the bridge once resolved
        "data": _json_safe(payload.get("data") or {}),
        "source": _json_safe(payload.get("source")),
        "timestamp": _json_safe(payload.get("timestamp")),
    }


class EventBusBridge:
    """Subscribes to an :class:`EventBus` and broadcasts to a
    :class:`ConnectionManager`.

    The bridge does not own the bus or the manager — it just wires them
    together. Lifecycle (``start`` / ``stop``) is the caller's job;
    the FastAPI lifespan hook in :mod:`engine.app` is the canonical
    caller.

    Parameters
    ----------
    bus:
        Anything with ``subscribe(event_type, handler)`` and
        ``unsubscribe(event_type, handler)``. The handler registered is
        an awaitable ``handler(payload: dict) -> None``.
    manager:
        Anything with an async ``broadcast(channel, message) -> int``.
        The bridge is agnostic to whether ``channel`` is a room id or a
        flat channel name — it just passes the configured string
        through.
    channels:
        Optional override of the default
        :data:`DEFAULT_EVENT_CHANNELS` mapping. Allows callers to extend
        or narrow the routed set without subclassing. Keys must be
        :class:`~engine.events.bus.EventType` members; values are the
        channel string to broadcast on.
    """

    def __init__(
        self,
        bus: EventBus,
        manager: ConnectionManager,
        *,
        channels: dict[EventType, str] | None = None,
    ) -> None:
        self._bus = bus
        self._manager = manager
        # Copy the mapping so a caller mutating their dict later can't
        # silently re-route events the bridge has already subscribed to.
        self._channels: dict[EventType, str] = dict(
            channels if channels is not None else DEFAULT_EVENT_CHANNELS
        )
        self._registered: list[EventType] = []
        # Cache the bound method. Each ``self._handle`` access produces a
        # fresh wrapper, so subscribe / unsubscribe must reference the
        # *same* object for the bus's identity-based bookkeeping.
        self._handler = self._handle

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def channels(self) -> dict[EventType, str]:
        """Return a defensive copy of the resolved channel mapping."""
        return dict(self._channels)

    @property
    def subscribed_event_types(self) -> tuple[EventType, ...]:
        """Event types the bridge is currently subscribed to."""
        return tuple(self._registered)

    def channel_for(self, event_type: EventType | str) -> str | None:
        """Return the channel an event type maps to, or ``None`` if unrouted.

        ``None`` signals a no-op: the bridge does not broadcast for this
        event type. This is the path the "no-op events are ignored"
        contract relies on.
        """
        # ``EventType`` is a ``StrEnum``, so a member and its dotted
        # string value hash identically. Try both forms for robustness
        # against producers that emit raw strings.
        if event_type in self._channels:
            return self._channels[event_type]
        if isinstance(event_type, str):
            try:
                et = EventType(event_type)
            except ValueError:
                return None
            return self._channels.get(et)
        return None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, event_types: Iterable[EventType] | None = None) -> None:
        """Subscribe the handler to each event type the bridge routes.

        Without an explicit ``event_types`` argument the bridge
        subscribes to every key of its channel map. Passing an explicit
        list lets callers start a subset (e.g. only order events) —
        every type must still have a configured channel or this method
        raises :class:`ValueError` so the misconfiguration is loud
        rather than silently dropping events.

        Idempotent-ish: calling ``start`` twice double-subscribes the
        handler. The FastAPI lifespan calls it exactly once; tests that
        re-start should ``stop`` first.
        """
        types = list(event_types) if event_types is not None else list(self._channels.keys())
        # Validate every requested type has a channel before mutating
        # any state — half-wired bridges are a footgun.
        missing = [et for et in types if et not in self._channels]
        if missing:
            names = ", ".join(getattr(et, "value", str(et)) for et in missing)
            raise ValueError(f"no channel configured for event type(s): {names}")

        for et in types:
            self._bus.subscribe(et, self._handler)
            self._registered.append(et)
        logger.info(
            "ws.bridge.started",
            event_types=[getattr(et, "value", str(et)) for et in self._registered],
            channels=len(self._channels),
        )

    def stop(self) -> None:
        """Unsubscribe from every event type previously attached.

        Errors unsubscribing a single type are logged and swallowed so
        one stuck unsubscribe can't prevent the rest of the teardown
        (and a graceful shutdown) from completing. ``subscribed_event_types``
        is empty after this returns.
        """
        for et in self._registered:
            try:
                self._bus.unsubscribe(et, self._handler)
            except Exception as exc:
                logger.warning(
                    "ws.bridge.unsubscribe_failed",
                    event_type=getattr(et, "value", str(et)),
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:200],
                )
        count = len(self._registered)
        self._registered.clear()
        logger.info("ws.bridge.stopped", unsubscribed=count)

    # ------------------------------------------------------------------
    # Handler
    # ------------------------------------------------------------------

    async def _handle(self, payload: dict[str, Any]) -> None:
        """Single bus-handler entry point.

        ``payload`` is the dict produced by ``Event.to_dict()`` — it
        carries ``type`` (or ``event_type``), ``data``, ``source``,
        ``timestamp``. The handler:

        1. Resolves the channel for the payload's event type. Unknown
           types are a no-op (logged at debug) — this is the contract
           the "no-op events are ignored" test enforces.
        2. Serializes the payload to a JSON-safe envelope.
        3. Forwards to ``manager.broadcast(channel, envelope)``.

        Any broadcast-side exception is logged and swallowed so a
        misbehaving manager can't poison the EventBus dispatch loop
        (which would block other, unrelated handlers).
        """
        if not isinstance(payload, dict):
            logger.warning("ws.bridge.non_dict_payload", payload_type=type(payload).__name__)
            return

        raw_type = payload.get("type") or payload.get("event_type")
        if not raw_type:
            logger.warning(
                "ws.bridge.event_missing_type",
                payload_keys=list(payload.keys()),
            )
            return

        channel = self.channel_for(raw_type)
        if channel is None:
            # No-op: this event type is not in our channel map. Logged
            # at debug because this is the expected path for any event
            # the bus delivers to a handler that the bridge happens to
            # share with another subscriber.
            logger.debug("ws.bridge.event_unrouted", event_type=raw_type)
            return

        envelope = serialize_event(payload)
        envelope["channel"] = channel

        try:
            recipients = await self._manager.broadcast(channel, envelope)
        except Exception:
            logger.exception(
                "ws.bridge.broadcast_failed",
                event_type=raw_type,
                channel=channel,
            )
            return

        logger.debug(
            "ws.bridge.broadcast",
            event_type=raw_type,
            channel=channel,
            recipients=recipients,
        )


__all__ = [
    "DEFAULT_EVENT_CHANNELS",
    "DEFAULT_EVENT_TYPES",
    "EventBusBridge",
    "serialize_event",
]
