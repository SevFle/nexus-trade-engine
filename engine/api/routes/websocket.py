"""WebSocket route — auth-then-subscribe protocol (gh#7 + SEV-275).

Connection flow
---------------
1. Client opens ``WS /api/v1/ws``. The server pulls a JWT token from
   one of three places, in priority order:

   a. ``?token=`` query string (SEV-275). Convenient for browser
      clients that can't set headers on the WebSocket handshake. Note
      that query strings appear in proxy access logs — prefer
      subprotocol or first-frame auth in production.
   b. ``Sec-WebSocket-Protocol`` header. The token is the *first*
      subprotocol value (e.g. ``Sec-WebSocket-Protocol: bearer.<JWT>,
      v10.websocket.base``); the server accepts the handshake with
      that subprotocol echoed back. This is the recommended browser
      pattern when the SPA controls the WebSocket constructor.
   c. First ``{"type": "auth", "token": "<JWT or nxs_*>"}`` frame,
      within ``AUTH_TIMEOUT_SECONDS``. Pre-SEV-275 default; still the
      most secure option because the token never touches the URL.

2. On success, the server sends a ``connection.ready`` frame followed
   by ``auth.ok``. The connection is now attached to the manager.

3. Client sends typed control frames:
   - ``{"type": "subscribe", "channels": [...]}`` — adds subs.
   - ``{"type": "unsubscribe", "channels": [...]}`` — drops subs.
   - ``{"type": "ping", "correlation_id": "..."}`` — heartbeat.
   - ``{"type": "ack", "seq": N}`` — flow-control ack (logged only).

4. Server emits a ``WSMessage`` envelope for every event delivered
   through the connection: ``{event, channel, ts, seq, correlation_id,
   version, data}``.

5. Server-initiated heartbeat: every ``DEFAULT_HEARTBEAT_SECONDS`` of
   client inactivity a ``{"type": "ping"}`` frame is pushed; the
   client is expected to reply with ``{"type": "pong"}`` (or any
   frame, which also counts as activity). A connection silent for
   ``3 * heartbeat`` is forcibly closed.

6. Graceful shutdown: server-side ``WebSocket.close(1001)`` runs in
   the lifespan teardown path; per-connection disconnect handling
   cancels the subscriber task, drops subscriptions, and detaches
   from the manager.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from engine.api.auth.api_keys import find_active_by_token, is_engine_token
from engine.api.auth.jwt import decode_token
from engine.api.websocket.constants import (
    AUTH_TIMEOUT_SECONDS,
    DEFAULT_HEARTBEAT_SECONDS,
    VALID_CHANNELS,
    WS_CLOSE_AUTH_TIMEOUT,
    WS_CLOSE_BAD_REQUEST,
    WS_CLOSE_GOING_AWAY,
    WS_CLOSE_UNAUTHENTICATED,
)
from engine.api.websocket.manager import get_manager
from engine.api.websocket.schemas import (
    AuthOkFrame,
    ConnectionReadyFrame,
    ErrorFrame,
    PongFrame,
    SubscribedFrame,
    UnsubscribedFrame,
    parse_client_frame,
)
from engine.db.models import User
from engine.db.session import get_session_factory

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()

router = APIRouter()

# Subprotocol prefix the server recognizes. The full subprotocol value
# is ``bearer.<token>`` so nginx/envoy's allow-list can be configured
# to permit only this prefix instead of an arbitrary string.
SUBPROTO_BEARER_PREFIX = "bearer."


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    """Main multiplexed WebSocket endpoint (SEV-275)."""

    # ----- Pre-accept auth: token in query or subprotocol header -----
    query_token = _extract_query_token(ws)
    subproto_token = _extract_subprotocol_token(ws)
    header_token = query_token or subproto_token

    # Accept the handshake; if a subprotocol was offered, echo it back.
    selected_subproto: str | None = None
    if subproto_token is not None:
        offered = ws.headers.get("sec-websocket-protocol", "")
        # Accept the *first* matching subprotocol in client-preference order.
        for candidate in (s.strip() for s in offered.split(",")):
            if candidate.startswith(SUBPROTO_BEARER_PREFIX):
                selected_subproto = candidate
                break
    await ws.accept(subprotocol=selected_subproto)

    manager = get_manager()

    # ----- Auth -----
    user: User | None
    if header_token is not None:
        user = await _user_for_token(header_token)
        if user is None:
            await _close(ws, code=WS_CLOSE_UNAUTHENTICATED, reason="auth_invalid")
            return
    else:
        # Fall back to first-frame auth with a timeout.
        user = await _authenticate_first_frame(ws)
        if user is None:
            return  # closed already

    await manager.attach(user.id, ws)
    try:
        # ----- Send connection.ready + auth.ok -----
        await _send_model(
            ws,
            ConnectionReadyFrame(
                user_id=str(user.id),
                heartbeat_seconds=DEFAULT_HEARTBEAT_SECONDS,
            ),
        )
        await _send_model(ws, AuthOkFrame(user_id=str(user.id)))

        # ----- Message loop -----
        await _message_loop(ws, user)

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning(
            "ws.unexpected_error",
            error_type=type(exc).__name__,
            error_message=str(exc)[:200],
        )
    finally:
        await manager.detach(user.id, ws)


# ---------------------------------------------------------------------------
# Internals — auth
# ---------------------------------------------------------------------------


def _extract_query_token(ws: WebSocket) -> str | None:
    """Pull ``?token=...`` from the upgrade request.

    Returns ``None`` if absent or empty. Validation happens downstream
    via :func:`_user_for_token`.
    """
    raw = ws.query_params.get("token")
    if not isinstance(raw, str) or not raw:
        return None
    return raw.strip() or None


def _extract_subprotocol_token(ws: WebSocket) -> str | None:
    """Pull ``bearer.<token>`` from the Sec-WebSocket-Protocol header."""
    offered = ws.headers.get("sec-websocket-protocol")
    if not offered:
        return None
    for candidate in (s.strip() for s in offered.split(",")):
        if candidate.lower().startswith(SUBPROTO_BEARER_PREFIX):
            token = candidate[len(SUBPROTO_BEARER_PREFIX) :].strip()
            return token or None
    return None


async def _authenticate_first_frame(ws: WebSocket) -> User | None:  # noqa: PLR0911
    """Pre-SEV-275 first-frame auth path.

    Used when neither query string nor subprotocol carried a token.
    The client must send ``{"type": "auth", "token": "..."}`` within
    :data:`AUTH_TIMEOUT_SECONDS`.
    """
    try:
        msg = await asyncio.wait_for(
            ws.receive_json(), timeout=AUTH_TIMEOUT_SECONDS
        )
    except TimeoutError:
        await _close(ws, code=WS_CLOSE_AUTH_TIMEOUT, reason="auth_timeout")
        return None
    except WebSocketDisconnect:
        return None
    except Exception:
        # Malformed JSON / wrong wire format.
        await _close(ws, code=WS_CLOSE_BAD_REQUEST, reason="auth_required")
        return None

    if not isinstance(msg, dict) or msg.get("type") != "auth":
        await _close(ws, code=WS_CLOSE_BAD_REQUEST, reason="auth_required")
        return None
    token = msg.get("token")
    if not isinstance(token, str) or not token:
        await _close(ws, code=WS_CLOSE_BAD_REQUEST, reason="auth_token_missing")
        return None

    user = await _user_for_token(token)
    if user is None:
        await _close(ws, code=WS_CLOSE_UNAUTHENTICATED, reason="auth_invalid")
        return None

    return user


async def _user_for_token(token: str) -> User | None:
    """Resolve a JWT or nexus engine API key to an active :class:`User`."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        if is_engine_token(token):
            return await _user_for_api_key(session, token)
        return await _user_for_jwt(session, token)


async def _user_for_jwt(session: AsyncSession, token: str) -> User | None:
    payload = decode_token(token)
    if payload is None:
        return None
    sub = payload.get("sub")
    if not sub:
        return None
    try:
        user_uuid = uuid.UUID(sub)
    except (ValueError, AttributeError):
        return None
    return await _load_active_user(session, user_uuid)


async def _user_for_api_key(session: AsyncSession, token: str) -> User | None:
    row = await find_active_by_token(session, token)
    if row is None:
        return None
    return await _load_active_user(session, row.user_id)


async def _load_active_user(session: AsyncSession, user_uuid: uuid.UUID) -> User | None:
    result = await session.execute(select(User).where(User.id == user_uuid))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        return None
    return user


# ---------------------------------------------------------------------------
# Internals — message loop
# ---------------------------------------------------------------------------


async def _message_loop(ws: WebSocket, user: User) -> None:
    """Dispatch inbound frames until the client disconnects."""
    manager = get_manager()
    # Heartbeat task — server-initiated ping if no traffic for one interval.
    heartbeat_task = asyncio.create_task(
        _server_heartbeat(ws, user), name="ws-heartbeat"
    )
    try:
        while True:
            raw = await ws.receive_json()
            manager.touch(user.id, ws)
            try:
                frame = parse_client_frame(raw)
            except Exception as exc:
                await _send_model(
                    ws,
                    ErrorFrame(
                        code="invalid_frame",
                        detail=str(exc)[:200],
                        correlation_id=(
                            raw.get("correlation_id")
                            if isinstance(raw, dict)
                            else None
                        ),
                    ),
                )
                continue

            if frame is None:
                await _send_model(
                    ws,
                    ErrorFrame(
                        code="unknown_message_type",
                        detail=str(raw.get("type") if isinstance(raw, dict) else None),
                    ),
                )
                continue

            match frame.type:
                case "subscribe":
                    valid_channels = [c for c in frame.channels if c in VALID_CHANNELS]
                    resulting = await manager.subscribe(
                        user.id, ws, valid_channels
                    )
                    await _send_model(
                        ws,
                        SubscribedFrame(
                            channels=sorted(resulting),
                            correlation_id=frame.correlation_id,
                        ),
                    )

                case "unsubscribe":
                    resulting = await manager.unsubscribe(
                        user.id, ws, frame.channels
                    )
                    await _send_model(
                        ws,
                        UnsubscribedFrame(
                            channels=sorted(resulting),
                            correlation_id=frame.correlation_id,
                        ),
                    )

                case "ping":
                    await _send_model(
                        ws,
                        PongFrame(correlation_id=frame.correlation_id),
                    )

                case "ack":
                    # Logged for observability; no action.
                    logger.debug(
                        "ws.client_ack",
                        user_id=str(user.id),
                        seq=frame.seq,
                        correlation_id=frame.correlation_id,
                    )
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(BaseException):
            await heartbeat_task


async def _server_heartbeat(ws: WebSocket, user: User) -> None:
    """Push a server-initiated ping if the client has been silent.

    Implementation note: we don't actually need a separate pong from
    the client because ``manager.touch`` is called on every inbound
    frame. If a full heartbeat interval elapses with no inbound frame,
    we send a ``{"type": "ping"}`` and let the next inbound frame
    confirm liveness. Three missed intervals trigger a close.
    """
    manager = get_manager()
    missed = 0
    while True:
        await asyncio.sleep(DEFAULT_HEARTBEAT_SECONDS)
        state_dict = manager._conns.get(user.id)  # noqa: SLF001
        if state_dict is None:
            return
        state = state_dict.get(ws)
        if state is None:
            return
        idle_for = __import__("time").monotonic() - state.last_seen
        if idle_for < DEFAULT_HEARTBEAT_SECONDS:
            missed = 0
            continue
        # Idle — poke the client.
        with contextlib.suppress(Exception):
            await ws.send_json({"type": "ping"})
        missed += 1
        if missed >= 3:
            logger.info(
                "ws.heartbeat_timeout",
                user_id=str(user.id),
                missed=missed,
            )
            with contextlib.suppress(Exception):
                await ws.close(
                    code=WS_CLOSE_GOING_AWAY, reason="heartbeat_timeout"
                )
            return

# ---------------------------------------------------------------------------
# Internals — small send helpers
# ---------------------------------------------------------------------------


async def _send_model(ws: WebSocket, model: Any) -> None:
    """Serialize a Pydantic model and send it; swallow send errors.

    Send errors here are typical of a client that just went away; the
    main loop's ``WebSocketDisconnect`` handler will clean up.
    """
    with contextlib.suppress(Exception):
        await ws.send_json(model.model_dump(mode="json"))


async def _close(ws: WebSocket, *, code: int, reason: str) -> None:
    with contextlib.suppress(Exception):
        await ws.close(code=code, reason=reason)


# ---------------------------------------------------------------------------
# Backwards-compat helpers (preserved for older callers / tests)
# ---------------------------------------------------------------------------


def _coerce_topic_list(value: Any) -> list[str]:
    """Pre-SEV-275 helper kept for backwards compatibility.

    New code uses :class:`engine.api.websocket.schemas.SubscribeFrame`
    and lets Pydantic reject malformed input. This helper still exists
    so older callers (and existing test fixtures) keep working.
    """
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str) and v in VALID_CHANNELS]
