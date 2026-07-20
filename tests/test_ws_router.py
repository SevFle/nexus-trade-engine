"""Comprehensive unit tests for ``engine/api/ws/router.py`` (the ``/ws`` endpoint).

The generic ``/ws`` route is the primary WebSocket entrypoint of the API and
one of the project's core differentiators, yet it had **no dedicated tests**
(27% coverage before this file). These tests pin down every observable
behaviour of the route so future refactors can't silently regress the
handshake, message loop, or teardown.

Coverage areas:

1. **Module state** — ``init_ws`` / ``get_manager`` install and expose the
   subsystem singletons (manager, resolver, rate limiter).
2. **Handshake — server not ready** — the route closes with ``1011`` and
   never accepts when the subsystem is uninitialized.
3. **Handshake — auth failure** — when ``authenticate_websocket`` returns a
   ``(code, reason)`` tuple the client gets an ``AUTH_INVALID`` error, the
   socket is closed with the propagated code, and nothing is registered.
4. **Handshake — success** — accept, register with the principal's
   user_id/scopes, send an ``ack``, and unregister on disconnect.
5. **Message loop** — ping/pong, non-dict (INVALID_MESSAGE), parse errors,
   per-message metrics, and ``manager.touch`` bookkeeping.
6. **auth refresh message** — valid token updates the session; invalid token
   closes the connection and breaks the loop.
7. **subscribe / unsubscribe** — success and the permission-denied,
   unknown-channel, and missing-params error paths.
8. **Disconnect / unexpected-exception cleanup** — the ``finally`` block
   always runs ``unregister``.
9. **Helpers** — ``_safe_send`` and ``_close_ws`` swallow transport errors.
10. **Regression** — a non-dict JSON message must yield ``INVALID_MESSAGE``
    rather than crashing the connection (the metrics-line guard bug).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import WebSocketDisconnect

from engine.api.ws import router as ws_router
from engine.api.ws.auth import AuthRateLimiter, AuthResult
from engine.api.ws.connection_manager import ConnectionManager
from engine.api.ws.protocol import (
    WS_CLOSE_AUTH_INVALID,
    WS_CLOSE_SERVER_ERROR,
    WS_CLOSE_TOKEN_EXPIRED,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeHost:
    host: str = "203.0.113.7"


class _FakeWebSocket:
    """Minimal WebSocket double that scripts inbound messages and records
    accept/close/send ordering.

    ``feed(...)`` queues a sequence of values returned (or raised) by
    ``receive_json``. Once the queue drains, the next ``receive_json`` blocks
    forever — mirroring a real idle socket — so tests must terminate the loop
    explicitly with a ``WebSocketDisconnect`` at the end of the script.
    """

    def __init__(self, *, query_params: dict[str, str] | None = None) -> None:
        self.query_params = query_params or {}
        self.client = _FakeHost()
        self.headers: dict[str, str] = {}
        self._receive_side_effect: Any = None
        self.sent: list[dict] = []
        self.accepted = False
        self.closed: list[tuple[int, str]] = []

    async def accept(self) -> None:
        self.accepted = True

    async def receive_json(self):
        se = self._receive_side_effect
        if isinstance(se, list):
            if not se:
                # Default to blocking once the scripted messages run out.
                await asyncio.sleep(3600)
                return {}  # pragma: no cover - unreachable after sleep
            item = se.pop(0)
        else:
            item = se
        if isinstance(item, BaseException):
            raise item
        return item

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed.append((code, reason))

    def feed(self, *messages: Any) -> None:
        """Script the sequence of inbound JSON messages / exceptions."""
        self._receive_side_effect = list(messages)


def _auth_result(
    user_id: str = "u1",
    scopes: list[str] | None = None,
) -> AuthResult:
    if scopes is None:
        scopes = [
            "read:portfolio",
            "read:portfolio:all",
            "read:orders",
            "read:orders:all",
            "read:strategies",
            "read:strategies:all",
        ]
    return AuthResult(user_id=user_id, scopes=scopes, token_data={"sub": user_id})


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_router_state():
    """Each test starts (and ends) with a clean router ``_state``."""
    ws_router._state.manager = None
    ws_router._state.resolver = None
    ws_router._state.rate_limiter = None
    yield
    ws_router._state.manager = None
    ws_router._state.resolver = None
    ws_router._state.rate_limiter = None


@pytest.fixture
def manager():
    return ConnectionManager()


@pytest.fixture
def ready(manager):
    """Initialise the router subsystem with a fresh manager."""
    ws_router.init_ws(manager)
    return manager


def _patch_auth(result: AuthResult | tuple):
    """Patch ``authenticate_websocket`` to a fixed outcome."""
    return patch(
        "engine.api.ws.router.authenticate_websocket",
        new=AsyncMock(return_value=result),
    )


# ---------------------------------------------------------------------------
# 1. Module state — init_ws / get_manager
# ---------------------------------------------------------------------------


class TestModuleState:
    def test_get_manager_none_before_init(self):
        assert ws_router.get_manager() is None

    def test_init_ws_installs_manager(self, manager):
        ws_router.init_ws(manager)
        assert ws_router.get_manager() is manager
        assert ws_router._state.manager is manager

    def test_init_ws_creates_resolver_bound_to_manager(self, manager):
        ws_router.init_ws(manager)
        resolver = ws_router._state.resolver
        assert resolver is not None
        # The resolver is bound to the same manager instance.
        assert resolver._manager is manager

    def test_init_ws_stores_rate_limiter_when_provided(self, manager):
        limiter = AuthRateLimiter()
        ws_router.init_ws(manager, rate_limiter=limiter)
        assert ws_router._state.rate_limiter is limiter

    def test_init_ws_rate_limiter_defaults_to_none(self, manager):
        ws_router.init_ws(manager)
        assert ws_router._state.rate_limiter is None

    def test_init_ws_replaces_previous_manager(self, manager):
        ws_router.init_ws(manager)
        first = ws_router.get_manager()
        manager2 = ConnectionManager()
        ws_router.init_ws(manager2)
        assert ws_router.get_manager() is manager2
        assert first is not manager2


# ---------------------------------------------------------------------------
# 2. Handshake — server not ready
# ---------------------------------------------------------------------------


class TestHandshakeServerNotReady:
    async def test_closes_with_server_error_when_not_initialized(self):
        # No init_ws() call — manager and resolver are both None.
        ws = _FakeWebSocket()
        ws.feed(WebSocketDisconnect())
        await ws_router.ws_endpoint(ws)
        assert ws.accepted is False
        assert ws.closed
        assert ws.closed[0][0] == WS_CLOSE_SERVER_ERROR  # 1011

    async def test_closes_when_resolver_missing_but_manager_set(self, manager):
        # manager set but resolver left None — still "not ready".
        ws_router._state.manager = manager
        ws_router._state.resolver = None
        ws = _FakeWebSocket()
        ws.feed(WebSocketDisconnect())
        await ws_router.ws_endpoint(ws)
        assert ws.accepted is False
        assert ws.closed[0][0] == WS_CLOSE_SERVER_ERROR

    async def test_not_ready_close_suppresses_send_failures(self, manager):
        # Even if ws.close() raises, the endpoint must not propagate.
        ws_router._state.manager = manager
        ws_router._state.resolver = None

        class _BrokenClose(_FakeWebSocket):
            async def close(self, code: int = 1000, reason: str = "") -> None:
                raise RuntimeError("transport gone")

        ws = _BrokenClose()
        ws.feed(WebSocketDisconnect())
        # Must not raise.
        await ws_router.ws_endpoint(ws)
        assert ws.accepted is False


# ---------------------------------------------------------------------------
# 3. Handshake — auth failure
# ---------------------------------------------------------------------------


class TestHandshakeAuthFailure:
    async def test_auth_tuple_sends_auth_invalid_error(self, ready):
        ws = _FakeWebSocket()
        ws.feed(WebSocketDisconnect())
        with _patch_auth((WS_CLOSE_AUTH_INVALID, "bad token")):
            await ws_router.ws_endpoint(ws)
        # An error frame was sent before the close.
        assert any(
            m.get("type") == "error" and m.get("code") == "AUTH_INVALID"
            for m in ws.sent
        )

    async def test_auth_tuple_closes_with_propagated_code(self, ready):
        ws = _FakeWebSocket()
        ws.feed(WebSocketDisconnect())
        with _patch_auth((WS_CLOSE_AUTH_INVALID, "bad token")):
            await ws_router.ws_endpoint(ws)
        assert ws.accepted is True  # accept happens before auth
        assert ws.closed
        assert ws.closed[0] == (WS_CLOSE_AUTH_INVALID, "bad token")

    async def test_auth_tuple_does_not_register(self, ready):
        ws = _FakeWebSocket()
        ws.feed(WebSocketDisconnect())
        with _patch_auth((WS_CLOSE_AUTH_INVALID, "bad token")):
            await ws_router.ws_endpoint(ws)
        assert ready.connection_count == 0

    async def test_auth_tuple_reason_reflected_in_error_message(self, ready):
        ws = _FakeWebSocket()
        ws.feed(WebSocketDisconnect())
        with _patch_auth((4404, "scope denied")):
            await ws_router.ws_endpoint(ws)
        error = next(m for m in ws.sent if m.get("type") == "error")
        assert error["message"] == "scope denied"

    async def test_auth_error_send_failure_is_swallowed(self, ready):
        # If send_json raises on the error frame, the close must still run.
        ws = _FakeWebSocket()
        ws.feed(WebSocketDisconnect())

        original_send = ws.send_json

        async def flaky_send(payload):
            if payload.get("type") == "error":
                raise RuntimeError("write pipe closed")
            await original_send(payload)

        ws.send_json = flaky_send  # type: ignore[method-assign]
        with _patch_auth((WS_CLOSE_AUTH_INVALID, "bad token")):
            await ws_router.ws_endpoint(ws)
        # close was still called despite the send failure.
        assert ws.closed


# ---------------------------------------------------------------------------
# 4. Handshake — success
# ---------------------------------------------------------------------------


class TestHandshakeSuccess:
    async def test_accepts_and_sends_ack(self, ready):
        ws = _FakeWebSocket()
        ws.feed(WebSocketDisconnect())
        with _patch_auth(_auth_result()):
            await ws_router.ws_endpoint(ws)
        assert ws.accepted is True
        # First outbound frame is the connection ack.
        assert ws.sent
        assert ws.sent[0]["type"] == "ack"
        assert ws.sent[0]["status"] == "ok"
        assert ws.sent[0]["message"] == "connected"

    async def test_registers_with_user_id_and_scopes(self, ready):
        ws = _FakeWebSocket()
        ws.feed(WebSocketDisconnect())
        captured = {}

        original_register = ready.register

        async def spy_register(ws_arg, user_id, scopes, metadata=None):
            cid = await original_register(ws_arg, user_id, scopes, metadata)
            captured["user_id"] = user_id
            captured["scopes"] = list(scopes)
            return cid

        ready.register = spy_register  # type: ignore[method-assign]
        with _patch_auth(_auth_result(user_id="alice", scopes=["read:portfolio"])):
            await ws_router.ws_endpoint(ws)
        assert captured["user_id"] == "alice"
        assert captured["scopes"] == ["read:portfolio"]

    async def test_unregisters_on_clean_disconnect(self, ready):
        ws = _FakeWebSocket()
        ws.feed(WebSocketDisconnect())
        with _patch_auth(_auth_result()):
            await ws_router.ws_endpoint(ws)
        # The finally block ran unregister — connection bookkeeping is clean.
        assert ready.connection_count == 0


# ---------------------------------------------------------------------------
# 5. Message loop
# ---------------------------------------------------------------------------


class TestMessageLoop:
    async def test_ping_returns_pong_with_matching_ref(self, ready):
        ws = _FakeWebSocket()
        ws.feed({"type": "ping", "ref": "r1"}, WebSocketDisconnect())
        with _patch_auth(_auth_result()):
            await ws_router.ws_endpoint(ws)
        pongs = [m for m in ws.sent if m.get("type") == "pong"]
        assert pongs
        assert pongs[0]["ref"] == "r1"

    async def test_ping_without_ref_returns_pong_with_null_ref(self, ready):
        ws = _FakeWebSocket()
        ws.feed({"type": "ping"}, WebSocketDisconnect())
        with _patch_auth(_auth_result()):
            await ws_router.ws_endpoint(ws)
        pongs = [m for m in ws.sent if m.get("type") == "pong"]
        assert pongs
        assert pongs[0]["ref"] is None

    async def test_non_dict_message_returns_invalid_message(self, ready):
        # Regression: previously crashed the connection with AttributeError
        # because the metrics line called raw.get(...) before the
        # isinstance(raw, dict) guard.
        ws = _FakeWebSocket()
        ws.feed(["not", "a", "dict"], WebSocketDisconnect())
        with _patch_auth(_auth_result()):
            await ws_router.ws_endpoint(ws)
        errors = [m for m in ws.sent if m.get("type") == "error"]
        assert errors
        assert errors[0]["code"] == "INVALID_MESSAGE"
        assert errors[0]["message"] == "expected JSON object"
        # The connection survived the bad frame (ack was sent, no close).
        assert ws.accepted is True
        assert not ws.closed

    async def test_non_dict_scalar_message_returns_invalid_message(self, ready):
        # A bare JSON scalar (number) must not crash the loop either.
        ws = _FakeWebSocket()
        ws.feed(42, WebSocketDisconnect())
        with _patch_auth(_auth_result()):
            await ws_router.ws_endpoint(ws)
        assert any(m.get("code") == "INVALID_MESSAGE" for m in ws.sent)

    async def test_unknown_message_type_returns_parse_error(self, ready):
        ws = _FakeWebSocket()
        ws.feed({"type": "bogus"}, WebSocketDisconnect())
        with _patch_auth(_auth_result()):
            await ws_router.ws_endpoint(ws)
        errors = [m for m in ws.sent if m.get("type") == "error"]
        assert errors
        assert errors[0]["code"] == "PARSE_ERROR"
        assert "bogus" in errors[0]["message"]

    async def test_missing_type_returns_parse_error(self, ready):
        ws = _FakeWebSocket()
        ws.feed({"ref": "x"}, WebSocketDisconnect())
        with _patch_auth(_auth_result()):
            await ws_router.ws_endpoint(ws)
        errors = [m for m in ws.sent if m.get("type") == "error"]
        assert errors
        assert errors[0]["code"] == "PARSE_ERROR"

    async def test_subscribe_missing_required_field_returns_parse_error(self, ready):
        # SubscribeMessage requires channel (min_length=1).
        ws = _FakeWebSocket()
        ws.feed({"type": "subscribe", "params": {}}, WebSocketDisconnect())
        with _patch_auth(_auth_result()):
            await ws_router.ws_endpoint(ws)
        errors = [m for m in ws.sent if m.get("type") == "error"]
        assert errors
        assert errors[0]["code"] == "PARSE_ERROR"

    async def test_metrics_counter_records_received_message_type(self, ready):
        ws = _FakeWebSocket()
        ws.feed({"type": "ping", "ref": "r1"}, WebSocketDisconnect())
        with patch("engine.api.ws.router.ws_metrics") as mock_metrics, _patch_auth(_auth_result()):
            await ws_router.ws_endpoint(ws)
        # The counter was called with the inbound type tag.
        calls = mock_metrics.metrics.counter.call_args_list
        tagged = [c for c in calls if c.args and c.args[0] == "sev_ws_messages_received_total"]
        assert tagged
        assert tagged[0].kwargs["tags"] == {"type": "ping"}

    async def test_metrics_counter_uses_unknown_tag_for_non_dict(self, ready):
        # Regression guard for the metrics-line guard bug.
        ws = _FakeWebSocket()
        ws.feed(["x"], WebSocketDisconnect())
        with patch("engine.api.ws.router.ws_metrics") as mock_metrics, _patch_auth(_auth_result()):
            await ws_router.ws_endpoint(ws)
        calls = mock_metrics.metrics.counter.call_args_list
        tagged = [c for c in calls if c.args and c.args[0] == "sev_ws_messages_received_total"]
        assert tagged
        assert tagged[0].kwargs["tags"] == {"type": "unknown"}

    async def test_touch_called_on_message_receipt(self, ready):
        ws = _FakeWebSocket()
        ws.feed({"type": "ping", "ref": "r1"}, WebSocketDisconnect())
        seen: list[str] = []

        original_touch = ready.touch

        def spy_touch(cid: str) -> None:
            seen.append(cid)
            original_touch(cid)

        ready.touch = spy_touch  # type: ignore[method-assign]
        with _patch_auth(_auth_result()):
            await ws_router.ws_endpoint(ws)
        # touch was called at least once with the connection id.
        assert seen
        # All touch calls used the same connection id.
        assert len(set(seen)) == 1


# ---------------------------------------------------------------------------
# 6. auth refresh message
# ---------------------------------------------------------------------------


class TestAuthRefreshMessage:
    async def test_valid_refresh_token_updates_session_and_acks(self, ready):
        ws = _FakeWebSocket()
        ws.feed(
            {"type": "auth", "token": "refresh-jwt", "ref": "r2"},
            WebSocketDisconnect(),
        )
        refreshed = _auth_result(user_id="u2", scopes=["read:portfolio"])
        with patch(
            "engine.api.ws.router.validate_refresh_token",
            return_value=refreshed,
        ), _patch_auth(_auth_result()):
            await ws_router.ws_endpoint(ws)
        acks = [m for m in ws.sent if m.get("type") == "ack"]
        # The second ack (after the connection ack) carries the refresh ref.
        refresh_ack = next(a for a in acks if a.get("ref") == "r2")
        assert refresh_ack["status"] == "ok"
        assert refresh_ack["message"] == "token refreshed"

    async def test_valid_refresh_token_session_used_for_subsequent_subscribe(
        self, ready
    ):
        # After a refresh, the new scopes must govern the next subscribe —
        # proving the session object is mutated in place. The initial auth
        # has NO portfolio scope (so a subscribe would 403), then the refresh
        # grants read:portfolio:all (so the same subscribe now succeeds).
        ws = _FakeWebSocket()
        ws.feed(
            {"type": "auth", "token": "refresh-jwt", "ref": "r2"},
            {
                "type": "subscribe",
                "channel": "portfolio",
                "params": {"account_id": "acct-1"},
                "ref": "r3",
            },
            WebSocketDisconnect(),
        )
        refreshed = _auth_result(
            user_id="owner", scopes=["read:portfolio:all"]
        )
        with patch(
            "engine.api.ws.router.validate_refresh_token",
            return_value=refreshed,
        ), _patch_auth(_auth_result(user_id="owner", scopes=[])):
            await ws_router.ws_endpoint(ws)
        sub_ack = next(a for a in ws.sent if a.get("ref") == "r3")
        assert sub_ack["status"] == "ok"

    async def test_invalid_refresh_token_sends_auth_invalid_error(self, ready):
        ws = _FakeWebSocket()
        ws.feed(
            {"type": "auth", "token": "garbage", "ref": "r2"},
            WebSocketDisconnect(),
        )
        with patch("engine.api.ws.router.validate_refresh_token", return_value=None), _patch_auth(_auth_result()):
            await ws_router.ws_endpoint(ws)
        errors = [m for m in ws.sent if m.get("type") == "error"]
        assert errors
        assert errors[0]["code"] == "AUTH_INVALID"
        assert errors[0]["ref"] == "r2"

    async def test_invalid_refresh_token_closes_with_token_expired(self, ready):
        ws = _FakeWebSocket()
        ws.feed(
            {"type": "auth", "token": "garbage", "ref": "r2"},
            WebSocketDisconnect(),
        )
        with patch("engine.api.ws.router.validate_refresh_token", return_value=None), _patch_auth(_auth_result()):
            await ws_router.ws_endpoint(ws)
        assert ws.closed
        assert ws.closed[0][0] == WS_CLOSE_TOKEN_EXPIRED  # 4403

    async def test_invalid_refresh_token_breaks_the_loop(self, ready):
        # After a failed refresh the connection must terminate — a
        # subsequent ping must NOT be answered.
        ws = _FakeWebSocket()
        ws.feed(
            {"type": "auth", "token": "garbage", "ref": "r2"},
            {"type": "ping", "ref": "never"},
            WebSocketDisconnect(),
        )
        with patch("engine.api.ws.router.validate_refresh_token", return_value=None), _patch_auth(_auth_result()):
            await ws_router.ws_endpoint(ws)
        # No pong was ever sent for the post-failure ping.
        assert not any(m.get("ref") == "never" and m.get("type") == "pong" for m in ws.sent)


# ---------------------------------------------------------------------------
# 7. subscribe / unsubscribe
# ---------------------------------------------------------------------------


class TestSubscribeMessage:
    async def test_subscribe_success_sends_ok_ack(self, ready):
        ws = _FakeWebSocket()
        ws.feed(
            {
                "type": "subscribe",
                "channel": "portfolio",
                "params": {"account_id": "acct-1"},
                "ref": "s1",
            },
            WebSocketDisconnect(),
        )
        with _patch_auth(_auth_result()):
            await ws_router.ws_endpoint(ws)
        ack = next(a for a in ws.sent if a.get("ref") == "s1")
        assert ack["status"] == "ok"

    async def test_subscribe_joins_resolved_room(self, ready):
        ws = _FakeWebSocket()
        ws.feed(
            {
                "type": "subscribe",
                "channel": "portfolio",
                "params": {"account_id": "acct-1"},
                "ref": "s1",
            },
            WebSocketDisconnect(),
        )
        captured_rooms: set[str] = set()
        original_join = ready.join_room

        async def spy_join(cid, room):
            captured_rooms.add(room)
            return await original_join(cid, room)

        ready.join_room = spy_join  # type: ignore[method-assign]
        with _patch_auth(_auth_result()):
            await ws_router.ws_endpoint(ws)
        assert "portfolio:account:acct-1" in captured_rooms

    async def test_subscribe_permission_denied_sends_error_ack(self, ready):
        # No portfolio scope at all -> 403.
        ws = _FakeWebSocket()
        ws.feed(
            {
                "type": "subscribe",
                "channel": "portfolio",
                "params": {"account_id": "acct-1"},
                "ref": "s1",
            },
            WebSocketDisconnect(),
        )
        with _patch_auth(_auth_result(scopes=[])):
            await ws_router.ws_endpoint(ws)
        ack = next(a for a in ws.sent if a.get("ref") == "s1")
        assert ack["status"] == "error"
        assert ack["error_code"] == "403"

    async def test_subscribe_unknown_channel_sends_error_ack(self, ready):
        ws = _FakeWebSocket()
        ws.feed(
            {
                "type": "subscribe",
                "channel": "totes-fake",
                "params": {},
                "ref": "s1",
            },
            WebSocketDisconnect(),
        )
        with _patch_auth(_auth_result()):
            await ws_router.ws_endpoint(ws)
        ack = next(a for a in ws.sent if a.get("ref") == "s1")
        assert ack["status"] == "error"
        assert ack["error_code"] == "404"

    async def test_subscribe_missing_params_sends_error_ack(self, ready):
        # portfolio requires account_id or strategy_id to resolve a room.
        ws = _FakeWebSocket()
        ws.feed(
            {"type": "subscribe", "channel": "portfolio", "params": {}, "ref": "s1"},
            WebSocketDisconnect(),
        )
        with _patch_auth(_auth_result()):
            await ws_router.ws_endpoint(ws)
        ack = next(a for a in ws.sent if a.get("ref") == "s1")
        assert ack["status"] == "error"
        assert ack["error_code"] == "400"

    async def test_subscribe_owner_mismatch_denied_without_all_scope(self, ready):
        # Has read:portfolio (not :all) but tries another user's account.
        ws = _FakeWebSocket()
        ws.feed(
            {
                "type": "subscribe",
                "channel": "portfolio",
                "params": {"account_id": "someone-else"},
                "ref": "s1",
            },
            WebSocketDisconnect(),
        )
        with _patch_auth(_auth_result(user_id="me", scopes=["read:portfolio"])):
            await ws_router.ws_endpoint(ws)
        ack = next(a for a in ws.sent if a.get("ref") == "s1")
        assert ack["status"] == "error"
        assert ack["error_code"] == "403"


class TestUnsubscribeMessage:
    async def test_unsubscribe_sends_ok_ack(self, ready):
        ws = _FakeWebSocket()
        ws.feed(
            {
                "type": "subscribe",
                "channel": "portfolio",
                "params": {"account_id": "acct-1"},
                "ref": "sub",
            },
            {
                "type": "unsubscribe",
                "channel": "portfolio",
                "params": {"account_id": "acct-1"},
                "ref": "unsub",
            },
            WebSocketDisconnect(),
        )
        with _patch_auth(_auth_result()):
            await ws_router.ws_endpoint(ws)
        ack = next(a for a in ws.sent if a.get("ref") == "unsub")
        assert ack["status"] == "ok"

    async def test_unsubscribe_leaves_resolved_room(self, ready):
        ws = _FakeWebSocket()
        ws.feed(
            {
                "type": "subscribe",
                "channel": "portfolio",
                "params": {"account_id": "acct-1"},
                "ref": "sub",
            },
            {
                "type": "unsubscribe",
                "channel": "portfolio",
                "params": {"account_id": "acct-1"},
                "ref": "unsub",
            },
            WebSocketDisconnect(),
        )
        left_rooms: list[str] = []
        original_leave = ready.leave_room

        async def spy_leave(cid, room):
            left_rooms.append(room)
            return await original_leave(cid, room)

        ready.leave_room = spy_leave  # type: ignore[method-assign]
        with _patch_auth(_auth_result()):
            await ws_router.ws_endpoint(ws)
        assert "portfolio:account:acct-1" in left_rooms

    async def test_unsubscribe_missing_params_still_ok(self, ready):
        # resolve_room_name returns None -> handle_unsubscribe returns success.
        ws = _FakeWebSocket()
        ws.feed(
            {
                "type": "unsubscribe",
                "channel": "portfolio",
                "params": {},
                "ref": "unsub",
            },
            WebSocketDisconnect(),
        )
        with _patch_auth(_auth_result()):
            await ws_router.ws_endpoint(ws)
        ack = next(a for a in ws.sent if a.get("ref") == "unsub")
        assert ack["status"] == "ok"


# ---------------------------------------------------------------------------
# 8. Disconnect / unexpected-exception cleanup
# ---------------------------------------------------------------------------


class TestDisconnectAndErrorCleanup:
    async def test_websocket_disconnect_breaks_cleanly(self, ready):
        ws = _FakeWebSocket()
        ws.feed(WebSocketDisconnect())
        with _patch_auth(_auth_result()):
            # Must return promptly (no hang) on a clean disconnect.
            await asyncio.wait_for(
                ws_router.ws_endpoint(ws), timeout=5.0
            )
        assert ready.connection_count == 0

    async def test_unexpected_exception_is_logged(self, ready):
        ws = _FakeWebSocket()

        class _BoomError(Exception):
            pass

        ws.feed(_BoomError("kaboom"), WebSocketDisconnect())
        with patch("engine.api.ws.router.logger") as mock_logger, _patch_auth(_auth_result()):
            await ws_router.ws_endpoint(ws)
        # A warning was emitted with the ws.unexpected_error event.
        warning_calls = mock_logger.warning.call_args_list
        assert any(
            call.args and call.args[0] == "ws.unexpected_error"
            for call in warning_calls
        )

    async def test_unexpected_exception_truncates_error_message(self, ready):
        ws = _FakeWebSocket()

        class _BoomError(Exception):
            pass

        ws.feed(_BoomError("x" * 5000), WebSocketDisconnect())
        captured = {}
        original_warning = ws_router.logger.warning

        def spy_warning(event, **kwargs):
            captured.update(kwargs)
            return original_warning(event, **kwargs)

        with patch("engine.api.ws.router.logger") as mock_logger:
            mock_logger.warning.side_effect = spy_warning
            with _patch_auth(_auth_result()):
                await ws_router.ws_endpoint(ws)
        # The message is truncated to <=200 chars per the [:200] slice.
        assert len(captured["error_message"]) <= 200

    async def test_unexpected_exception_still_runs_unregister(self, ready):
        ws = _FakeWebSocket()

        class _BoomError(Exception):
            pass

        ws.feed(_BoomError("kaboom"), WebSocketDisconnect())
        with _patch_auth(_auth_result()):
            await ws_router.ws_endpoint(ws)
        # The finally block ran unregister even on the error path.
        assert ready.connection_count == 0

    async def test_disconnect_during_initial_receive_not_registered(self, ready):
        # If the client disconnects before the first message, no work is
        # done beyond the connection ack; cleanup still runs.
        ws = _FakeWebSocket()
        ws.feed(WebSocketDisconnect())
        with _patch_auth(_auth_result()):
            await ws_router.ws_endpoint(ws)
        assert ws.accepted is True
        assert ready.connection_count == 0


# ---------------------------------------------------------------------------
# 9. Helpers — _safe_send / _close_ws
# ---------------------------------------------------------------------------


class TestSafeSendAndCloseHelpers:
    async def test_safe_send_suppresses_send_exception(self, ready):
        ws = _FakeWebSocket()

        async def broken_send(_payload):
            raise RuntimeError("write pipe closed")

        ws.send_json = broken_send  # type: ignore[method-assign]
        # A ping triggers a _safe_send -> PongMessage. The broken send must
        # be swallowed, not propagated.
        ws.feed({"type": "ping", "ref": "r1"}, WebSocketDisconnect())
        with _patch_auth(_auth_result()):
            await ws_router.ws_endpoint(ws)

    async def test_close_ws_suppresses_close_exception(self, ready):
        ws = _FakeWebSocket()
        ws.feed(
            {"type": "auth", "token": "garbage", "ref": "r2"},
            WebSocketDisconnect(),
        )

        async def broken_close(code=1000, reason=""):
            raise RuntimeError("already closed")

        ws.close = broken_close  # type: ignore[method-assign]
        with patch("engine.api.ws.router.validate_refresh_token", return_value=None), _patch_auth(_auth_result()):
            # Must not raise even though close() blows up on the failed
            # refresh-token path.
            await ws_router.ws_endpoint(ws)


# ---------------------------------------------------------------------------
# 10. _dispatch_message — direct unit coverage
# ---------------------------------------------------------------------------


class TestDispatchMessageDirect:
    """Drive ``_dispatch_message`` directly to cover the unknown-type branch
    that the public endpoint never reaches through normal JSON."""

    async def test_unknown_message_type_returns_false(self, ready):
        ws = _FakeWebSocket()
        session = ws_router._Session(user_id="u1", scopes=[])

        class _Unknown:
            type = "totally-unknown"
            ref = None

        should_break = await ws_router._dispatch_message(
            ws, _Unknown(), "conn-1", session
        )
        assert should_break is False
        # Nothing was sent for an unknown type.
        assert ws.sent == []
