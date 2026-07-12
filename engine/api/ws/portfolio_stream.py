"""Authenticated ``/ws/portfolio`` streaming endpoint.

A focused, single-purpose WebSocket endpoint that streams JSON portfolio
snapshots to a connected client. Unlike the generic ``/ws/events`` route
(which fans out *every* engine event to room-based subscribers), this
endpoint is scoped to **portfolio-related events only**:

- ``portfolio.updated``  — aggregate portfolio state / PnL recalculation
- ``position.opened``    — a new position was opened
- ``position.closed``    — an existing position was closed
- ``order.filled``       — an order fill landed (affects positions + PnL)

Connection lifecycle:

1. The session JWT is validated from a query param **before** the socket
   is accepted (mirroring :mod:`engine.api.ws.events`). A bad or missing
   token rejects the handshake at the HTTP layer.
2. On success the connection is registered with the
   :class:`ConnectionManager` and a **per-connection subscriber** is
   registered on the :class:`EventBus` for the four portfolio event
   types above.
3. Each delivered event is serialized into a flat JSON snapshot (see
   :func:`build_snapshot`) and forwarded to that single client as an
   :class:`EventMessage`.
4. On disconnect (clean, error, or cancel) the per-connection subscriber
   is **unsubscribed** from the bus *before* the connection is
   unregistered, so no event is ever delivered to a dead socket and the
   bus dispatch loop is never left holding a stale callback.

The wiring is owned by a module-level :class:`_State` singleton
(``init_portfolio_stream`` / ``reset_state``) so tests get a clean slate.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from engine.api.auth.jwt import decode_token
from engine.api.ws.auth import AuthResult, extract_scopes
from engine.api.ws.metrics import ws_metrics
from engine.api.ws.protocol import (
    WS_CLOSE_AUTH_INVALID,
    WS_CLOSE_SERVER_ERROR,
    AckMessage,
    ErrorMessage,
    EventMessage,
    PongMessage,
    parse_inbound,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from engine.api.ws.connection_manager import ConnectionManager
    from engine.events.bus import EventBus, EventType

logger = structlog.get_logger()

router = APIRouter()

#: Query-param names that may carry the session token (parity with /ws/events).
_SESSION_TOKEN_PARAMS: tuple[str, ...] = ("token", "session_token")

#: Channel name stamped on every outbound :class:`EventMessage`.
PORTFOLIO_CHANNEL = "portfolio"

#: The EventBus event-type *values* this endpoint streams. Kept as plain
#: strings (the ``.value`` of each :class:`EventType` member) so callers
#: that only have the raw payload ``type`` can compare without importing
#: the enum. See :func:`portfolio_event_types` for the enum members.
PORTFOLIO_EVENT_TYPE_VALUES: tuple[str, ...] = (
    "portfolio.updated",
    "position.opened",
    "position.closed",
    "order.filled",
)


class _State:
    """Module-level holder for the /ws/portfolio subsystem singletons."""

    __slots__ = ("bus", "manager")

    def __init__(self) -> None:
        self.manager: ConnectionManager | None = None
        self.bus: EventBus | None = None


_state = _State()


class _Session:
    """Per-connection principal carried through the message loop."""

    __slots__ = ("scopes", "user_id")

    def __init__(self, user_id: str, scopes: list[str]) -> None:
        self.user_id = user_id
        self.scopes = scopes


def get_state() -> _State:
    """Expose the module state (mainly for tests / introspection)."""
    return _state


def reset_state() -> None:
    """Clear the subsystem singletons without touching live sockets."""
    _state.manager = None
    _state.bus = None


def init_portfolio_stream(manager: ConnectionManager, bus: EventBus) -> None:
    """Install the :class:`ConnectionManager` and :class:`EventBus`.

    Intended to be called once during app startup. Safe to call again to
    re-point the endpoint at a different manager/bus (existing live
    sockets keep their own per-connection subscribers until they
    disconnect).
    """
    _state.manager = manager
    _state.bus = bus
    logger.info("ws_portfolio.initialized")


def portfolio_event_types() -> list[EventType]:
    """Resolve the portfolio-related :class:`EventType` members.

    Imported lazily so this module does not hard-depend on the events
    package at import time (keeping the auth/protocol surface usable in
    isolation by tests).
    """
    from engine.events.bus import EventType  # noqa: PLC0415

    return [
        EventType.PORTFOLIO_UPDATED,
        EventType.POSITION_OPENED,
        EventType.POSITION_CLOSED,
        EventType.ORDER_FILLED,
    ]


# ---------------------------------------------------------------------------
# Auth — validated before ws.accept(), identical contract to /ws/events
# ---------------------------------------------------------------------------


def _read_session_token(ws: WebSocket) -> str | None:
    """Pull the session token from the handshake query params.

    Returns ``None`` when no recognized param is present. ``token`` is the
    primary name (parity with the rest of the WS surface);
    ``session_token`` is accepted as an alias.
    """
    query = getattr(ws, "query_params", None) or {}
    for name in _SESSION_TOKEN_PARAMS:
        value = query.get(name)
        if isinstance(value, str) and value:
            return value
    return None


def _validate_session_token(ws: WebSocket) -> AuthResult | None:
    """Validate the session token the same way REST routes do.

    Decodes the JWT, requires a ``sub`` claim, and derives scopes with the
    shared :func:`extract_scopes` helper. Returns ``None`` for a missing,
    malformed, expired or subject-less token — callers must reject the
    handshake *before* ``ws.accept()`` on a ``None`` result.
    """
    token = _read_session_token(ws)
    if token is None:
        return None

    token_data = decode_token(token)
    if token_data is None:
        return None

    sub = token_data.get("sub")
    if not isinstance(sub, str) or not sub:
        return None

    scopes = extract_scopes(token_data)
    return AuthResult(user_id=sub, scopes=scopes, token_data=token_data)


# ---------------------------------------------------------------------------
# Event → snapshot serialization
# ---------------------------------------------------------------------------


def build_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    """Flatten an EventBus payload into a JSON-serializable snapshot.

    The EventBus delivers envelopes of the shape::

        {"type": "position.opened",
         "data": {...},
         "source": "engine",
         "timestamp": "2025-..."}

    A portfolio client cares about *what changed* (``event``), *the new
    state* (``data``) and *when* (``timestamp``). This helper normalizes
    the envelope, always returning a dict with a ``dict`` ``data`` field
    so downstream serialization never trips on a non-dict payload.
    """
    raw_data = payload.get("data")
    return {
        "event": payload.get("type"),
        "data": raw_data if isinstance(raw_data, dict) else {},
        "source": payload.get("source"),
        "timestamp": payload.get("timestamp"),
    }


def make_portfolio_handler(
    manager: ConnectionManager,
    connection_id: str,
) -> Callable[[dict[str, Any]], Awaitable[None]]:
    """Build a per-connection EventBus handler.

    The returned coroutine serializes each delivered payload via
    :func:`build_snapshot`, wraps it in an :class:`EventMessage` and
    enqueues it to exactly one connection through ``manager.send``.

    Send failures (closed/evicted socket, full queue) are swallowed and
    counted so a single bad client never aborts the bus dispatch loop —
    the connection will be reaped on its next disconnect.
    """

    async def _handler(payload: dict[str, Any]) -> None:
        snapshot = build_snapshot(payload)
        event_type = payload.get("type")
        try:
            msg = EventMessage(
                channel=PORTFOLIO_CHANNEL,
                room=PORTFOLIO_CHANNEL,
                payload=snapshot,
                seq=manager.next_seq(PORTFOLIO_CHANNEL),
            )
            await manager.send(connection_id, msg)
        except Exception as exc:
            ws_metrics.metrics.counter(
                "sev_ws_messages_dropped_total", tags={"reason": "send_error"}
            )
            logger.warning(
                "ws_portfolio.send_failed",
                connection_id=connection_id[:8],
                event_type=event_type,
                error_type=type(exc).__name__,
                error=str(exc)[:200],
            )

    # Stamp a readable name for debuggability (bus logs handler.__name__).
    _handler.__name__ = f"portfolio_handler_{connection_id[:8]}"
    return _handler


def register_subscriptions(
    bus: EventBus,
    handler: Callable[[dict[str, Any]], Awaitable[None]],
    event_types: list[EventType] | None = None,
) -> list[EventType]:
    """Subscribe ``handler`` to the portfolio event types on ``bus``.

    Returns the list of event types actually subscribed, in subscription
    order, so the caller can hand it to :func:`unregister_subscriptions`
    for symmetric cleanup.
    """
    types = event_types if event_types is not None else portfolio_event_types()
    for et in types:
        bus.subscribe(et, handler)
    logger.debug(
        "ws_portfolio.subscribed",
        handler=getattr(handler, "__name__", "handler"),
        event_types=[getattr(et, "value", str(et)) for et in types],
    )
    return list(types)


def unregister_subscriptions(
    bus: EventBus,
    handler: Callable[[dict[str, Any]], Awaitable[None]],
    event_types: list[EventType],
) -> None:
    """Best-effort unsubscribe of ``handler`` from ``event_types``.

    Each unsubscribe is wrapped so one failing entry never blocks cleanup
    of the remaining event types.
    """
    for et in event_types:
        with contextlib.suppress(Exception):
            bus.unsubscribe(et, handler)
    logger.debug(
        "ws_portfolio.unsubscribed",
        handler=getattr(handler, "__name__", "handler"),
        event_types=[getattr(et, "value", str(et)) for et in event_types],
    )


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.websocket("/ws/portfolio")
async def ws_portfolio_endpoint(ws: WebSocket) -> None:
    """Authenticated streaming endpoint for portfolio snapshots.

    The session token **must** be supplied as a query param on the
    handshake. It is validated before the socket is accepted; an invalid
    token rejects the upgrade with ``4401`` so the server never accepts
    an unauthenticated connection.
    """
    # 1) Pre-accept auth gate — reject bad/missing tokens at the HTTP layer.
    auth = _validate_session_token(ws)
    if auth is None:
        ws_metrics.metrics.counter(
            "sev_ws_auth_failures_total", tags={"reason": "portfolio_invalid"}
        )
        logger.warning("ws_portfolio.auth_rejected")
        with contextlib.suppress(Exception):
            await ws.close(code=WS_CLOSE_AUTH_INVALID, reason="invalid session token")
        return

    # 2) Guard against the endpoint being hit before init.
    if _state.manager is None or _state.bus is None:
        with contextlib.suppress(Exception):
            await ws.close(code=WS_CLOSE_SERVER_ERROR, reason="server not ready")
        return

    await ws.accept()

    connection_id = await _state.manager.register(
        ws,
        user_id=auth.user_id,
        scopes=list(auth.scopes),
    )
    session = _Session(auth.user_id, list(auth.scopes))

    # 3) Register a per-connection subscriber on the EventBus.
    handler = make_portfolio_handler(_state.manager, connection_id)
    subscribed_types = register_subscriptions(_state.bus, handler)

    ack = AckMessage(status="ok", message="connected")
    with contextlib.suppress(Exception):
        await ws.send_json(ack.model_dump(mode="json"))

    try:
        while True:
            try:
                raw = await ws.receive_json()
            except WebSocketDisconnect:
                break

            ws_metrics.metrics.counter(
                "sev_ws_messages_received_total",
                tags={"type": raw.get("type", "unknown") if isinstance(raw, dict) else "unknown"},
            )
            _state.manager.touch(connection_id)

            if not isinstance(raw, dict):
                await _safe_send(
                    ws,
                    ErrorMessage(code="INVALID_MESSAGE", message="expected JSON object"),
                )
                continue

            msg, parse_error = parse_inbound(raw)
            if msg is None:
                await _safe_send(
                    ws,
                    ErrorMessage(code="PARSE_ERROR", message=parse_error or "invalid message"),
                )
                continue

            # /ws/portfolio is read-only from the client's perspective; the
            # only inbound message it acts on is a keepalive ping.
            if msg.type == "ping":
                await _safe_send(ws, PongMessage(ref=msg.ref))

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning(
            "ws_portfolio.unexpected_error",
            connection_id=connection_id[:8],
            error_type=type(exc).__name__,
            error_message=str(exc)[:200],
        )
    finally:
        # 4) Cleanup: unsubscribe FIRST so the bus stops delivering to this
        # connection before we tear down its send queue / sender task.
        with contextlib.suppress(Exception):
            unregister_subscriptions(_state.bus, handler, subscribed_types)
        await _state.manager.unregister(connection_id)
    # ``session`` is retained for future per-message auth refresh parity with
    # /ws; not currently mutated by this read-only endpoint.
    _ = session


async def _safe_send(ws: WebSocket, msg) -> None:
    with contextlib.suppress(Exception):
        await ws.send_json(msg.model_dump(mode="json"))


__all__ = [
    "PORTFOLIO_CHANNEL",
    "PORTFOLIO_EVENT_TYPE_VALUES",
    "build_snapshot",
    "get_state",
    "init_portfolio_stream",
    "make_portfolio_handler",
    "portfolio_event_types",
    "register_subscriptions",
    "reset_state",
    "router",
    "unregister_subscriptions",
    "ws_portfolio_endpoint",
]
