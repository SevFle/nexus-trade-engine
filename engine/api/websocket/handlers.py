"""WebSocket endpoint handlers (SEV-275).

Implements the four endpoints specified in the plan:

- ``/ws``              — unified multiplexed stream
- ``/ws/portfolio``    — pre-bound to the portfolio family
- ``/ws/orders``       — pre-bound to the orders family
- ``/ws/market``       — pre-bound to the market data families

All four share the same protocol — they only differ in the set of
allowed subscribe channels. The handler runs:

1. Accept the upgrade.
2. Authenticate (JWT / API key). On failure emit an ``auth.failed``
   frame and close with the appropriate code.
3. Register with the ConnectionManager and start the sender task.
4. Run the receive loop:
   - ``subscribe``   → registry.add; ack frame.
   - ``unsubscribe`` → registry.remove; ack frame.
   - ``ping``        → pong frame.
   - ``anything else`` → ``error`` frame with code=unknown_message_type.
5. On disconnect, the manager tears the connection down.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import structlog
from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from engine.api.websocket import ws_metrics as mx
from engine.api.websocket.auth import authenticate, authorize_channel
from engine.api.websocket.channels import (
    Channel,
    for_market,
    for_market_depth,
    for_orders,
    for_portfolio,
)
from engine.api.websocket.connection_manager_v2 import (
    ConnectionManagerV2,
    _Connection,
)
from engine.api.websocket.constants import (
    AUTH_TIMEOUT_SECONDS,
    WS_PROTOCOL_VERSION,
    CloseCode,
)
from engine.api.websocket.exceptions import (
    ForbiddenError,
    MalformedFrameError,
    SubscriptionLimitError,
    WebSocketError,
)
from engine.api.websocket.logging import bind_logger, fresh_correlation_id
from engine.api.websocket.models import Principal
from engine.api.websocket.rate_limit import OutboundRateLimiter
from engine.api.websocket.schemas import (
    AuthFailedFrame,
    PingFrame,
    SubscribeFrame,
    UnsubscribeFrame,
)

logger = structlog.get_logger()


# Per-family allowed_channels. The four endpoints share the handler —
# the only difference is which subset of the union they accept.
_ALLOWED_FAMILIES = ("portfolio", "orders", "market", "market_depth")


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------
async def serve_unified(ws: WebSocket, manager: ConnectionManagerV2) -> None:
    """``/ws`` — multiplexed stream. Clients subscribe to any family."""
    await _serve(ws, manager, allowed_families=set(_ALLOWED_FAMILIES))


async def serve_portfolio(ws: WebSocket, manager: ConnectionManagerV2) -> None:
    """``/ws/portfolio`` — auto-subscribes the user to its portfolio channel."""
    await _serve(
        ws, manager, allowed_families={"portfolio"}, auto_subscribe_user=True
    )


async def serve_orders(ws: WebSocket, manager: ConnectionManagerV2) -> None:
    """``/ws/orders`` — auto-subscribes the user to its orders channel."""
    await _serve(
        ws, manager, allowed_families={"orders"}, auto_subscribe_user=True
    )


async def serve_market(ws: WebSocket, manager: ConnectionManagerV2) -> None:
    """``/ws/market`` — accepts market + market_depth subscriptions only."""
    await _serve(ws, manager, allowed_families={"market", "market_depth"})


# ---------------------------------------------------------------------------
# Shared handler
# ---------------------------------------------------------------------------
async def _serve(
    ws: WebSocket,
    manager: ConnectionManagerV2,
    *,
    allowed_families: set[str],
    auto_subscribe_user: bool = False,
) -> None:
    # Negotiate subprotocol if the client sent nexus.<token>. The
    # accept call must echo one of the offered subprotocols or the
    # browser's WebSocket constructor will fail the upgrade before we
    # ever see it.
    subprotocols: list[str] = []
    offered = ws.scope.get("subprotocols") if hasattr(ws, "scope") else None
    if isinstance(offered, list):
        subprotocols = [p for p in offered if isinstance(p, str) and p.startswith("nexus.")]
    await ws.accept(subprotocol=subprotocols[0] if subprotocols else None)

    # 1. Auth
    log = bind_logger(connection_id=fresh_correlation_id())
    try:
        principal = await asyncio.wait_for(_do_auth(ws), timeout=AUTH_TIMEOUT_SECONDS)
    except TimeoutError:
        await _close(ws, CloseCode.AUTH_TIMEOUT, "auth_timeout")
        mx.auth_failure(reason="timeout")
        return
    except WebSocketError as exc:
        await _emit(ws, AuthFailedFrame(reason=_auth_reason(exc)).model_dump())  # type: ignore[arg-type]
        await _close(ws, exc.code, exc.reason)
        mx.auth_failure(reason=exc.reason)
        return
    except WebSocketDisconnect:
        return
    except Exception as exc:
        log.exception("ws_v2.handshake_error", error_type=type(exc).__name__)
        await _close(ws, CloseCode.INTERNAL_ERROR, "internal_error")
        return

    log = bind_logger(principal, connection_id=fresh_correlation_id())
    log.info("ws_v2.connected", user_id=str(principal.user_id))

    # 2. Register + start sender
    try:
        conn = await manager.register(ws, principal)
    except Exception as exc:
        log.warning("ws_v2.register_failed", error=str(exc))
        await _close(ws, CloseCode.GOING_AWAY, "shutdown")
        return

    rate_limiter = OutboundRateLimiter()
    await manager.spawn_sender(conn)

    # 3. Auto-subscribe family endpoint clients to their user channel.
    if auto_subscribe_user:
        for family in allowed_families & {"portfolio", "orders"}:
            channel = (
                for_portfolio(principal.user_id)
                if family == "portfolio"
                else for_orders(principal.user_id)
            )
            with contextlib.suppress(Exception):
                await _do_subscribe(conn, manager, channel, principal)

    # 4. Auth-ok ack
    await _emit(
        ws,
        {
            "type": "auth.ok",
            "v": WS_PROTOCOL_VERSION,
            "user_id": str(principal.user_id),
            "scopes": sorted(principal.scopes),
        },
    )

    # 5. Receive loop
    try:
        while True:
            try:
                raw = await ws.receive_json()
            except WebSocketDisconnect:
                break
            except (json.JSONDecodeError, ValueError):
                await _emit(
                    ws,
                    {
                        "type": "error",
                        "code": "malformed",
                        "detail": "invalid_json",
                        "recoverable": True,
                    },
                )
                continue

            try:
                await _handle_client_frame(
                    raw, conn, manager, principal, allowed_families, rate_limiter
                )
            except SubscriptionLimitError as exc:
                await _emit(
                    ws,
                    {
                        "type": "error",
                        "code": "too_many_subscriptions",
                        "detail": exc.reason,
                        "recoverable": False,
                    },
                )
            except WebSocketError as exc:
                await _emit(
                    ws,
                    {
                        "type": "error",
                        "code": _error_code(exc),
                        "detail": exc.reason,
                        "recoverable": False,
                    },
                )
                if exc.code != CloseCode.MALFORMED:
                    await _close(ws, exc.code, exc.reason)
                    break
            except Exception:
                log.exception("ws_v2.handler_error")
                await _emit(
                    ws,
                    {
                        "type": "error",
                        "code": "server_error",
                        "detail": "internal",
                        "recoverable": True,
                    },
                )
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("ws_v2.unexpected_error")
    finally:
        await manager.disconnect(conn, code=CloseCode.GOING_AWAY)
        await rate_limiter.reset(str(principal.user_id))
        log.info("ws_v2.closed", user_id=str(principal.user_id))


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------
async def _do_auth(ws: WebSocket) -> Principal:
    """Extract + validate credentials. Supports both
    ``Sec-WebSocket-Protocol`` / query-string / Authorization header
    tokens *and* the post-accept ``{"type":"auth", "token": ...}``
    frame used by the legacy endpoint. We try the frame first if
    none of the upgrade-time channels yielded a token — that keeps
    browser clients happy without breaking the subprotocol fast path.
    """
    principal = await authenticate(ws)
    return principal


def _auth_reason(exc: WebSocketError) -> str:
    """Map an exception to an :class:`AuthFailedFrame` reason literal."""
    from engine.api.websocket.exceptions import (
        AuthRequiredError,
        AuthTimeoutError,
        ForbiddenError,
        InactiveUserError,
        InvalidTokenError,
    )

    if isinstance(exc, AuthTimeoutError):
        return "timeout"
    if isinstance(exc, AuthRequiredError):
        return "missing_token"
    if isinstance(exc, InvalidTokenError):
        return "invalid_token"
    if isinstance(exc, InactiveUserError):
        return "inactive_user"
    if isinstance(exc, ForbiddenError):
        return "forbidden"
    return "invalid_token"


# ---------------------------------------------------------------------------
# Frame dispatch
# ---------------------------------------------------------------------------
async def _handle_client_frame(
    raw: Any,
    conn: _Connection,
    manager: ConnectionManagerV2,
    principal: Principal,
    allowed_families: set[str],
    rate_limiter: OutboundRateLimiter,
) -> None:
    """Dispatch one inbound JSON frame."""
    if not isinstance(raw, dict):
        raise MalformedFrameError(reason="not_a_dict")

    mtype = raw.get("type")
    if mtype == "auth":
        # Post-handshake auth is a no-op — we're already authenticated.
        await _emit(
            conn.ws,
            {
                "type": "auth.ok",
                "v": WS_PROTOCOL_VERSION,
                "user_id": str(principal.user_id),
                "scopes": sorted(principal.scopes),
            },
        )
        return

    if mtype == "subscribe":
        try:
            frame = SubscribeFrame.model_validate(raw)
        except ValidationError as exc:
            raise MalformedFrameError(reason="subscribe_invalid") from exc
        if frame.channel not in allowed_families:
            raise ForbiddenError(reason="forbidden")
        authorize_channel(principal, frame.channel)
        await _do_subscribe_channels(frame, conn, manager)
        return

    if mtype == "unsubscribe":
        try:
            frame = UnsubscribeFrame.model_validate(raw)
        except ValidationError as exc:
            raise MalformedFrameError(reason="unsubscribe_invalid") from exc
        await _do_unsubscribe_channels(frame, conn, manager)
        return

    if mtype == "ping":
        try:
            frame = PingFrame.model_validate(raw)
        except ValidationError as exc:
            raise MalformedFrameError(reason="ping_invalid") from exc
        await rate_limiter.require(str(principal.user_id))
        await _emit(
            conn.ws,
            {
                "type": "pong",
                "v": WS_PROTOCOL_VERSION,
                "server_ts": _now_iso(),
                "client_ts": frame.ts.isoformat() if frame.ts else None,
            },
        )
        return

    # Unknown message type — surface as a recoverable error so the
    # client can keep going.
    raise WebSocketError(
        reason="unknown_message_type",
        code=CloseCode.MALFORMED,
    )


# ---------------------------------------------------------------------------
# Subscribe / unsubscribe
# ---------------------------------------------------------------------------
async def _do_subscribe(
    conn: _Connection,
    manager: ConnectionManagerV2,
    channel: Channel,
    principal: Principal,
) -> bool:
    authorize_channel(principal, channel.family)
    added = await manager.subscribe(conn, channel)
    if added:
        await _emit(
            conn.ws,
            {
                "type": "subscribed",
                "v": WS_PROTOCOL_VERSION,
                "channel": channel.family,
                "symbols": [channel.key] if channel.is_symbol_scoped else [],
            },
        )
    return added


async def _do_subscribe_channels(
    frame: SubscribeFrame, conn: _Connection, manager: ConnectionManagerV2
) -> None:
    if frame.channel in ("portfolio", "orders"):
        # User channels — there's exactly one per user, symbols list is ignored.
        channel = (
            for_portfolio(conn.principal.user_id)
            if frame.channel == "portfolio"
            else for_orders(conn.principal.user_id)
        )
        added = await manager.subscribe(conn, channel)
        if added:
            await _emit(
                conn.ws,
                {
                    "type": "subscribed",
                    "v": WS_PROTOCOL_VERSION,
                    "channel": frame.channel,
                    "symbols": [],
                },
            )
        else:
            # Already subscribed — still ack so the client knows the
            # operation was idempotent.
            await _emit(
                conn.ws,
                {
                    "type": "subscribed",
                    "v": WS_PROTOCOL_VERSION,
                    "channel": frame.channel,
                    "symbols": [],
                },
            )
        return

    added_keys: list[str] = []
    symbols = frame.symbols or []
    for raw_symbol in symbols:
        if not isinstance(raw_symbol, str):
            continue
        try:
            channel = (
                for_market(raw_symbol)
                if frame.channel == "market"
                else for_market_depth(raw_symbol)
            )
        except ValueError:
            # Bad symbol — skip rather than failing the whole batch.
            continue
        if await manager.subscribe(conn, channel):
            added_keys.append(channel.key)
    await _emit(
        conn.ws,
        {
            "type": "subscribed",
            "v": WS_PROTOCOL_VERSION,
            "channel": frame.channel,
            "symbols": added_keys,
        },
    )


async def _do_unsubscribe_channels(
    frame: UnsubscribeFrame, conn: _Connection, manager: ConnectionManagerV2
) -> None:
    if frame.channel in ("portfolio", "orders"):
        channel = (
            for_portfolio(conn.principal.user_id)
            if frame.channel == "portfolio"
            else for_orders(conn.principal.user_id)
        )
        await manager.unsubscribe(conn, channel)
        await _emit(
            conn.ws,
            {
                "type": "unsubscribed",
                "v": WS_PROTOCOL_VERSION,
                "channel": frame.channel,
                "symbols": [],
            },
        )
        return

    removed: list[str] = []
    for raw_symbol in frame.symbols or []:
        if not isinstance(raw_symbol, str):
            continue
        try:
            channel = (
                for_market(raw_symbol)
                if frame.channel == "market"
                else for_market_depth(raw_symbol)
            )
        except ValueError:
            continue
        if await manager.unsubscribe(conn, channel):
            removed.append(channel.key)
    await _emit(
        conn.ws,
        {
            "type": "unsubscribed",
            "v": WS_PROTOCOL_VERSION,
            "channel": frame.channel,
            "symbols": removed,
        },
    )


# ---------------------------------------------------------------------------
# Wire helpers
# ---------------------------------------------------------------------------
async def _emit(ws: WebSocket, payload: dict[str, Any]) -> None:
    with contextlib.suppress(Exception):
        await ws.send_json(payload)


async def _close(ws: WebSocket, code: int, reason: str) -> None:
    with contextlib.suppress(Exception):
        await ws.close(code=code, reason=reason)


def _error_code(exc: WebSocketError) -> str:
    from engine.api.websocket.exceptions import (
        ForbiddenError,
        MalformedFrameError,
        RateLimitedError,
        SubscriptionLimitError,
    )

    if isinstance(exc, RateLimitedError):
        return "rate_limited"
    if isinstance(exc, SubscriptionLimitError):
        return "too_many_subscriptions"
    if isinstance(exc, ForbiddenError):
        return "forbidden"
    if isinstance(exc, MalformedFrameError):
        return "malformed"
    return "server_error"


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(tz=UTC).isoformat()


__all__ = [
    "serve_market",
    "serve_orders",
    "serve_portfolio",
    "serve_unified",
]
