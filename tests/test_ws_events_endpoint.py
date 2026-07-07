"""Unit tests for the authenticated /ws/events streaming endpoint (SEV-275).

Covers the three behaviours introduced by ``engine.api.ws.events``:

1. **Auth rejection** — a missing/invalid session token rejects the
   handshake *before* ``ws.accept()``.
2. **Early loop capture** — :func:`init_ws_events` stores the running
   event loop on ``_state.loop`` immediately.
3. **Clean re-init** — a second ``init_ws_events`` disconnects every
   existing client and stops the previous bridge before installing the
   new one.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import WebSocketDisconnect

from engine.api.ws import events as ws_events
from engine.api.ws.connection_manager import ConnectionManager
from engine.api.ws.event_bridge import EventBusBridge
from engine.api.ws.protocol import WS_CLOSE_AUTH_INVALID

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeHost:
    host: str = "1.2.3.4"


class _FakeWebSocket:
    """Minimal WebSocket stand-in that records accept/close ordering."""

    def __init__(
        self,
        *,
        query_params: dict[str, str] | None = None,
        subprotocols: list[str] | None = None,
    ) -> None:
        self.query_params = query_params or {}
        #: ASGI connection scope — the canonical home of offered
        #: subprotocols (mirrors real Starlette sockets).
        self.scope: dict[str, Any] = {}
        if subprotocols is not None:
            self.scope["subprotocols"] = list(subprotocols)
        self.client = _FakeHost()
        self.headers: dict[str, str] = {}
        #: ``side_effect`` may be a single value, an Exception, or a list
        #: consumed in order (mirrors ``unittest.mock`` semantics).
        self._receive_side_effect: Any = None
        self.sent: list[dict] = []
        self.accepted = False
        self.accepted_subprotocol: str | None = None
        self.closed: list[tuple[int, str]] = []

    async def accept(self, subprotocol: str | None = None) -> None:
        self.accepted = True
        self.accepted_subprotocol = subprotocol

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


class _FakeBus:
    """In-memory EventBus double that records subscribe/unsubscribe."""

    def __init__(self) -> None:
        self._handlers: dict[Any, list] = {}

    def subscribe(self, event_type, handler) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    def unsubscribe(self, event_type, handler) -> None:
        handlers = self._handlers.get(event_type)
        if handlers is None:
            return
        self._handlers[event_type] = [h for h in handlers if h is not handler]


def _make_token_data(sub: str = "user123", role: str = "admin", **extra: Any) -> dict[str, Any]:
    return {"sub": sub, "role": role, "type": "access", **extra}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_ws_events_state():
    """Ensure each test starts (and ends) with a clean subsystem state."""
    ws_events.reset_state()
    yield
    ws_events.reset_state()


@pytest.fixture
def manager():
    return ConnectionManager()


# ---------------------------------------------------------------------------
# 1. Auth rejection — token validated before ws.accept()
# ---------------------------------------------------------------------------


class TestAuthRejectionBeforeAccept:
    async def test_missing_token_rejected_before_accept(self, manager):
        ws_events.init_ws_events(manager)
        ws = _FakeWebSocket(query_params={})
        ws.feed(WebSocketDisconnect())
        await ws_events.ws_events_endpoint(ws)
        assert ws.accepted is False
        assert ws.closed
        assert ws.closed[0][0] == WS_CLOSE_AUTH_INVALID

    @patch("engine.api.ws.events.decode_token", return_value=None)
    async def test_invalid_token_rejected_before_accept(self, _mock, manager):
        ws_events.init_ws_events(manager)
        ws = _FakeWebSocket(query_params={"token": "not-a-real-jwt"})
        ws.feed(WebSocketDisconnect())
        await ws_events.ws_events_endpoint(ws)
        assert ws.accepted is False
        assert ws.closed[0][0] == WS_CLOSE_AUTH_INVALID

    @patch("engine.api.ws.events.decode_token")
    async def test_token_without_sub_rejected_before_accept(self, mock_decode, manager):
        mock_decode.return_value = {"role": "admin", "type": "access"}
        ws_events.init_ws_events(manager)
        ws = _FakeWebSocket(query_params={"token": "jwt"})
        ws.feed(WebSocketDisconnect())
        await ws_events.ws_events_endpoint(ws)
        assert ws.accepted is False
        assert ws.closed[0][0] == WS_CLOSE_AUTH_INVALID

    @patch("engine.api.ws.events.decode_token")
    async def test_session_token_alias_accepted(self, mock_decode, manager):
        # The query param may also be named ``session_token``.
        mock_decode.return_value = _make_token_data(sub="u1", role="viewer")
        ws_events.init_ws_events(manager)
        ws = _FakeWebSocket(query_params={"session_token": "jwt"})
        ws.feed({"type": "ping", "ref": "r1"}, WebSocketDisconnect())

        await ws_events.ws_events_endpoint(ws)
        assert ws.accepted is True
        assert not ws.closed  # clean disconnect, not an auth close
        # Pong was sent in reply to the ping.
        assert any(m.get("type") == "pong" for m in ws.sent)

    @patch("engine.api.ws.events.decode_token")
    async def test_valid_token_accepted_and_registered(self, mock_decode, manager):
        mock_decode.return_value = _make_token_data(sub="u1", role="admin")
        ws_events.init_ws_events(manager)
        ws = _FakeWebSocket(query_params={"token": "jwt"})
        ws.feed(WebSocketDisconnect())

        await ws_events.ws_events_endpoint(ws)
        assert ws.accepted is True
        # The first outbound message is the connection ack.
        assert ws.sent
        assert ws.sent[0]["type"] == "ack"
        assert ws.sent[0]["status"] == "ok"
        # Connection was registered and then cleaned up on disconnect.
        assert manager.connection_count == 0

    async def test_rejected_token_does_not_register(self, manager):
        ws_events.init_ws_events(manager)
        ws = _FakeWebSocket(query_params={})
        ws.feed(WebSocketDisconnect())
        await ws_events.ws_events_endpoint(ws)
        assert ws.accepted is False
        assert manager.connection_count == 0

    async def test_endpoint_before_init_closes_with_server_error(self):
        # No init_ws_events() call — manager/resolver are None.
        ws = _FakeWebSocket(query_params={"token": "jwt"})
        ws.feed(WebSocketDisconnect())
        with patch("engine.api.ws.events.decode_token") as mock_decode:
            mock_decode.return_value = _make_token_data(sub="u1")
            await ws_events.ws_events_endpoint(ws)
        assert ws.accepted is False
        assert ws.closed
        # Server-error close (1011), not an auth close.
        assert ws.closed[0][0] == 1011


# ---------------------------------------------------------------------------
# 1b. Subprotocol-based handshake auth (bearer.<token>)
# ---------------------------------------------------------------------------


class TestSubprotocolHandshakeAuth:
    """Auth via the ``bearer.<token>`` WebSocket subprotocol.

    These tests verify the security constraints added to the handshake:
    a single bearer subprotocol authenticates, an ambiguous multi-bearer
    handshake is rejected before ``ws.accept()``, and the echoed
    subprotocol is always the constant ``auth.v1`` (never the raw token).
    """

    @patch("engine.api.ws.events.decode_token")
    async def test_single_bearer_subprotocol_authenticates(self, mock_decode, manager):
        mock_decode.return_value = _make_token_data(sub="handshake-user", role="admin")
        ws_events.init_ws_events(manager)
        ws = _FakeWebSocket(subprotocols=["bearer.jwt-from-subprotocol", "auth.v1"])
        ws.feed(WebSocketDisconnect())

        await ws_events.ws_events_endpoint(ws)
        assert ws.accepted is True
        # The token was decoded from the subprotocol (no query param given).
        assert mock_decode.call_args.args[0] == "jwt-from-subprotocol"

    @patch("engine.api.ws.events.decode_token")
    async def test_handshake_token_takes_precedence_over_empty_query(self, mock_decode, manager):
        mock_decode.return_value = _make_token_data(sub="u1")
        ws_events.init_ws_events(manager)
        # No query param, token arrives only via the subprotocol.
        ws = _FakeWebSocket(
            query_params={},
            subprotocols=["bearer.subproto-token", "auth.v1"],
        )
        ws.feed(WebSocketDisconnect())
        await ws_events.ws_events_endpoint(ws)
        assert ws.accepted is True
        assert mock_decode.call_args.args[0] == "subproto-token"

    @patch("engine.api.ws.events.decode_token")
    async def test_ambiguous_multi_bearer_rejected_before_accept(self, mock_decode, manager):
        # Two bearer subprotocols => ambiguous => reject before accept.
        mock_decode.return_value = _make_token_data(sub="u1")
        ws_events.init_ws_events(manager)
        ws = _FakeWebSocket(
            subprotocols=["bearer.token-a", "bearer.token-b", "auth.v1"],
        )
        ws.feed(WebSocketDisconnect())

        await ws_events.ws_events_endpoint(ws)
        assert ws.accepted is False
        assert ws.closed
        assert ws.closed[0][0] == WS_CLOSE_AUTH_INVALID
        # decode_token must never run on an ambiguous handshake.
        assert not mock_decode.called

    @patch("engine.api.ws.events.decode_token")
    async def test_ambiguous_rejection_does_not_register(self, mock_decode, manager):
        mock_decode.return_value = _make_token_data(sub="u1")
        ws_events.init_ws_events(manager)
        ws = _FakeWebSocket(subprotocols=["bearer.a", "bearer.b"])
        ws.feed(WebSocketDisconnect())
        await ws_events.ws_events_endpoint(ws)
        assert manager.connection_count == 0

    @patch("engine.api.ws.events.decode_token")
    async def test_invalid_handshake_token_rejected_before_accept(self, mock_decode, manager):
        mock_decode.return_value = None  # token fails to decode
        ws_events.init_ws_events(manager)
        ws = _FakeWebSocket(subprotocols=["bearer.not-a-real-jwt", "auth.v1"])
        ws.feed(WebSocketDisconnect())

        await ws_events.ws_events_endpoint(ws)
        assert ws.accepted is False
        assert ws.closed[0][0] == WS_CLOSE_AUTH_INVALID

    @patch("engine.api.ws.events.decode_token")
    async def test_conflicting_query_and_subprotocol_tokens_rejected(self, mock_decode, manager):
        # Same principal decodes either way, but the two channels disagree
        # on the *raw* token — refuse to guess.
        mock_decode.return_value = _make_token_data(sub="u1")
        ws_events.init_ws_events(manager)
        ws = _FakeWebSocket(
            query_params={"token": "query-token"},
            subprotocols=["bearer.subproto-token", "auth.v1"],
        )
        ws.feed(WebSocketDisconnect())

        await ws_events.ws_events_endpoint(ws)
        assert ws.accepted is False
        assert ws.closed[0][0] == WS_CLOSE_AUTH_INVALID

    @patch("engine.api.ws.events.decode_token")
    async def test_matching_query_and_subprotocol_tokens_accepted(self, mock_decode, manager):
        mock_decode.return_value = _make_token_data(sub="u1")
        ws_events.init_ws_events(manager)
        shared = "same-token"
        ws = _FakeWebSocket(
            query_params={"token": shared},
            subprotocols=[f"bearer.{shared}", "auth.v1"],
        )
        ws.feed(WebSocketDisconnect())

        await ws_events.ws_events_endpoint(ws)
        assert ws.accepted is True
        assert ws.accepted_subprotocol == "auth.v1"


class TestSubprotocolEcho:
    """The server must echo a constant subprotocol, never the raw token."""

    @patch("engine.api.ws.events.decode_token")
    async def test_echoes_constant_auth_v1(self, mock_decode, manager):
        mock_decode.return_value = _make_token_data(sub="u1")
        ws_events.init_ws_events(manager)
        ws = _FakeWebSocket(
            query_params={"token": "jwt"},
            subprotocols=["bearer.jwt", "auth.v1"],
        )
        ws.feed(WebSocketDisconnect())
        await ws_events.ws_events_endpoint(ws)
        assert ws.accepted_subprotocol == "auth.v1"

    @patch("engine.api.ws.events.decode_token")
    async def test_never_echoes_raw_bearer_token(self, mock_decode, manager):
        secret = "super-secret-jwt-value"
        mock_decode.return_value = _make_token_data(sub="u1")
        ws_events.init_ws_events(manager)
        ws = _FakeWebSocket(
            query_params={"token": secret},
            subprotocols=[f"bearer.{secret}", "auth.v1"],
        )
        ws.feed(WebSocketDisconnect())
        await ws_events.ws_events_endpoint(ws)
        assert ws.accepted is True
        # The echoed subprotocol must not leak the credential.
        echoed = ws.accepted_subprotocol or ""
        assert secret not in echoed
        assert not echoed.startswith("bearer.")

    @patch("engine.api.ws.events.decode_token")
    async def test_no_subprotocol_echo_when_constant_not_offered(self, mock_decode, manager):
        # A query-param client that never offered auth.v1 gets no echo.
        mock_decode.return_value = _make_token_data(sub="u1")
        ws_events.init_ws_events(manager)
        ws = _FakeWebSocket(query_params={"token": "jwt"})  # no subprotocols
        ws.feed(WebSocketDisconnect())
        await ws_events.ws_events_endpoint(ws)
        assert ws.accepted is True
        assert ws.accepted_subprotocol is None


# ---------------------------------------------------------------------------
# 2. Early loop capture
# ---------------------------------------------------------------------------


class TestEarlyLoopCapture:
    async def test_loop_captured_immediately_after_init(self, manager):
        assert ws_events._state.loop is None
        ws_events.init_ws_events(manager)
        assert ws_events._state.loop is asyncio.get_running_loop()

    async def test_loop_captured_before_bridge_start(self, manager):
        started_order: list[str] = []

        bridge = EventBusBridge(bus=_FakeBus(), manager=manager)
        original_start = bridge.start

        def spy_start(*args, **kwargs):
            # By the time bridge.start runs inside init_ws_events, the loop
            # must already be captured.
            if ws_events._state.loop is asyncio.get_running_loop():
                started_order.append("loop_first")
            original_start(*args, **kwargs)
            started_order.append("bridge_started")

        bridge.start = spy_start  # type: ignore[method-assign]
        ws_events.init_ws_events(manager, bridge=bridge)
        assert "loop_first" in started_order

    async def test_loop_is_the_currently_running_loop(self, manager):
        ws_events.init_ws_events(manager)
        captured = ws_events._state.loop
        assert captured is not None
        assert captured.is_running()


# ---------------------------------------------------------------------------
# 3. Clean re-init
# ---------------------------------------------------------------------------


class TestCleanReinit:
    async def test_reinit_stops_previous_bridge(self, manager):
        bridge1 = EventBusBridge(bus=_FakeBus(), manager=manager)
        ws_events.init_ws_events(manager, bridge=bridge1)
        assert bridge1._registered  # started -> subscribed

        bridge2 = EventBusBridge(bus=_FakeBus(), manager=ConnectionManager())
        ws_events.init_ws_events(ConnectionManager(), bridge=bridge2)

        # The old bridge was stopped (registrations cleared).
        assert bridge1._registered == []
        # And the new one is the active bridge.
        assert ws_events._state.bridge is bridge2
        assert bridge2._registered  # new bridge started

    async def test_reinit_disconnects_existing_clients(self):
        manager1 = ConnectionManager()
        ws_events.init_ws_events(manager1)

        # Register a *live* client against the first manager directly so it
        # is still connected at re-init time.
        live_ws = _FakeWebSocket()
        await manager1.register(live_ws, "live_user", [])
        assert manager1.connection_count == 1

        # Re-init with a brand-new manager — the live client must be closed.
        manager2 = ConnectionManager()
        ws_events.init_ws_events(manager2)
        # close_all is scheduled on the loop; let it drain. close_all sleeps
        # ~0.1s per connection internally, so poll for up to 1s.
        for _ in range(100):
            if live_ws.closed:
                break
            await asyncio.sleep(0.01)
        assert live_ws.closed
        assert manager1.connection_count == 0
        # The new manager owns the state.
        assert ws_events._state.manager is manager2

    async def test_reinit_schedules_close_all_on_captured_loop(self):
        manager1 = ConnectionManager()
        live_ws = _FakeWebSocket()
        await manager1.register(live_ws, "live_user", [])

        ws_events.init_ws_events(manager1)
        loop_before = ws_events._state.loop

        ws_events.init_ws_events(ConnectionManager())

        # A teardown task was scheduled on the captured loop; the live
        # socket must end up closed once it drains. close_all sleeps ~0.1s
        # per connection internally, so poll for up to 1s.
        for _ in range(100):
            if live_ws.closed:
                break
            await asyncio.sleep(0.01)
        assert live_ws.closed
        assert loop_before is asyncio.get_running_loop()

    async def test_reinit_without_previous_manager_is_noop_teardown(self, manager):
        # First init has nothing to tear down — must not raise.
        ws_events.init_ws_events(manager)
        assert ws_events._state.manager is manager

    async def test_reinit_replaces_resolver(self, manager):
        ws_events.init_ws_events(manager)
        first_resolver = ws_events._state.resolver
        manager2 = ConnectionManager()
        ws_events.init_ws_events(manager2)
        assert ws_events._state.resolver is not first_resolver
        assert ws_events._state.manager is manager2
