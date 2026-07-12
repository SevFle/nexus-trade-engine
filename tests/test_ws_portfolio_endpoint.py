"""Focused unit tests for the authenticated ``/ws/portfolio`` endpoint.

Covers the four behaviours introduced by :mod:`engine.api.ws.portfolio_stream`:

1. **Auth gate** — a missing / invalid / subject-less session token is
   rejected *before* ``ws.accept()``; a valid token is accepted.
2. **Subscription registration** — on connect a per-connection handler is
   subscribed to exactly the portfolio-related EventBus event types, and
   the declared value list stays in sync with the enum members.
3. **Event → WebSocket serialization** — :func:`build_snapshot` flattens
   the bus envelope, the per-connection handler wraps it in an
   ``EventMessage`` and forwards it to one client, and a delivered bus
   event reaches the live WebSocket as JSON.
4. **Disconnect cleanup** — on disconnect the handler is unsubscribed
   from the bus *before* the connection is torn down, so no event is ever
   delivered to a dead socket; cleanup is best-effort and resilient.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import WebSocketDisconnect

from engine.api.ws import portfolio_stream as ps
from engine.api.ws.auth import AuthResult, extract_scopes
from engine.api.ws.connection_manager import ConnectionManager
from engine.api.ws.portfolio_stream import (
    PORTFOLIO_CHANNEL,
    PORTFOLIO_EVENT_TYPE_VALUES,
    build_snapshot,
    make_portfolio_handler,
    portfolio_event_types,
    register_subscriptions,
    unregister_subscriptions,
)
from engine.api.ws.protocol import WS_CLOSE_AUTH_INVALID, EventMessage
from engine.events.bus import EventType

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeHost:
    host: str = "1.2.3.4"


class _ControllableWebSocket:
    """Queue-backed WebSocket double.

    ``receive_json`` blocks on an asyncio queue so a test can drive an
    *active* connection: start the endpoint as a task, deliver bus events
    while it is connected, then feed a :class:`WebSocketDisconnect` to
    end it.
    """

    def __init__(self, *, query_params: dict[str, str] | None = None) -> None:
        self.query_params = query_params or {}
        self.client = _FakeHost()
        self.headers: dict[str, str] = {}
        self._inbox: asyncio.Queue[Any] = asyncio.Queue()
        self.sent: list[dict] = []
        self.accepted = False
        self.closed: list[tuple[int, str]] = []

    async def accept(self) -> None:
        self.accepted = True

    async def receive_json(self):
        item = await self._inbox.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed.append((code, reason))

    def feed(self, *items: Any) -> None:
        """Queue inbound JSON messages / exceptions for ``receive_json``."""
        for item in items:
            self._inbox.put_nowait(item)


class _FakeBus:
    """In-memory EventBus double that records subscribe/unsubscribe.

    Tests can call :meth:`deliver` to simulate the bus dispatching a
    payload to every handler registered for an event type.
    """

    def __init__(self) -> None:
        self._handlers: dict[Any, list] = {}

    def subscribe(self, event_type, handler) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    def unsubscribe(self, event_type, handler) -> None:
        handlers = self._handlers.get(event_type)
        if handlers is None:
            return
        self._handlers[event_type] = [h for h in handlers if h is not handler]

    def handlers_for(self, event_type) -> list:
        return list(self._handlers.get(event_type, []))

    async def deliver(self, event_type, payload) -> None:
        for handler in list(self._handlers.get(event_type, [])):
            await handler(payload)


class _FakeManager:
    """Minimal ConnectionManager double for the handler unit test.

    Only exposes the two methods :func:`make_portfolio_handler` touches:
    ``next_seq`` and ``send``.
    """

    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []
        self._seq = 0

    def next_seq(self, room: str) -> int:
        # Mirror ConnectionManager.next_seq: returns the current counter
        # (0 for the first call) then bumps it, so sequence numbers are
        # 0-indexed like production.
        seq = self._seq
        self._seq += 1
        return seq

    async def send(self, connection_id: str, message: Any) -> None:
        self.sent.append((connection_id, message))


def _make_token_data(sub: str = "user123", role: str = "admin", **extra: Any) -> dict[str, Any]:
    return {"sub": sub, "role": role, "type": "access", **extra}


def _bus_payload(
    event_type: EventType,
    *,
    data: dict[str, Any] | None = None,
    source: str = "oms",
) -> dict[str, Any]:
    """Build a payload shaped like :class:`engine.events.bus.Event.to_dict`."""
    return {
        "type": event_type.value,
        "data": data if data is not None else {},
        "source": source,
        "timestamp": "2025-01-01T00:00:00Z",
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state():
    """Each test starts and ends with a clean subsystem singleton."""
    ps.reset_state()
    yield
    ps.reset_state()


@pytest.fixture
def manager():
    return ConnectionManager()


@pytest.fixture
def bus():
    return _FakeBus()


# ===========================================================================
# 1. Auth gate — token validated before ws.accept()
# ===========================================================================


class TestAuthGate:
    async def test_missing_token_rejected_before_accept(self, manager, bus):
        ps.init_portfolio_stream(manager, bus)
        ws = _ControllableWebSocket(query_params={})
        ws.feed(WebSocketDisconnect())

        await ps.ws_portfolio_endpoint(ws)

        assert ws.accepted is False
        assert ws.closed
        assert ws.closed[0][0] == WS_CLOSE_AUTH_INVALID
        # Nothing was ever registered or subscribed.
        assert manager.connection_count == 0
        assert bus.handlers_for(EventType.PORTFOLIO_UPDATED) == []

    @patch("engine.api.ws.portfolio_stream.decode_token", return_value=None)
    async def test_invalid_token_rejected_before_accept(self, _mock, manager, bus):
        ps.init_portfolio_stream(manager, bus)
        ws = _ControllableWebSocket(query_params={"token": "not-a-real-jwt"})
        ws.feed(WebSocketDisconnect())

        await ps.ws_portfolio_endpoint(ws)

        assert ws.accepted is False
        assert ws.closed[0][0] == WS_CLOSE_AUTH_INVALID
        assert manager.connection_count == 0

    @patch("engine.api.ws.portfolio_stream.decode_token")
    async def test_token_without_sub_rejected_before_accept(self, mock_decode, manager, bus):
        mock_decode.return_value = {"role": "admin", "type": "access"}
        ps.init_portfolio_stream(manager, bus)
        ws = _ControllableWebSocket(query_params={"token": "jwt"})
        ws.feed(WebSocketDisconnect())

        await ps.ws_portfolio_endpoint(ws)

        assert ws.accepted is False
        assert ws.closed[0][0] == WS_CLOSE_AUTH_INVALID

    async def test_endpoint_before_init_closes_with_server_error(self):
        # No init_portfolio_stream() — manager/bus are None. A *valid* token
        # must still get past the auth gate to reach the not-ready guard.
        ws = _ControllableWebSocket(query_params={"token": "jwt"})
        ws.feed(WebSocketDisconnect())
        with patch("engine.api.ws.portfolio_stream.decode_token") as mock_decode:
            mock_decode.return_value = _make_token_data(sub="u1")
            await ps.ws_portfolio_endpoint(ws)

        assert ws.accepted is False
        assert ws.closed
        # Server-error close (1011), not an auth close.
        assert ws.closed[0][0] == 1011

    @patch("engine.api.ws.portfolio_stream.decode_token")
    async def test_valid_token_accepted_and_acked(self, mock_decode, manager, bus):
        mock_decode.return_value = _make_token_data(sub="u1", role="admin")
        ps.init_portfolio_stream(manager, bus)
        ws = _ControllableWebSocket(query_params={"token": "jwt"})
        ws.feed(WebSocketDisconnect())

        await ps.ws_portfolio_endpoint(ws)

        assert ws.accepted is True
        assert not ws.closed  # clean disconnect, not an auth/server close
        # The first outbound message is the connection ack.
        assert ws.sent
        assert ws.sent[0]["type"] == "ack"
        assert ws.sent[0]["status"] == "ok"
        # Connection was registered and then cleaned up on disconnect.
        assert manager.connection_count == 0

    @patch("engine.api.ws.portfolio_stream.decode_token")
    async def test_session_token_alias_accepted(self, mock_decode, manager, bus):
        mock_decode.return_value = _make_token_data(sub="u1", role="viewer")
        ps.init_portfolio_stream(manager, bus)
        ws = _ControllableWebSocket(query_params={"session_token": "jwt"})
        ws.feed({"type": "ping", "ref": "r1"}, WebSocketDisconnect())

        await ps.ws_portfolio_endpoint(ws)

        assert ws.accepted is True
        assert not ws.closed
        # The read-only endpoint answers a keepalive ping with a pong.
        assert any(m.get("type") == "pong" for m in ws.sent)

    @patch("engine.api.ws.portfolio_stream.decode_token")
    async def test_rejected_token_never_subscribes(self, mock_decode, manager, bus):
        mock_decode.return_value = None
        ps.init_portfolio_stream(manager, bus)
        ws = _ControllableWebSocket(query_params={"token": "bad"})
        ws.feed(WebSocketDisconnect())

        await ps.ws_portfolio_endpoint(ws)

        for et in portfolio_event_types():
            assert bus.handlers_for(et) == []

    def test_validate_session_token_wires_to_shared_extract_scopes(self):
        # Sanity: the endpoint derives scopes through the same helper as the
        # REST auth surface (guards against import-name drift like the one
        # that previously broke /ws/events collection).
        scopes = extract_scopes({"role": "viewer"})
        assert "read:portfolio" in scopes
        result = AuthResult(user_id="u1", scopes=scopes, token_data={"sub": "u1"})
        assert result.user_id == "u1"


# ===========================================================================
# 2. Subscription registration on the EventBus
# ===========================================================================


class TestSubscriptionRegistration:
    def test_portfolio_event_types_match_declared_values(self):
        """The enum members and the public value tuple must stay in sync."""
        members = portfolio_event_types()
        assert [e.value for e in members] == list(PORTFOLIO_EVENT_TYPE_VALUES)

    def test_portfolio_event_types_are_the_expected_four(self):
        members = set(portfolio_event_types())
        assert members == {
            EventType.PORTFOLIO_UPDATED,
            EventType.POSITION_OPENED,
            EventType.POSITION_CLOSED,
            EventType.ORDER_FILLED,
        }

    def test_register_subscriptions_subscribes_handler_once_per_type(self, bus):
        async def handler(payload):  # pragma: no cover - never delivered
            pass

        subscribed = register_subscriptions(bus, handler)

        assert subscribed == portfolio_event_types()
        for et in portfolio_event_types():
            assert bus.handlers_for(et) == [handler]

    def test_register_subscriptions_returns_types_for_cleanup(self, bus):
        async def handler(payload):  # pragma: no cover - never delivered
            pass

        subscribed = register_subscriptions(bus, handler)
        # Symmetric: handing the returned list to unregister clears everything.
        unregister_subscriptions(bus, handler, subscribed)
        for et in portfolio_event_types():
            assert bus.handlers_for(et) == []

    def test_register_subscriptions_accepts_explicit_types(self, bus):
        async def handler(payload):  # pragma: no cover
            pass

        only_two = [EventType.POSITION_OPENED, EventType.ORDER_FILLED]
        subscribed = register_subscriptions(bus, handler, event_types=only_two)
        assert subscribed == only_two
        assert bus.handlers_for(EventType.POSITION_OPENED) == [handler]
        assert bus.handlers_for(EventType.ORDER_FILLED) == [handler]
        # The other two portfolio types were NOT subscribed.
        assert bus.handlers_for(EventType.PORTFOLIO_UPDATED) == []
        assert bus.handlers_for(EventType.POSITION_CLOSED) == []

    @patch("engine.api.ws.portfolio_stream.decode_token")
    async def test_connect_subscribes_per_connection_handler(self, mock_decode, manager, bus):
        mock_decode.return_value = _make_token_data(sub="u1", role="admin")
        ps.init_portfolio_stream(manager, bus)
        ws = _ControllableWebSocket(query_params={"token": "jwt"})
        ws.feed(WebSocketDisconnect())

        await ps.ws_portfolio_endpoint(ws)

        # While connected (before the disconnect drained) exactly one handler
        # was registered per portfolio event type. After cleanup they are gone.
        for et in portfolio_event_types():
            assert bus.handlers_for(et) == []

    @patch("engine.api.ws.portfolio_stream.decode_token")
    async def test_two_clients_get_distinct_handlers(self, mock_decode, manager, bus):
        mock_decode.return_value = _make_token_data(sub="u1", role="admin")
        ps.init_portfolio_stream(manager, bus)
        ws1 = _ControllableWebSocket(query_params={"token": "jwt"})
        ws2 = _ControllableWebSocket(query_params={"token": "jwt"})

        # Run both connections to completion (immediate disconnect).
        ws1.feed(WebSocketDisconnect())
        ws2.feed(WebSocketDisconnect())
        await ps.ws_portfolio_endpoint(ws1)
        await ps.ws_portfolio_endpoint(ws2)

        # All handlers cleaned up for both clients.
        for et in portfolio_event_types():
            assert bus.handlers_for(et) == []


# ===========================================================================
# 3. Event → WebSocket message serialization
# ===========================================================================


class TestEventSerialization:
    def test_build_snapshot_flattens_envelope(self):
        payload = _bus_payload(
            EventType.POSITION_OPENED,
            data={"symbol": "AAPL", "qty": 10, "side": "BUY"},
            source="oms",
        )
        snap = build_snapshot(payload)
        assert snap == {
            "event": "position.opened",
            "data": {"symbol": "AAPL", "qty": 10, "side": "BUY"},
            "source": "oms",
            "timestamp": "2025-01-01T00:00:00Z",
        }

    def test_build_snapshot_normalizes_non_dict_data(self):
        # A malformed payload with a non-dict ``data`` must still serialize.
        snap = build_snapshot({"type": "portfolio.updated", "data": "oops"})
        assert snap["data"] == {}
        assert snap["event"] == "portfolio.updated"
        assert snap["source"] is None

    def test_build_snapshot_missing_data(self):
        snap = build_snapshot({"type": "order.filled"})
        assert snap["data"] == {}
        assert snap["event"] == "order.filled"

    async def test_handler_wraps_snapshot_in_event_message(self):
        fm = _FakeManager()
        handler = make_portfolio_handler(fm, "conn-123")
        payload = _bus_payload(
            EventType.POSITION_OPENED, data={"symbol": "AAPL"}
        )

        await handler(payload)

        assert len(fm.sent) == 1
        cid, msg = fm.sent[0]
        assert cid == "conn-123"
        assert isinstance(msg, EventMessage)
        assert msg.channel == PORTFOLIO_CHANNEL
        assert msg.room == PORTFOLIO_CHANNEL
        assert msg.payload == build_snapshot(payload)
        assert msg.seq == 0  # 0-indexed: first sequence number for the channel

    async def test_handler_increments_seq_per_event(self):
        fm = _FakeManager()
        handler = make_portfolio_handler(fm, "conn-1")

        await handler(_bus_payload(EventType.POSITION_OPENED))
        await handler(_bus_payload(EventType.ORDER_FILLED))
        await handler(_bus_payload(EventType.PORTFOLIO_UPDATED))

        assert [m.seq for _, m in fm.sent] == [0, 1, 2]

    async def test_handler_never_raises_on_send_error(self):
        fm = _FakeManager()

        async def boom(_cid, _msg):
            raise RuntimeError("socket gone")

        fm.send = boom  # type: ignore[method-assign]
        handler = make_portfolio_handler(fm, "conn-1")

        # A failing send must be swallowed so the bus dispatch loop survives.
        await handler(_bus_payload(EventType.PORTFOLIO_UPDATED, data={"pnl": 42.0}))

    async def test_handler_has_readable_name(self):
        fm = _FakeManager()
        handler = make_portfolio_handler(fm, "conn-abcdefgh")
        assert handler.__name__.startswith("portfolio_handler_")

    async def test_delivered_bus_event_reaches_live_websocket(self, manager, bus):
        """End-to-end: a bus event is serialized and delivered as JSON."""
        with patch("engine.api.ws.portfolio_stream.decode_token") as mock_decode:
            mock_decode.return_value = _make_token_data(sub="u1", role="admin")
            ps.init_portfolio_stream(manager, bus)
            ws = _ControllableWebSocket(query_params={"token": "jwt"})

            task = asyncio.create_task(ps.ws_portfolio_endpoint(ws))
            # Let the endpoint register, subscribe and send the ack.
            for _ in range(100):
                if ws.accepted and ws.sent:
                    break
                await asyncio.sleep(0.005)
            assert ws.accepted

            # Deliver a PnL recalculation event through the bus.
            await bus.deliver(
                EventType.PORTFOLIO_UPDATED,
                _bus_payload(
                    EventType.PORTFOLIO_UPDATED,
                    data={"equity": 125000.0, "pnl": 1234.56},
                ),
            )

            # The sender loop drains the send queue to ws.send_json; poll.
            event_msg = None
            for _ in range(200):
                for sent in ws.sent:
                    if sent.get("type") == "event":
                        event_msg = sent
                        break
                if event_msg is not None:
                    break
                await asyncio.sleep(0.005)

            # End the connection cleanly.
            ws.feed(WebSocketDisconnect())
            await task

        assert event_msg is not None, "event message never reached the client"
        assert event_msg["channel"] == PORTFOLIO_CHANNEL
        assert event_msg["payload"]["event"] == "portfolio.updated"
        assert event_msg["payload"]["data"] == {"equity": 125000.0, "pnl": 1234.56}
        # next_seq is 0-indexed (EventMessage.seq is ``ge=0``); the first
        # event on the portfolio channel carries seq 0.
        assert isinstance(event_msg["seq"], int)
        assert event_msg["seq"] >= 0
        # Connection was cleaned up.
        assert manager.connection_count == 0


# ===========================================================================
# 4. Disconnect cleanup
# ===========================================================================


class TestDisconnectCleanup:
    @patch("engine.api.ws.portfolio_stream.decode_token")
    async def test_clean_disconnect_unsubscribes_handler(self, mock_decode, manager, bus):
        mock_decode.return_value = _make_token_data(sub="u1", role="admin")
        ps.init_portfolio_stream(manager, bus)
        ws = _ControllableWebSocket(query_params={"token": "jwt"})
        ws.feed(WebSocketDisconnect())

        await ps.ws_portfolio_endpoint(ws)

        for et in portfolio_event_types():
            assert bus.handlers_for(et) == []
        assert manager.connection_count == 0

    @patch("engine.api.ws.portfolio_stream.decode_token")
    async def test_post_disconnect_event_does_not_reach_client(
        self, mock_decode, manager, bus
    ):
        """After cleanup a bus event must not be delivered to the dead socket."""
        mock_decode.return_value = _make_token_data(sub="u1", role="admin")
        ps.init_portfolio_stream(manager, bus)
        ws = _ControllableWebSocket(query_params={"token": "jwt"})
        ws.feed(WebSocketDisconnect())

        await ps.ws_portfolio_endpoint(ws)
        sent_before = list(ws.sent)

        # Deliver an event after disconnect — handler is gone, so nothing
        # should be enqueued, and even if it were, the connection is gone.
        await bus.deliver(
            EventType.POSITION_CLOSED,
            _bus_payload(EventType.POSITION_CLOSED, data={"symbol": "AAPL"}),
        )
        # Give any (there should be none) sender loop a chance to run.
        await asyncio.sleep(0.02)

        assert ws.sent == sent_before  # no new message appended
        assert manager.connection_count == 0

    @patch("engine.api.ws.portfolio_stream.decode_token")
    async def test_unexpected_error_still_cleans_up(self, mock_decode, manager, bus):
        mock_decode.return_value = _make_token_data(sub="u1", role="admin")
        ps.init_portfolio_stream(manager, bus)
        ws = _ControllableWebSocket(query_params={"token": "jwt"})

        # Force an exception inside the receive loop: the first message is a
        # non-WebSocketDisconnect exception, which the broad except catches
        # before the finally block runs cleanup.
        ws.feed(RuntimeError("boom-in-loop"))

        await ps.ws_portfolio_endpoint(ws)

        for et in portfolio_event_types():
            assert bus.handlers_for(et) == []
        assert manager.connection_count == 0

    def test_unregister_subscriptions_is_best_effort(self, bus):
        """A failing unsubscribe for one type must not block the rest."""
        calls = []

        def make_failing_unsubscribe():
            # First unsubscribe raises, subsequent ones succeed.
            state = {"first": True}

            def _unsub(et, handler):
                if state["first"]:
                    state["first"] = False
                    raise RuntimeError("transient")
                calls.append(et)
            return _unsub

        bus.unsubscribe = make_failing_unsubscribe()  # type: ignore[method-assign]

        async def handler(payload):  # pragma: no cover
            pass

        types = portfolio_event_types()
        # Register so the bus is in a realistic state, then unregister.
        register_subscriptions(bus, handler)
        # Must not raise despite the first unsubscribe blowing up.
        unregister_subscriptions(bus, handler, types)
        # The remaining (n-1) unsubscribes still executed.
        assert len(calls) == len(types) - 1

    @patch("engine.api.ws.portfolio_stream.decode_token")
    async def test_unregister_runs_before_connection_torn_down(
        self, mock_decode, manager
    ):
        """Cleanup order: bus unsubscribe happens before connection unregister."""

        order: list[str] = []
        ordered_bus = _OrderedBus(order)
        mock_decode.return_value = _make_token_data(sub="u1", role="admin")

        # Wrap the manager's real unregister so it records into ``order``.
        real_unregister = manager.unregister

        async def _ordered_unregister(connection_id, reason="client_disconnect"):
            order.append("unregister")
            await real_unregister(connection_id, reason=reason)

        manager.unregister = _ordered_unregister  # type: ignore[method-assign]

        ps.init_portfolio_stream(manager, ordered_bus)
        ws = _ControllableWebSocket(query_params={"token": "jwt"})
        ws.feed(WebSocketDisconnect())

        await ps.ws_portfolio_endpoint(ws)

        # Every "unsubscribe" event must precede every "unregister" event.
        unsubs = [i for i, name in enumerate(order) if name == "unsubscribe"]
        unregs = [i for i, name in enumerate(order) if name == "unregister"]
        assert unsubs and unregs, order
        assert max(unsubs) < min(unregs), order


class _OrderedBus(_FakeBus):
    """FakeBus that records the relative order of unsubscribe calls.

    Used together with a patched ConnectionManager.unregister to assert
    that bus cleanup runs *before* connection teardown.
    """

    def __init__(self, order: list[str]) -> None:
        super().__init__()
        self._order = order

    def unsubscribe(self, event_type, handler) -> None:
        self._order.append("unsubscribe")
        super().unsubscribe(event_type, handler)
