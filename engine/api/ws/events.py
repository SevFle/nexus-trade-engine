"""Authenticated ``/ws/events`` streaming endpoint (SEV-275 follow-up).

This module provides a second WebSocket entrypoint — ``/ws/events`` —
that fans out :class:`EventBus` events to subscribed clients. Unlike the
generic ``/ws`` route in :mod:`engine.api.ws.router`, the events endpoint
authenticates the **session token up front, from a query param, before
``ws.accept()``**. A bad or missing token rejects the WebSocket handshake
at the HTTP layer so the server never upgrades an unauthenticated socket.

The subsystem is owned by a module-level :class:`_State` singleton that
holds the :class:`ConnectionManager`, :class:`ChannelResolver`, the
:class:`EventBusBridge` and — importantly — the running event loop
captured at init time (:attr:`_State.loop`). Wiring is done via
:func:`init_ws_events`, which is safe to call more than once: a re-init
disconnects every existing client and stops the previous bridge before
installing the new one.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from engine.api.auth.jwt import decode_token
from engine.api.ws.auth import (
    AmbiguousSubprotocolError,
    AuthResult,
    _extract_scopes,
    _extract_token_from_handshake,
    _offered_subprotocols,
    select_echo_subprotocol,
)
from engine.api.ws.metrics import ws_metrics
from engine.api.ws.protocol import (
    WS_CLOSE_AUTH_INVALID,
    WS_CLOSE_SERVER_ERROR,
    AckMessage,
    ErrorMessage,
    PongMessage,
    parse_inbound,
)

if TYPE_CHECKING:
    from engine.api.ws.channels import ChannelResolver
    from engine.api.ws.connection_manager import ConnectionManager
    from engine.api.ws.event_bridge import EventBusBridge

logger = structlog.get_logger()

router = APIRouter()

#: Query-param names that may carry the session token. ``token`` matches the
#: rest of the WS auth surface; ``session_token`` is accepted as an alias for
#: callers that name it explicitly.
_SESSION_TOKEN_PARAMS: tuple[str, ...] = ("token", "session_token")


class _State:
    """Module-level holder for the /ws/events subsystem singletons."""

    __slots__ = ("_reinit_tasks", "bridge", "loop", "manager", "resolver")

    def __init__(self) -> None:
        self.manager: ConnectionManager | None = None
        self.resolver: ChannelResolver | None = None
        self.bridge: EventBusBridge | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        #: Background tasks spawned to tear down a previous manager/bridge
        #: during a re-init. Tracked so callers/tests can await them.
        self._reinit_tasks: set[asyncio.Task[None]] = set()

    def track_reinit_task(self, task: asyncio.Task[None]) -> None:
        """Track a background re-init teardown task until it completes."""
        self._reinit_tasks.add(task)
        task.add_done_callback(self._reinit_tasks.discard)

    def clear_reinit_tasks(self) -> None:
        """Drop all tracked re-init teardown tasks (does not cancel them)."""
        self._reinit_tasks.clear()


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
    """Clear the subsystem singletons without touching live sockets.

    Intended for tests that want a clean slate. Does **not** run the
    graceful re-init path — it simply drops the references.
    """
    _state.manager = None
    _state.resolver = None
    _state.bridge = None
    _state.loop = None
    _state.clear_reinit_tasks()


def init_ws_events(
    manager: ConnectionManager,
    resolver: ChannelResolver | None = None,
    bridge: EventBusBridge | None = None,
    *,
    start_bridge: bool = True,
) -> None:
    """Initialize (or re-initialize) the ``/ws/events`` subsystem.

    Behaviour:

    - **Early loop capture.** The currently running event loop is captured
      into :attr:`_State.loop` *first*, before any other work, so any
      component that needs to schedule onto the loop has it available from
      the moment init returns — regardless of whether a client has
      connected yet.
    - **Clean re-init.** If the subsystem is already initialized, every
      existing client is disconnected (``manager.close_all``) and the
      previous bridge is stopped (``bridge.stop``) *before* the new
      manager/bridge are installed. This prevents leaked connections and
      double event-bus subscriptions on config reload.

    ``manager`` is required. ``resolver`` defaults to a new
    :class:`ChannelResolver` bound to ``manager``. ``bridge`` is optional;
    when provided and ``start_bridge`` is true it is started immediately.

    The async teardown (``close_all``) is scheduled on the captured loop —
    :func:`init_ws_events` itself stays synchronous so it can be called from
    the same place as :func:`engine.api.ws.router.init_ws`. The resulting
    background task is tracked on :attr:`_State._reinit_tasks`.
    """
    # 1) Capture the running loop immediately — before anything else.
    _state.loop = asyncio.get_running_loop()

    # 2) Clean re-init: tear down the previous subsystem first.
    if _state.manager is not None:
        _teardown_previous()

    # 3) Install the new subsystem.
    from engine.api.ws.channels import ChannelResolver  # noqa: PLC0415

    _state.manager = manager
    _state.resolver = resolver if resolver is not None else ChannelResolver(manager)
    _state.bridge = bridge
    if bridge is not None and start_bridge:
        bridge.start()
    logger.info(
        "ws_events.initialized",
        has_bridge=bridge is not None,
        loop_id=id(_state.loop),
    )


def _teardown_previous() -> None:
    """Disconnect all clients of the previous manager and stop its bridge.

    The bridge stop is synchronous; the manager teardown (``close_all``) is
    async and therefore scheduled on the captured loop. Both are best-effort
    so a failing teardown never blocks a re-init.
    """
    previous_manager = _state.manager
    previous_bridge = _state.bridge

    if previous_bridge is not None:
        with contextlib.suppress(Exception):
            previous_bridge.stop()

    if previous_manager is not None and _state.loop is not None and _state.loop.is_running():
        task = _state.loop.create_task(
            _graceful_close_all(previous_manager),
            name="ws_events_reinit_close_all",
        )
        _state.track_reinit_task(task)


async def _graceful_close_all(manager: ConnectionManager) -> None:
    """Best-effort ``close_all`` that never raises."""
    with contextlib.suppress(Exception):
        await manager.close_all(code=1001, reason="server reinitializing events stream")


# ---------------------------------------------------------------------------
# Session-token validation (mirrors the REST auth dependency)
# ---------------------------------------------------------------------------


def _read_session_token(ws: WebSocket) -> str | None:
    """Pull the session token from the handshake query params.

    Returns ``None`` when no recognized param is present. The lookup order
    keeps ``token`` primary for parity with the rest of the WS surface.
    """
    query = getattr(ws, "query_params", None) or {}
    for name in _SESSION_TOKEN_PARAMS:
        value = query.get(name)
        if isinstance(value, str) and value:
            return value
    return None


def _validate_session_token(ws: WebSocket) -> AuthResult | None:
    """Validate the session token the same way REST routes do.

    REST routes resolve a principal through
    :func:`engine.api.auth.dependency.get_current_user`, which ultimately
    trusts :func:`engine.api.auth.jwt.decode_token`. For the WS handshake we
    can't run the full HTTP dependency (it reads headers / a DB session), so
    we replicate the token-validation half: decode the JWT and require a
    ``sub`` claim, then derive scopes with the shared
    :func:`engine.api.ws.auth._extract_scopes`.

    The token is resolved from **two** channels, in order:

    1. The ``bearer.<token>`` WebSocket subprotocol handshake (preferred —
       it is not logged in access URLs). May raise
       :class:`AmbiguousSubprotocolError` when the client offers more than
       one bearer subprotocol; callers must reject the handshake.
    2. The ``token`` / ``session_token`` query param (legacy fallback).

    If both channels carry a token they must agree exactly; a mismatch is
    treated as an invalid handshake (returns ``None``).

    Returns an :class:`AuthResult` on success or ``None`` when the token is
    missing, malformed, expired or lacks a subject. Callers must reject the
    handshake *before* ``ws.accept()`` on a ``None`` result.
    """
    query_token = _read_session_token(ws)

    # Subprotocol handshake auth (preferred). Raises AmbiguousSubprotocolError
    # on an ambiguous multi-bearer handshake — propagated to the endpoint.
    handshake_token = _extract_token_from_handshake(_offered_subprotocols(ws))

    if handshake_token is not None:
        if query_token is not None and query_token != handshake_token:
            # Conflicting credentials across channels — refuse to guess.
            return None
        token = handshake_token
    else:
        token = query_token

    if token is None:
        return None

    token_data = decode_token(token)
    if token_data is None:
        return None

    sub = token_data.get("sub")
    if not isinstance(sub, str) or not sub:
        return None

    scopes = _extract_scopes(token_data)
    return AuthResult(user_id=sub, scopes=scopes, token_data=token_data)


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.websocket("/ws/events")
async def ws_events_endpoint(ws: WebSocket) -> None:
    """Authenticated streaming endpoint for engine events.

    The session token **must** be supplied as a query param on the
    handshake. It is validated before the socket is accepted; an invalid
    token rejects the upgrade with ``4401`` so the server never accepts an
    unauthenticated connection.
    """
    # 1) Pre-accept auth — reject bad/missing tokens at the HTTP layer.
    try:
        auth = _validate_session_token(ws)
    except AmbiguousSubprotocolError:
        ws_metrics.metrics.counter(
            "sev_ws_auth_failures_total", tags={"reason": "ambiguous_subprotocol"}
        )
        logger.warning("ws_events.ambiguous_subprotocol")
        with contextlib.suppress(Exception):
            await ws.close(
                code=WS_CLOSE_AUTH_INVALID, reason="ambiguous subprotocol handshake"
            )
        return

    if auth is None:
        ws_metrics.metrics.counter("sev_ws_auth_failures_total", tags={"reason": "events_invalid"})
        logger.warning("ws_events.auth_rejected")
        with contextlib.suppress(Exception):
            await ws.close(code=WS_CLOSE_AUTH_INVALID, reason="invalid session token")
        return

    # 2) Guard against the endpoint being hit before init.
    if _state.manager is None or _state.resolver is None:
        with contextlib.suppress(Exception):
            await ws.close(code=WS_CLOSE_SERVER_ERROR, reason="server not ready")
        return

    # Echo a constant, credential-free subprotocol — never the raw
    # ``bearer.<token>`` — so the token is not reflected in the response.
    await ws.accept(subprotocol=select_echo_subprotocol(ws))

    connection_id = await _state.manager.register(
        ws,
        user_id=auth.user_id,
        scopes=list(auth.scopes),
    )
    session = _Session(auth.user_id, list(auth.scopes))

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
                    ErrorMessage(
                        code="INVALID_MESSAGE",
                        message="expected JSON object",
                    ),
                )
                continue

            msg, parse_error = parse_inbound(raw)
            if msg is None:
                await _safe_send(
                    ws,
                    ErrorMessage(
                        code="PARSE_ERROR",
                        message=parse_error or "invalid message",
                    ),
                )
                continue

            await _dispatch_message(ws, msg, connection_id, session)

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning(
            "ws_events.unexpected_error",
            connection_id=connection_id[:8],
            error_type=type(exc).__name__,
            error_message=str(exc)[:200],
        )
    finally:
        await _state.manager.unregister(connection_id)


async def _dispatch_message(
    ws: WebSocket,
    msg,
    connection_id: str,
    session: _Session,
) -> None:
    if msg.type == "ping":
        await _safe_send(ws, PongMessage(ref=msg.ref))
        return

    if msg.type == "subscribe":
        result = await _state.resolver.handle_subscribe(
            connection_id, msg, session.user_id, session.scopes
        )
        await _safe_send(
            ws,
            AckMessage(
                ref=msg.ref,
                status="ok" if result.success else "error",
                error_code=result.error_code,
                message=result.message,
            ),
        )
        return

    if msg.type == "unsubscribe":
        result = await _state.resolver.handle_unsubscribe(
            connection_id, msg, session.user_id
        )
        await _safe_send(
            ws,
            AckMessage(
                ref=msg.ref,
                status="ok",
                message=result.message,
            ),
        )
        return


async def _safe_send(ws: WebSocket, msg) -> None:
    with contextlib.suppress(Exception):
        await ws.send_json(msg.model_dump(mode="json"))


__all__ = [
    "get_state",
    "init_ws_events",
    "reset_state",
    "router",
]
