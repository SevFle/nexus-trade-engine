"""Integration tests for the WebSocket API (SEV-275).

Covers:

- Auth via query-string ``?token=``, subprotocol header ``bearer.<jwt>``,
  and first-frame ``{"type": "auth", "token": ...}``.
- Channel subscription (``subscribe`` / ``unsubscribe``).
- Server-side event delivery through :class:`ConnectionManager.broadcast`.
- Heartbeat ping/pong.
- Graceful disconnect cleanup.
- Tenant isolation (events for user B don't leak to user A).

The tests build an isolated FastAPI app + SQLite DB and use
``starlette.testclient.TestClient.websocket_connect`` for the WS
handshake. No real network, no real Redis.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from fastapi import FastAPI
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    MetaData,
    String,
    Table,
    Text,
    Uuid,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from engine.api.auth.jwt import create_access_token
from engine.api.routes.websocket import router as websocket_router
from engine.api.websocket.manager import get_manager, reset_manager
from engine.db import session as session_module
from engine.db.models import User

# ---------------------------------------------------------------------------
# Test schema — minimal subset of the production schema that the WS
# route touches (users + refresh_tokens + api_keys). Mirrors the
# pattern used by tests/test_auth.py.
# ---------------------------------------------------------------------------


def _build_metadata() -> MetaData:
    md = MetaData()
    Table(
        "users",
        md,
        Column("id", Uuid, primary_key=True),
        Column("email", String(255), unique=True, nullable=False),
        Column("hashed_password", String(255), nullable=True),
        Column("display_name", String(100), nullable=False),
        Column("is_active", Boolean, default=True),
        Column("role", String(20), default="user"),
        Column("auth_provider", String(20), default="local"),
        Column("external_id", String(255), nullable=True),
        Column("mfa_enabled", Boolean, default=False, nullable=False),
        Column("mfa_secret_encrypted", Text, nullable=True),
        Column("mfa_backup_codes", JSON, nullable=True),
        Column("created_at", DateTime, default=datetime.now),
        Column("updated_at", DateTime, default=datetime.now, onupdate=datetime.now),
    )
    Table(
        "refresh_tokens",
        md,
        Column("id", Uuid, primary_key=True),
        Column(
            "user_id",
            Uuid,
            ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        Column("token_hash", String(64), unique=True, nullable=False),
        Column("expires_at", DateTime, nullable=False),
        Column("revoked_at", DateTime, nullable=True),
        Column("created_at", DateTime, default=datetime.now),
        Column("user_agent", String(512), nullable=True),
        Column("ip_address", String(45), nullable=True),
    )
    Table(
        "api_keys",
        md,
        Column("id", Uuid, primary_key=True),
        Column("user_id", Uuid, ForeignKey("users.id"), nullable=False, index=True),
        Column("token_hash", String(64), unique=True, nullable=False),
        Column("name", String(255), nullable=False),
        Column("scopes", JSON, nullable=False),
        Column("last_used_at", DateTime, nullable=True),
        Column("expires_at", DateTime, nullable=True),
        Column("created_at", DateTime, default=datetime.now),
        Column("revoked_at", DateTime, nullable=True),
    )
    return md


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def _set_test_secret():
    from engine.config import settings

    original = settings.secret_key
    settings.secret_key = "test-secret-key-for-jwt-signing-min-32-bytes-long"
    yield
    settings.secret_key = original


@pytest.fixture
async def ws_engine():
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(_build_metadata().create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def ws_session_factory(ws_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(ws_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
async def ws_user(ws_session_factory) -> tuple[uuid.UUID, str]:
    """Insert a single active user and return ``(user_id, jwt_token)``."""
    user_id = uuid.uuid4()
    email = f"ws-{user_id.hex[:8]}@example.com"
    async with ws_session_factory() as session:
        session.add(
            User(
                id=user_id,
                email=email,
                display_name="WS Test",
                is_active=True,
                role="user",
                auth_provider="local",
            )
        )
        await session.commit()
    token = create_access_token(
        sub=str(user_id), email=email, role="user", provider="local"
    )
    return user_id, token


@pytest.fixture
async def ws_user_b(ws_session_factory) -> tuple[uuid.UUID, str]:
    """Second user for tenant-isolation tests."""
    user_id = uuid.uuid4()
    email = f"wsb-{user_id.hex[:8]}@example.com"
    async with ws_session_factory() as session:
        session.add(
            User(
                id=user_id,
                email=email,
                display_name="WS Test B",
                is_active=True,
                role="user",
                auth_provider="local",
            )
        )
        await session.commit()
    token = create_access_token(
        sub=str(user_id), email=email, role="user", provider="local"
    )
    return user_id, token


@pytest.fixture
async def ws_app(ws_session_factory, _set_test_secret):
    """A minimal FastAPI app with just the WS router.

    We deliberately don't call ``engine.app.create_app`` because the
    production lifespan tries to sync legal documents against a richer
    schema than our test DB provides. The WS route doesn't need any
    of that — just the User table.
    """
    # Reset the singleton so we get a clean ConnectionManager per test.
    reset_manager()

    # Monkey-patch the module-level session factory so the WS route
    # resolves the user against our in-memory SQLite instance, not the
    # configured production URL. The route calls
    # ``get_session_factory()`` which reads the module global.
    session_module._session_factory = ws_session_factory
    try:
        app = FastAPI()
        app.include_router(websocket_router, prefix="/api/v1")
        yield app
    finally:
        session_module._session_factory = None


@pytest.fixture
def ws_client(ws_app):
    """Synchronous Starlette TestClient scoped to the ws_app."""
    with TestClient(ws_app) as client:
        yield client


# ---------------------------------------------------------------------------
# Auth path
# ---------------------------------------------------------------------------


class TestAuthHandshake:
    """Three valid auth paths + the rejection paths."""

    def test_first_frame_auth_succeeds(self, ws_client, ws_user):
        _uid, token = ws_user
        with ws_client.websocket_connect("/api/v1/ws") as ws:
            ws.send_json({"type": "auth", "token": token})
            ready = ws.receive_json()
            assert ready["type"] == "connection.ready"
            assert ready["heartbeat_seconds"] > 0
            ok = ws.receive_json()
            assert ok["type"] == "auth.ok"
            assert ok["user_id"] == str(_uid)

    def test_query_token_auth_succeeds(self, ws_client, ws_user):
        _uid, token = ws_user
        with ws_client.websocket_connect(f"/api/v1/ws?token={token}") as ws:
            ready = ws.receive_json()
            assert ready["type"] == "connection.ready"
            ok = ws.receive_json()
            assert ok["type"] == "auth.ok"

    def test_subprotocol_bearer_auth_succeeds(self, ws_client, ws_user):
        _uid, token = ws_user
        subproto = f"bearer.{token}"
        with ws_client.websocket_connect(
            "/api/v1/ws", subprotocols=[subproto]
        ) as ws:
            ready = ws.receive_json()
            assert ready["type"] == "connection.ready"
            ok = ws.receive_json()
            assert ok["type"] == "auth.ok"

    def test_invalid_token_closes_4401(self, ws_client):
        # An obviously-bogus token fails JWT decode AND isn't a valid
        # engine API key → server closes with 4401. Starlette's
        # TestClient surfaces the close on the next receive, not on
        # context-manager exit.
        with ws_client.websocket_connect("/api/v1/ws?token=not-a-token") as ws:
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
            assert exc_info.value.code == 4401

    def test_missing_token_first_frame_closes_4400(self, ws_client):
        with ws_client.websocket_connect("/api/v1/ws") as ws:
            ws.send_json({"type": "subscribe", "channels": ["portfolio"]})
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
            assert exc_info.value.code == 4400


# ---------------------------------------------------------------------------
# Subscription path
# ---------------------------------------------------------------------------


class TestSubscription:
    def test_subscribe_ack_lists_channels(self, ws_client, ws_user):
        _uid, token = ws_user
        with ws_client.websocket_connect(f"/api/v1/ws?token={token}") as ws:
            _consume_handshake(ws)
            ws.send_json({"type": "subscribe", "channels": ["portfolio", "order"]})
            ack = ws.receive_json()
            assert ack["type"] == "subscribed"
            assert set(ack["channels"]) == {"portfolio", "order"}

    def test_subscribe_drops_unknown_channels(self, ws_client, ws_user):
        _uid, token = ws_user
        with ws_client.websocket_connect(f"/api/v1/ws?token={token}") as ws:
            _consume_handshake(ws)
            ws.send_json(
                {"type": "subscribe", "channels": ["portfolio", "wizardry"]}
            )
            ack = ws.receive_json()
            assert ack["type"] == "subscribed"
            assert ack["channels"] == ["portfolio"]

    def test_unsubscribe_removes_channel(self, ws_client, ws_user):
        _uid, token = ws_user
        with ws_client.websocket_connect(f"/api/v1/ws?token={token}") as ws:
            _consume_handshake(ws)
            ws.send_json({"type": "subscribe", "channels": ["portfolio", "order"]})
            _consume(ws)
            ws.send_json({"type": "unsubscribe", "channels": ["portfolio"]})
            ack = ws.receive_json()
            assert ack["type"] == "unsubscribed"
            assert ack["channels"] == ["order"]

    def test_subscribe_correlation_id_echoed(self, ws_client, ws_user):
        _uid, token = ws_user
        with ws_client.websocket_connect(f"/api/v1/ws?token={token}") as ws:
            _consume_handshake(ws)
            ws.send_json(
                {
                    "type": "subscribe",
                    "channels": ["portfolio"],
                    "correlation_id": "abc-123",
                }
            )
            ack = ws.receive_json()
            assert ack["correlation_id"] == "abc-123"


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


class TestHeartbeat:
    def test_client_ping_gets_pong(self, ws_client, ws_user):
        _uid, token = ws_user
        with ws_client.websocket_connect(f"/api/v1/ws?token={token}") as ws:
            _consume_handshake(ws)
            ws.send_json({"type": "ping"})
            pong = ws.receive_json()
            assert pong["type"] == "pong"

    def test_client_ping_with_correlation_id(self, ws_client, ws_user):
        _uid, token = ws_user
        with ws_client.websocket_connect(f"/api/v1/ws?token={token}") as ws:
            _consume_handshake(ws)
            ws.send_json({"type": "ping", "correlation_id": "rt-42"})
            pong = ws.receive_json()
            assert pong["type"] == "pong"
            assert pong["correlation_id"] == "rt-42"


# ---------------------------------------------------------------------------
# Message delivery (end-to-end through ConnectionManager.broadcast)
# ---------------------------------------------------------------------------


class TestMessageDelivery:
    def test_delivery_via_portal(self, ws_client, ws_user):
        """End-to-end delivery: client subscribes, server broadcasts,
        client receives envelope."""
        uid, token = ws_user
        manager = get_manager()
        with ws_client.websocket_connect(f"/api/v1/ws?token={token}") as ws:
            _consume_handshake(ws)
            ws.send_json({"type": "subscribe", "channels": ["portfolio"]})
            _consume(ws)

            # Run the broadcast on the TestClient's portal (the same
            # event loop the WS handler is using).
            _broadcast(ws_client, manager, uid)
            env = ws.receive_json()
            assert env["channel"] == "portfolio"
            assert env["event"] == "portfolio.updated"
            assert env["data"]["v"] == 1
            assert env["seq"] == 0
            assert env["version"]
            assert env["correlation_id"]

    def test_correlation_id_round_trip(self, ws_client, ws_user):
        uid, token = ws_user
        manager = get_manager()
        with ws_client.websocket_connect(f"/api/v1/ws?token={token}") as ws:
            _consume_handshake(ws)
            ws.send_json({"type": "subscribe", "channels": ["portfolio"]})
            _consume(ws)

            _broadcast(ws_client, manager, uid, correlation_id="server-cid-7")
            env = ws.receive_json()
            assert env["correlation_id"] == "server-cid-7"

    def test_unsubscribed_does_not_receive(self, ws_client, ws_user):
        uid, token = ws_user
        manager = get_manager()
        with ws_client.websocket_connect(f"/api/v1/ws?token={token}") as ws:
            _consume_handshake(ws)
            ws.send_json({"type": "subscribe", "channels": ["order"]})
            _consume(ws)

            _broadcast(ws_client, manager, uid, topic="portfolio")
            # Now send a second broadcast on a subscribed channel so
            # the receive loop has a deterministic terminator.
            _broadcast(ws_client, manager, uid, topic="order")
            env = ws.receive_json()
            assert env["channel"] == "order"

    def test_seq_monotonic_for_multiple_events(self, ws_client, ws_user):
        uid, token = ws_user
        manager = get_manager()
        with ws_client.websocket_connect(f"/api/v1/ws?token={token}") as ws:
            _consume_handshake(ws)
            ws.send_json({"type": "subscribe", "channels": ["portfolio"]})
            _consume(ws)

            for i in range(3):
                _broadcast(
                    ws_client,
                    manager,
                    uid,
                    payload={"event_type": "portfolio.updated", "i": i},
                )

            seqs = []
            for _ in range(3):
                env = ws.receive_json()
                seqs.append(env["seq"])
            assert seqs == [0, 1, 2]


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    def test_event_for_user_b_not_delivered_to_a(
        self, ws_client, ws_user, ws_user_b
    ):
        uid_a, token_a = ws_user
        uid_b, _token_b = ws_user_b
        manager = get_manager()
        with ws_client.websocket_connect(f"/api/v1/ws?token={token_a}") as ws:
            _consume_handshake(ws)
            ws.send_json({"type": "subscribe", "channels": ["portfolio"]})
            _consume(ws)

            # Push an event for user B → user A must not see it.
            _broadcast(ws_client, manager, uid_b, topic="portfolio")
            # Push an event for user A → that one must arrive.
            _broadcast(
                ws_client,
                manager,
                uid_a,
                topic="portfolio",
                payload={"for": "a"},
            )
            env = ws.receive_json()
            assert env["data"]["for"] == "a"


# ---------------------------------------------------------------------------
# Disconnect handling
# ---------------------------------------------------------------------------


class TestDisconnectHandling:
    def test_unsubscribe_stops_flow(self, ws_client, ws_user):
        uid, token = ws_user
        manager = get_manager()
        with ws_client.websocket_connect(f"/api/v1/ws?token={token}") as ws:
            _consume_handshake(ws)
            ws.send_json({"type": "subscribe", "channels": ["portfolio"]})
            _consume(ws)

            _broadcast(ws_client, manager, uid)
            env = ws.receive_json()
            assert env["channel"] == "portfolio"

            ws.send_json({"type": "unsubscribe", "channels": ["portfolio"]})
            _consume(ws)

            # Push another event — must not arrive. Follow up with a
            # ping so the receive loop has a deterministic terminator.
            _broadcast(ws_client, manager, uid)
            ws.send_json({"type": "ping"})
            pong = ws.receive_json()
            assert pong["type"] == "pong"

    def test_unknown_frame_returns_error(self, ws_client, ws_user):
        _uid, token = ws_user
        with ws_client.websocket_connect(f"/api/v1/ws?token={token}") as ws:
            _consume_handshake(ws)
            ws.send_json({"type": "wat"})
            err = ws.receive_json()
            assert err["type"] == "error"
            assert err["code"] == "unknown_message_type"

    def test_malformed_subscribe_returns_error(self, ws_client, ws_user):
        _uid, token = ws_user
        with ws_client.websocket_connect(f"/api/v1/ws?token={token}") as ws:
            _consume_handshake(ws)
            ws.send_json({"type": "subscribe", "channels": "not-a-list"})
            err = ws.receive_json()
            assert err["type"] == "error"
            assert err["code"] == "invalid_frame"

    def test_disconnect_cleans_up_manager(self, ws_client, ws_user):
        uid, token = ws_user
        manager = get_manager()
        with ws_client.websocket_connect(f"/api/v1/ws?token={token}") as ws:
            _consume_handshake(ws)
            ws.send_json({"type": "subscribe", "channels": ["portfolio"]})
            _consume(ws)
            assert manager.user_connection_count(uid) == 1
        # After disconnect, the manager should report 0 connections.
        # The cleanup is async; spin briefly.
        import time

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if manager.user_connection_count(uid) == 0:
                break
            time.sleep(0.05)
        assert manager.user_connection_count(uid) == 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _consume_handshake(ws) -> None:
    """Read the ``connection.ready`` + ``auth.ok`` frames."""
    ready = ws.receive_json()
    assert ready["type"] == "connection.ready", ready
    ok = ws.receive_json()
    assert ok["type"] == "auth.ok", ok


def _consume(ws) -> None:
    """Read a single frame and discard."""
    ws.receive_json()


async def _broadcast_via_manager(
    manager,
    uid,
    *,
    topic: str = "portfolio",
    payload: dict | None = None,
    event: str = "portfolio.updated",
    correlation_id: str = "cid",
) -> None:
    """Helper that runs ``manager.broadcast`` as a coroutine so we can
    submit it to the TestClient's portal."""
    await manager.broadcast(
        user_id=uid,
        topic=topic,
        payload=payload or {"v": 1},
        event=event,
        correlation_id=correlation_id,
    )


def _broadcast(ws_client, manager, uid, **kwargs) -> None:
    """Schedule a broadcast on the TestClient's portal (same event loop
    the WS handler runs on) and block the calling thread until it
    completes.

    ``portal.call`` expects a *callable*; we hand it a zero-arg lambda
    that returns the coroutine we want awaited.
    """
    ws_client.portal.call(lambda: _broadcast_via_manager(manager, uid, **kwargs))
