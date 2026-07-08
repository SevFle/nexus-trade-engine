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
from starlette.exceptions import WebSocketException

from engine.api.ws import events as ws_events
from engine.api.ws.connection_manager import ConnectionManager
from engine.api.ws.event_bridge import EventBusBridge
from engine.api.ws.protocol import WS_CLOSE_AUTH_INVALID
from engine.config import Settings

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
        headers: dict[str, str] | None = None,
    ) -> None:
        self.query_params = query_params or {}
        # Default to an allowed Origin so the pre-existing tests (which
        # don't exercise origin validation) keep passing now that the
        # endpoint enforces an origin allowlist. Tests that *do* care can
        # pass an explicit ``headers`` mapping (e.g. ``{}`` for "missing").
        self.headers = {"origin": "http://localhost:3000"} if headers is None else dict(headers)
        self.client = _FakeHost()
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


# ---------------------------------------------------------------------------
# 4. Origin allowlist — CSWSH protection (validated before ws.accept())
# ---------------------------------------------------------------------------


class TestOriginAllowlist:
    """The ``Origin`` header must be present and on the allowlist.

    A missing or disallowed origin rejects the handshake at the HTTP
    layer with ``403`` *before* the socket is accepted (and before auth).
    """

    async def test_rejects_missing_origin(self, manager):
        ws_events.init_ws_events(manager)
        ws = _FakeWebSocket(query_params={"token": "jwt"}, headers={})
        ws.feed(WebSocketDisconnect())
        with patch("engine.api.ws.events.decode_token") as mock_decode:
            mock_decode.return_value = _make_token_data(sub="u1")
            with pytest.raises(WebSocketException) as exc_info:
                await ws_events.ws_events_endpoint(ws)
        # Rejected before upgrade with WS close code 1008 (policy violation).
        assert exc_info.value.code == 1008
        # Nothing was accepted or registered.
        assert ws.accepted is False
        assert not ws.closed
        assert manager.connection_count == 0

    async def test_rejects_disallowed_origin(self, manager):
        ws_events.init_ws_events(manager)
        ws = _FakeWebSocket(
            query_params={"token": "jwt"},
            headers={"origin": "https://evil.example.com"},
        )
        ws.feed(WebSocketDisconnect())
        with patch("engine.api.ws.events.decode_token") as mock_decode:
            # Even a *valid* token must not rescue a bad origin.
            mock_decode.return_value = _make_token_data(sub="u1", role="admin")
            with pytest.raises(WebSocketException) as exc_info:
                await ws_events.ws_events_endpoint(ws)
        assert exc_info.value.code == 1008
        assert ws.accepted is False
        assert not ws.closed
        assert manager.connection_count == 0

    @patch("engine.api.ws.events.decode_token")
    async def test_accepts_allowed_origin(self, mock_decode, manager):
        # Every default dev origin must be accepted.
        from engine.config import settings

        mock_decode.return_value = _make_token_data(sub="u1", role="admin")
        ws_events.init_ws_events(manager)
        for origin in settings.allowed_origins:
            ws = _FakeWebSocket(
                query_params={"token": "jwt"},
                headers={"origin": origin},
            )
            ws.feed(WebSocketDisconnect())
            await ws_events.ws_events_endpoint(ws)
            # Origin was on the allowlist, so the handshake proceeded.
            assert ws.accepted is True
            assert ws.sent[0]["type"] == "ack"
        # Each connection was cleaned up on disconnect.
        assert manager.connection_count == 0


# ---------------------------------------------------------------------------
# 5. _validate_origin — direct unit tests (WebSocketException, code 1008)
# ---------------------------------------------------------------------------


class TestValidateOriginUnit:
    """Direct unit tests for the origin allowlist check.

    Locks in the post-fix behaviour: the handshake is rejected with a
    :class:`starlette.exceptions.WebSocketException` (close code ``1008``,
    policy violation) rather than an ``HTTPException``. ``HTTPException``
    is the wrong tool for a WebSocket route — Starlette's WS exception
    handler does not match it, so it would surface as an unhandled
    ``1011`` server-error close instead of a clean rejection.
    """

    def test_disallowed_origin_raises_websocket_exception(self):
        ws = _FakeWebSocket(headers={"origin": "https://evil.example.com"})
        with pytest.raises(WebSocketException) as exc_info:
            ws_events._validate_origin(ws)
        assert exc_info.value.code == 1008
        assert exc_info.value.reason == "origin not allowed"

    def test_missing_origin_raises_websocket_exception(self):
        ws = _FakeWebSocket(headers={})
        with pytest.raises(WebSocketException) as exc_info:
            ws_events._validate_origin(ws)
        assert exc_info.value.code == 1008
        assert exc_info.value.reason == "origin not allowed"

    def test_allowed_origin_does_not_raise(self):
        ws = _FakeWebSocket(headers={"origin": "http://localhost:3000"})
        # Returns None and raises nothing for an allowlisted origin.
        assert ws_events._validate_origin(ws) is None

    def test_origin_case_variants_accepted(self):
        # The lookup covers both common header casings used by the fake WS.
        for header_name in ("origin", "Origin"):
            ws = _FakeWebSocket(headers={header_name: "http://localhost:3000"})
            assert ws_events._validate_origin(ws) is None

    def test_raises_websocket_exception_not_http_exception(self):
        # Regression guard: must be WebSocketException, NOT HTTPException
        # (Starlette's WS handler ignores HTTPException → unhandled 1011).
        from fastapi import HTTPException

        ws = _FakeWebSocket(headers={})
        with pytest.raises(WebSocketException) as exc_info:
            ws_events._validate_origin(ws)
        assert not isinstance(exc_info.value, HTTPException)


# ---------------------------------------------------------------------------
# 6. Settings — allowed_origins misconfig warning in non-dev envs
# ---------------------------------------------------------------------------


class TestConfigAllowedOriginsValidator:
    """The pydantic ``model_validator`` must warn (not fail) when a
    non-``development`` env only lists localhost/loopback origins.

    ``allowed_origins`` defaults to local-dev origins; forgetting to set
    ``NEXUS_ALLOWED_ORIGINS`` in prod would silently leave the WebSocket
    origin allowlist pointed at localhost. The validator surfaces that
    via a structlog warning without rejecting the config.
    """

    @staticmethod
    def _http_origins(*hosts: str) -> list[str]:
        return [f"http://{h}:3000" for h in hosts]

    def test_warns_when_non_dev_and_only_localhost(self):
        from structlog.testing import capture_logs

        with capture_logs() as logs:
            Settings(
                app_env="production",
                allowed_origins=self._http_origins("localhost", "127.0.0.1"),
            )
        assert any(e["event"] == "config.allowed_origins_local_only" for e in logs)

    def test_warns_when_non_dev_and_only_loopback(self):
        from structlog.testing import capture_logs

        with capture_logs() as logs:
            Settings(
                app_env="staging",
                allowed_origins=["http://127.0.0.1:5173"],
            )
        assert any(e["event"] == "config.allowed_origins_local_only" for e in logs)

    def test_no_warning_when_dev_env_even_if_local_only(self):
        from structlog.testing import capture_logs

        with capture_logs() as logs:
            Settings(
                app_env="development",
                allowed_origins=self._http_origins("localhost"),
            )
        assert not any(e["event"] == "config.allowed_origins_local_only" for e in logs)

    def test_no_warning_when_non_dev_but_real_origin_present(self):
        from structlog.testing import capture_logs

        with capture_logs() as logs:
            Settings(
                app_env="production",
                allowed_origins=self._http_origins("localhost", "app.example.com"),
            )
        assert not any(e["event"] == "config.allowed_origins_local_only" for e in logs)

    def test_no_warning_when_non_dev_and_empty_origins(self):
        from structlog.testing import capture_logs

        with capture_logs() as logs:
            Settings(app_env="production", allowed_origins=[])
        assert not any(e["event"] == "config.allowed_origins_local_only" for e in logs)

    def test_warning_includes_env_and_origins(self):
        from structlog.testing import capture_logs

        origins = self._http_origins("localhost")
        with capture_logs() as logs:
            Settings(app_env="production", allowed_origins=origins)
        matches = [e for e in logs if e["event"] == "config.allowed_origins_local_only"]
        assert matches
        assert matches[0]["app_env"] == "production"
        assert matches[0]["allowed_origins"] == origins
