"""WebSocket route — auth-then-subscribe protocol (gh#7).

Connection flow
---------------
1. Client opens ``WS /api/v1/ws``. Server accepts.
2. Client must send ``{"type": "auth", "token": "<JWT or nxs_*>"}``
   within ``AUTH_TIMEOUT_SECONDS``. Server validates.
3. On success, server replies ``{"type": "auth.ok", "user_id": ...}``
   and the connection is attached to the manager.
4. Client subscribes via ``{"type": "subscribe", "topics": [...]}``
   and unsubscribes with ``{"type": "unsubscribe", "topics": [...]}``.
5. ``{"type": "ping"}`` from either side keeps the connection warm
   (the server replies with ``{"type": "pong"}``).

JWT in the URL is intentionally **not** supported — query strings end
up in proxy logs.
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
from engine.api.websocket.manager import VALID_TOPICS, get_manager
from engine.db.models import User
from engine.db.session import get_session_factory

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()

router = APIRouter()

AUTH_TIMEOUT_SECONDS = 10.0


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    manager = get_manager()

    # 1. Auth
    user = await _authenticate(ws)
    if user is None:
        return  # _authenticate already closed the socket

    await manager.attach(user.id, ws)
    try:
        await _send(ws, {"type": "auth.ok", "user_id": str(user.id)})

        # 2. Message loop
        while True:
            msg = await ws.receive_json()
            mtype = msg.get("type") if isinstance(msg, dict) else None

            if mtype == "subscribe":
                topics = _coerce_topic_list(msg.get("topics"))
                resulting = await manager.subscribe(user.id, ws, topics)
                await _send(
                    ws,
                    {"type": "subscribed", "topics": sorted(resulting)},
                )

            elif mtype == "unsubscribe":
                topics = _coerce_topic_list(msg.get("topics"))
                resulting = await manager.unsubscribe(user.id, ws, topics)
                await _send(
                    ws,
                    {"type": "unsubscribed", "topics": sorted(resulting)},
                )

            elif mtype == "ping":
                await _send(ws, {"type": "pong"})

            else:
                await _send(
                    ws,
                    {
                        "type": "error",
                        "code": "unknown_message_type",
                        "detail": str(mtype),
                    },
                )

    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001 - last-line defence
        logger.warning(
            "ws.unexpected_error",
            error_type=type(exc).__name__,
            error_message=str(exc)[:200],
        )
    finally:
        await manager.detach(user.id, ws)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _authenticate(ws: WebSocket) -> User | None:
    try:
        msg = await asyncio.wait_for(
            ws.receive_json(), timeout=AUTH_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        await _close(ws, code=4401, reason="auth_timeout")
        return None
    except WebSocketDisconnect:
        return None

    if not isinstance(msg, dict) or msg.get("type") != "auth":
        await _close(ws, code=4400, reason="auth_required")
        return None
    token = msg.get("token")
    if not isinstance(token, str) or not token:
        await _close(ws, code=4400, reason="auth_token_missing")
        return None

    user = await _user_for_token(token)
    if user is None:
        await _close(ws, code=4401, reason="auth_invalid")
        return None

    return user


async def _user_for_token(token: str) -> User | None:
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


def _coerce_topic_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str) and v in VALID_TOPICS]


async def _send(ws: WebSocket, payload: dict[str, Any]) -> None:
    with contextlib.suppress(Exception):
        await ws.send_json(payload)


async def _close(ws: WebSocket, *, code: int, reason: str) -> None:
    with contextlib.suppress(Exception):
        await ws.close(code=code, reason=reason)
