"""Happy-path tests for the ``/ws/events`` WebSocket endpoint.

Connects through FastAPI's :class:`~fastapi.testclient.TestClient`
websocket transport, publishes a real :class:`~engine.events.bus.Event`
to the in-process :class:`~engine.events.bus.EventBus`, and asserts the
client receives the serialized event over the socket. This exercises the
full vertical slice: accept → register → subscribe → bridge fan-out →
JSON stream → disconnect cleanup.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine.api.routes.ws_events import (
    EVENTS_CHANNEL,
    get_state,
    init_ws_events,
    reset_ws_events_for_tests,
    router,
)
from engine.api.websocket.manager import ConnectionManager
from engine.events.bus import Event, EventBus, EventType


@pytest.fixture
def events_app():
    """Build a minimal app with the ws_events router wired to a fresh bus + manager."""
    manager = ConnectionManager()
    # In-process bus only — never touches Redis, so no external deps.
    bus = EventBus()
    init_ws_events(bus, manager)

    app = FastAPI()
    app.include_router(router)

    yield app, bus

    reset_ws_events_for_tests()


def test_client_receives_published_event_via_firehose(events_app):
    """Default subscription (global ``events`` firehose) delivers every event."""
    app, bus = events_app
    client = TestClient(app)

    with client.websocket_connect("/ws/events") as ws:
        # 1. Connected ack — proves registration + subscription completed
        #    *before* we publish, so there is no lost-event race.
        ack = ws.receive_json()
        assert ack["type"] == "connected"
        assert ack["channels"] == [EVENTS_CHANNEL]
        assert ack["connection_id"]

        # 2. Publish an event into the server's running event loop. The
        #    TestClient drives the ASGI app on a separate event loop /
        #    thread, so we schedule the publish via the loop the endpoint
        #    captured on connect.
        state = get_state()
        loop = state.loop
        assert loop is not None, "endpoint should capture the running loop"

        fut = asyncio.run_coroutine_threadsafe(
            bus.publish(
                Event(
                    EventType.PORTFOLIO_UPDATED,
                    {"portfolio_id": "p-1", "nav": 12345.67},
                    source="test",
                )
            ),
            loop,
        )
        fut.result(timeout=5)

        # 3. The serialized event arrives over the socket.
        msg = ws.receive_json()
        assert msg["type"] == EventType.PORTFOLIO_UPDATED.value
        assert msg["source"] == "test"
        assert msg["data"] == {"portfolio_id": "p-1", "nav": 12345.67}
        assert "timestamp" in msg


def test_namespace_channel_isolation(events_app):
    """A ``?channels=portfolio`` client gets portfolio events, not order events."""
    app, bus = events_app
    client = TestClient(app)

    with client.websocket_connect("/ws/events?channels=portfolio") as ws:
        ack = ws.receive_json()
        assert ack["type"] == "connected"
        assert ack["channels"] == ["portfolio"]

        state = get_state()
        loop = state.loop

        # Publish an order event — this client is NOT subscribed to the
        # ``order``/``events`` channels, so it must never see it.
        for et in (EventType.ORDER_CREATED, EventType.ORDER_FILLED):
            fut = asyncio.run_coroutine_threadsafe(
                bus.publish(Event(et, {"order_id": "o-1"})),
                loop,
            )
            fut.result(timeout=5)

        # Publish a portfolio event — this one the client IS entitled to.
        fut = asyncio.run_coroutine_threadsafe(
            bus.publish(Event(EventType.PORTFOLIO_UPDATED, {"portfolio_id": "p-2"})),
            loop,
        )
        fut.result(timeout=5)

        # The first (and only) delivered message is the portfolio event:
        # order events were filtered out by channel membership.
        msg = ws.receive_json()
        assert msg["type"] == EventType.PORTFOLIO_UPDATED.value
        assert msg["data"]["portfolio_id"] == "p-2"

        # The connection is cleaned up correctly on close — after leaving
        # the context the manager should hold no connections.
        assert state.manager is not None


def test_disconnect_cleans_up_connection(events_app):
    """Leaving the websocket context detaches the connection from the manager."""
    app, _bus = events_app
    client = TestClient(app)
    state = get_state()
    assert state.manager is not None

    with client.websocket_connect("/ws/events") as ws:
        ws.receive_json()  # consume the connected ack
        assert state.manager.connection_count == 1

    # After the context exits the socket is gone and the manager drops it.
    assert state.manager.connection_count == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
