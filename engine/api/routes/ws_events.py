"""``/ws/events`` WebSocket endpoint (SEV-298 follow-up).

A minimal, *unauthenticated* vertical slice that streams real-time
:class:`~engine.events.bus.EventBus` events to connected WebSocket
clients. It reuses the two existing primitives and wires them together:

- :class:`engine.api.websocket.manager.ConnectionManager` — the
  **channel-based** pub/sub manager. Each connection is registered under
  a unique id and may subscribe to one or more named channels. The
  manager owns all fan-out, send-error recovery and disconnect cleanup.
- :class:`engine.events.bus.EventBus` — the engine's in-process / Redis
  pub-sub event bus. Domain code publishes :class:`Event` objects to it.

The glue between them is :class:`WsEventsBridge`: a single shared
subscriber that listens to *every* :class:`~engine.events.bus.EventType`
on the bus and re-broadcasts each event (already serialized as a plain
dict via ``Event.to_dict()``) to the channel-based manager. Events fan
out to two channel flavours so clients can pick their granularity:

- ``events`` — the global firehose (every event).
- ``<namespace>`` — the event-type's top-level segment, e.g.
  ``portfolio`` for ``portfolio.updated``, ``order`` for
  ``order.filled``. This lets a client subscribe to a whole domain.

Connection flow
---------------
1. Client opens ``WS /ws/events[?channels=a,b&client_id=...]``.
2. Server accepts, registers the socket with the ``ConnectionManager``,
   and subscribes it to the requested channels (default: ``events``).
3. Server emits a ``{"type": "connected", ...}`` ack so the client knows
   its subscriptions are live (also a clean sync point for tests).
4. The server then idles on ``receive_json`` to detect client
   disconnect and answer ``ping`` with ``pong``. Meanwhile the bridge
   pushes events through the manager whenever the bus fires.
5. On disconnect the connection is detached from the manager, which
   prunes every channel membership it held.

Auth / rate limiting are intentionally out of scope for this slice —
see the module-level TODO and the linked SEV tracker.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import TYPE_CHECKING

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from engine.api.websocket.manager import ConnectionManager
from engine.events.bus import EventBus, EventType

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = structlog.get_logger()

router = APIRouter()

#: Global firehose channel — every published event lands here.
EVENTS_CHANNEL: str = "events"

#: Close code used when the server is not yet initialized.
_WS_CLOSE_NOT_READY = 1011


def event_to_channels(event_type_value: str) -> list[str]:
    """Return the channels an event of ``event_type_value`` fans out to.

    Always includes the global :data:`EVENTS_CHANNEL` firehose plus the
    event-type's top-level namespace segment (the part before the first
    ``"."``). For ``"portfolio.updated"`` that yields
    ``["events", "portfolio"]``; for a bare ``"events"`` it yields just
    ``["events"]``.

    Channel names are constrained to ``[A-Za-z0-9:_-]`` by the manager,
    and every existing :class:`EventType` value is ``<word>.<word>``-ish,
    so the derived namespace is always a legal, non-deny-listed channel.
    """
    namespace = event_type_value.split(".", 1)[0].strip()
    channels = [EVENTS_CHANNEL]
    if namespace and namespace != EVENTS_CHANNEL:
        channels.append(namespace)
    return channels


class WsEventsBridge:
    """Subscribes to an :class:`EventBus` and re-broadcasts to a
    channel-based :class:`ConnectionManager`.

    The bridge owns no state beyond its bus subscription bookkeeping —
    the manager does the actual fan-out. Start/stop lifecycle is the
    caller's responsibility (app lifespan or a test fixture).
    """

    def __init__(self, bus: EventBus, manager: ConnectionManager) -> None:
        self._bus = bus
        self._manager = manager
        self._registered: list[EventType] = []
        # Cache the bound method: ``EventBus.subscribe`` /
        # ``unsubscribe`` use identity-based bookkeeping, so the same
        # wrapper object must be passed to both.
        self._handler = self._handle

    def start(self, event_types: Iterable[EventType] | None = None) -> None:
        """Subscribe the handler to every (or the given) ``EventType``."""
        types = list(event_types) if event_types is not None else list(EventType)
        for et in types:
            self._bus.subscribe(et, self._handler)
            self._registered.append(et)
        logger.info("ws_events.bridge_started", event_types=len(self._registered))

    def stop(self) -> None:
        """Unsubscribe from every event type previously attached.

        Errors are swallowed so a torn-down bus never aborts the
        remaining shutdown steps.
        """
        for et in self._registered:
            with contextlib.suppress(Exception):
                self._bus.unsubscribe(et, self._handler)
        self._registered.clear()
        logger.info("ws_events.bridge_stopped")

    async def _handle(self, payload: dict) -> None:
        """Single bus-handler entry point.

        ``payload`` is the dict produced by ``Event.to_dict()`` — it
        carries ``type`` (the event-type value), ``data``, ``source``
        and ``timestamp``. It is forwarded verbatim (already
        JSON-serializable) so clients see a stable wire shape.
        """
        event_type = payload.get("type")
        if not event_type:
            logger.warning("ws_events.event_missing_type", keys=list(payload.keys()))
            return
        for channel in event_to_channels(str(event_type)):
            try:
                await self._manager.broadcast(channel, payload)
            except Exception:
                logger.exception("ws_events.broadcast_failed", channel=channel)


# ---------------------------------------------------------------------------
# Module-level state + wiring helpers
# ---------------------------------------------------------------------------
#
# Mirrors the ``init_ws`` / ``_state`` pattern used by
# :mod:`engine.api.ws.router`: the app lifespan (or a test fixture) calls
# :func:`init_ws_events` once to inject the shared bus + manager and spin
# up the bridge; the route reads them back through :func:`get_state`.


class _State:
    __slots__ = ("bridge", "bus", "loop", "manager")

    def __init__(self) -> None:
        self.bus: EventBus | None = None
        self.manager: ConnectionManager | None = None
        self.bridge: WsEventsBridge | None = None
        #: The server's running event loop, captured on first connect.
        #: Lets out-of-loop callers (tests, worker threads) publish into
        #: the server loop via :func:`asyncio.run_coroutine_threadsafe`.
        self.loop: asyncio.AbstractEventLoop | None = None


_state = _State()


def init_ws_events(bus: EventBus, manager: ConnectionManager) -> WsEventsBridge:
    """Wire the shared bus + manager and start the broadcast bridge.

    Returns the bridge so the caller can ``stop()`` it on shutdown.
    Safe to call once per process; calling again replaces the prior
    wiring (the old bridge is stopped first).
    """
    if _state.bridge is not None:
        _state.bridge.stop()
    _state.bus = bus
    _state.manager = manager
    bridge = WsEventsBridge(bus, manager)
    bridge.start()
    _state.bridge = bridge
    return bridge


def get_state() -> _State:
    """Expose the module state (bus, manager, bridge, captured loop)."""
    return _state


def reset_ws_events_for_tests() -> None:
    """Tear down any wiring and reset module state to pristine."""
    if _state.bridge is not None:
        _state.bridge.stop()
    _state.bus = None
    _state.manager = None
    _state.bridge = None
    _state.loop = None


@router.websocket("/ws/events")
async def ws_events_endpoint(ws: WebSocket) -> None:
    """Accept a connection, subscribe it to event channels, and stream events.

    Query params (all optional):

    - ``client_id`` — opaque caller identity (defaults to a generated
      uuid; required to be non-empty by the manager so per-user
      ``user:`` isolation rules have something to scope against).
    - ``channels`` — comma-separated channel list to subscribe to. When
      omitted the connection joins the global ``events`` firehose.

    No auth yet (TODO: SEV tracker) — this is the minimal slice.
    """
    manager = _state.manager
    if manager is None or _state.bus is None:
        with contextlib.suppress(Exception):
            await ws.close(code=_WS_CLOSE_NOT_READY, reason="server not ready")
        return

    # Capture the running loop up front (before any await) so callers in
    # other threads can publish into the server loop reliably.
    _state.loop = asyncio.get_running_loop()

    await ws.accept()

    connection_id = uuid.uuid4().hex
    query = ws.query_params
    client_id = (query.get("client_id") or connection_id).strip() or connection_id

    try:
        await manager.connect(connection_id, ws, user_id=client_id)
    except ValueError:
        # Defensive: an empty/whitespace user_id is rejected by the
        # manager. Should not happen given the fallback above, but never
        # leave a half-accepted socket open.
        with contextlib.suppress(Exception):
            await ws.close(code=1008, reason="invalid client_id")
        return

    # Resolve requested channels (default: global firehose) and join them.
    raw_channels = query.get("channels", "")
    requested = [c.strip() for c in raw_channels.split(",") if c.strip()] or [EVENTS_CHANNEL]
    subscribed = [
        channel
        for channel in requested
        if await manager.subscribe(connection_id, channel)
    ]

    logger.info(
        "ws_events.connected",
        connection_id=connection_id,
        client_id=client_id,
        channels=subscribed,
    )

    # Ack so the client (and tests) know subscriptions are live before
    # any event is expected to arrive.
    with contextlib.suppress(Exception):
        await ws.send_json(
            {
                "type": "connected",
                "connection_id": connection_id,
                "channels": subscribed,
            }
        )

    try:
        while True:
            try:
                msg = await ws.receive_json()
            except WebSocketDisconnect:
                break
            # Minimal client protocol: a ``ping`` is answered with ``pong``.
            # Anything else is ignored (no subscribe/unsubscribe over the
            # wire yet — channel selection happens via the query string).
            if isinstance(msg, dict) and msg.get("type") == "ping":
                with contextlib.suppress(Exception):
                    await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning(
            "ws_events.error",
            connection_id=connection_id,
            error_type=type(exc).__name__,
            error_message=str(exc)[:200],
        )
    finally:
        with contextlib.suppress(Exception):
            await manager.disconnect(connection_id)
        logger.info("ws_events.disconnected", connection_id=connection_id)


__all__ = [
    "EVENTS_CHANNEL",
    "WsEventsBridge",
    "event_to_channels",
    "get_state",
    "init_ws_events",
    "reset_ws_events_for_tests",
    "router",
    "ws_events_endpoint",
]
