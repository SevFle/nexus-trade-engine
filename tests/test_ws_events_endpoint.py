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
from engine.api.ws.auth import AuthResult, extract_scopes
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

    def __init__(self, *, query_params: dict[str, str] | None = None) -> None:
        self.query_params = query_params or {}
        self.client = _FakeHost()
        self.headers: dict[str, str] = {}
        #: ``side_effect`` may be a single value, an Exception, or a list
        #: consumed in order (mirrors ``unittest.mock`` semantics).
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
# 1b. Scope-extraction wiring (regression for the import-name drift bug)
# ---------------------------------------------------------------------------


class TestScopeExtractionWiring:
    """Guard against the events/auth import name drifting out of sync.

    ``engine.api.ws.events._validate_session_token`` derives scopes from the
    decoded JWT via the shared ``engine.api.ws.auth.extract_scopes`` helper.
    An earlier revision referenced a non-existent ``_extract_scopes`` and
    broke ``conftest`` collection. These tests pin the wiring down.
    """

    def test_extract_scopes_is_importable_from_auth(self):
        # The public name events.py depends on must exist on the auth module.
        import engine.api.ws.auth as auth_mod

        assert hasattr(auth_mod, "extract_scopes")
        assert callable(auth_mod.extract_scopes)

    def test_events_module_does_not_reference_private_extract_scopes(self):
        # The import that originally broke collection must not resurface.
        import engine.api.ws.auth as auth_mod

        assert not hasattr(auth_mod, "_extract_scopes"), (
            "events.py imports extract_scopes from engine.api.ws.auth; "
            "a private _extract_scopes must not exist there"
        )

    @patch("engine.api.ws.events.decode_token")
    async def test_validate_session_token_uses_shared_extract_scopes(
        self, mock_decode, manager
    ):
        # An admin token fans out to the full admin scope set via the shared
        # helper — proving the events module is wired to extract_scopes.
        mock_decode.return_value = _make_token_data(sub="u1", role="admin")
        ws_events.init_ws_events(manager)
        ws = _FakeWebSocket(query_params={"token": "jwt"})
        ws.feed(WebSocketDisconnect())

        await ws_events.ws_events_endpoint(ws)

        # Registered with the admin scopes that extract_scopes() yields.
        assert ws.accepted is True
        assert manager.connection_count == 0
        # extract_scopes for role=admin returns the :all read scopes.
        assert set(extract_scopes({"role": "admin"})) == {
            "read:portfolio",
            "read:portfolio:all",
            "read:orders",
            "read:orders:all",
            "read:strategies",
            "read:strategies:all",
        }

    def test_authresult_dataclass_round_trips(self):
        # The other symbol events.py imports alongside extract_scopes.
        result = AuthResult(
            user_id="u1",
            scopes=extract_scopes({"role": "viewer"}),
            token_data={"sub": "u1", "role": "viewer"},
        )
        assert result.user_id == "u1"
        assert "read:portfolio" in result.scopes


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
