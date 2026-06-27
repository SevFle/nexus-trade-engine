"""FastAPI WebSocket endpoint (SEV-275).

Connection flow:
1. Client opens WS /api/v1/ws
2. Server accepts and authenticates (JWT via query param or first message)
3. On success, registers with ConnectionManager and enters message loop.
4. Supported message types: ping, auth, subscribe, unsubscribe.
5. Server sends heartbeats and handles token refresh.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from engine.api.ws.auth import (
    AuthRateLimiter,
    authenticate_websocket,
    validate_refresh_token,
)
from engine.api.ws.channels import ChannelResolver
from engine.api.ws.metrics import ws_metrics
from engine.api.ws.protocol import (
    WS_CLOSE_TOKEN_EXPIRED,
    AckMessage,
    ErrorMessage,
    PongMessage,
    parse_inbound,
)

if TYPE_CHECKING:
    from engine.api.ws.connection_manager import ConnectionManager

logger = structlog.get_logger()

router = APIRouter()


class _State:
    __slots__ = ("manager", "rate_limiter", "resolver")

    def __init__(self) -> None:
        self.manager: ConnectionManager | None = None
        self.resolver: ChannelResolver | None = None
        self.rate_limiter: AuthRateLimiter | None = None


_state = _State()


class _Session:
    __slots__ = ("scopes", "user_id")

    def __init__(self, user_id: str, scopes: list[str]) -> None:
        self.user_id = user_id
        self.scopes = scopes


def init_ws(
    manager: ConnectionManager,
    rate_limiter: AuthRateLimiter | None = None,
) -> None:
    _state.manager = manager
    _state.resolver = ChannelResolver(manager)
    _state.rate_limiter = rate_limiter


def get_manager() -> ConnectionManager | None:
    return _state.manager


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    if _state.manager is None or _state.resolver is None:
        with contextlib.suppress(Exception):
            await ws.close(code=1011, reason="server not ready")
        return

    await ws.accept()

    auth_result = await authenticate_websocket(ws, rate_limiter=_state.rate_limiter)

    if isinstance(auth_result, tuple):
        code, reason = auth_result
        error_msg = ErrorMessage(code="AUTH_INVALID", message=reason)
        with contextlib.suppress(Exception):
            await ws.send_json(error_msg.model_dump(mode="json"))
        with contextlib.suppress(Exception):
            await ws.close(code=code, reason=reason)
        return

    connection_id = await _state.manager.register(
        ws,
        user_id=auth_result.user_id,
        scopes=list(auth_result.scopes),
    )
    session = _Session(auth_result.user_id, list(auth_result.scopes))

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
                tags={"type": raw.get("type", "unknown")},
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

            should_break = await _dispatch_message(ws, msg, connection_id, session)
            if should_break:
                break

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning(
            "ws.unexpected_error",
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
) -> bool:
    if msg.type == "ping":
        await _safe_send(ws, PongMessage(ref=msg.ref))
        return False

    if msg.type == "auth":
        refresh_result = validate_refresh_token(msg.token)
        if refresh_result is None:
            await _safe_send(
                ws,
                ErrorMessage(
                    code="AUTH_INVALID",
                    message="invalid refresh token",
                    ref=msg.ref,
                ),
            )
            await _close_ws(
                ws,
                code=WS_CLOSE_TOKEN_EXPIRED,
                reason="token expired",
            )
            return True

        session.user_id = refresh_result.user_id
        session.scopes = list(refresh_result.scopes)
        await _safe_send(
            ws,
            AckMessage(
                ref=msg.ref,
                status="ok",
                message="token refreshed",
            ),
        )
        return False

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
        return False

    if msg.type == "unsubscribe":
        result = await _state.resolver.handle_unsubscribe(connection_id, msg, session.user_id)
        await _safe_send(
            ws,
            AckMessage(
                ref=msg.ref,
                status="ok",
                message=result.message,
            ),
        )
        return False

    return False


async def _safe_send(ws: WebSocket, msg) -> None:
    with contextlib.suppress(Exception):
        await ws.send_json(msg.model_dump(mode="json"))


async def _close_ws(ws: WebSocket, *, code: int, reason: str) -> None:
    with contextlib.suppress(Exception):
        await ws.close(code=code, reason=reason)
