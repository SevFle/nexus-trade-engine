"""Integration tests for the auth system — register, login, refresh, RBAC, user scoping."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Boolean, Column, DateTime, MetaData, String, Table, Uuid
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from engine.api.auth.jwt import create_access_token, decode_token
from engine.app import create_app
from engine.config import settings
from engine.deps import get_db

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.fixture(scope="module")
def _set_test_settings():
    original = settings.secret_key
    settings.secret_key = "test-secret-key-for-integration-tests"
    yield
    settings.secret_key = original


@pytest.fixture
async def auth_engine():
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)

    metadata = MetaData()
    Table(
        "users",
        metadata,
        Column("id", Uuid, primary_key=True),
        Column("email", String(255), unique=True, nullable=False),
        Column("hashed_password", String(255), nullable=True),
        Column("display_name", String(100), nullable=False),
        Column("is_active", Boolean, default=True),
        Column("role", String(20), default="user"),
        Column("auth_provider", String(20), default="local"),
        Column("external_id", String(255), nullable=True),
        Column("created_at", DateTime, default=datetime.now),
        Column("updated_at", DateTime, default=datetime.now, onupdate=datetime.now),
    )
    Table(
        "refresh_tokens",
        metadata,
        Column("id", Uuid, primary_key=True),
        Column("user_id", Uuid, nullable=False, index=True),
        Column("token_hash", String(64), unique=True, nullable=False),
        Column("expires_at", DateTime, nullable=False),
        Column("revoked_at", DateTime, nullable=True),
        Column("created_at", DateTime, default=datetime.now),
        Column("user_agent", String(512), nullable=True),
        Column("ip_address", String(45), nullable=True),
    )

    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def auth_db(auth_engine) -> AsyncIterator[AsyncSession]:
    session_factory = async_sessionmaker(auth_engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session


@pytest.fixture
async def auth_client(auth_db: AsyncSession) -> AsyncIterator[AsyncClient]:

    from engine.api.auth.local import LocalAuthProvider
    from engine.api.auth.registry import AuthProviderRegistry

    app = create_app()

    registry = AuthProviderRegistry()
    registry.register(LocalAuthProvider())
    app.state.auth_registry = registry

    async def override_get_db():
        yield auth_db

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
def registered_user() -> dict:
    email = f"test-{uuid.uuid4().hex[:8]}@example.com"
    return {
        "email": email,
        "password": "testpassword123",
        "display_name": "Test User",
    }


@pytest.fixture
async def auth_tokens(auth_client: AsyncClient, registered_user: dict) -> dict:
    resp = await auth_client.post("/api/v1/auth/register", json=registered_user)
    assert resp.status_code == 201
    return resp.json()


class TestRegister:
    async def test_register_success(self, auth_client: AsyncClient, registered_user: dict):
        resp = await auth_client.post("/api/v1/auth/register", json=registered_user)
        assert resp.status_code == 201
        data = resp.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["token_type"] == "bearer"

    async def test_register_duplicate_email(self, auth_client: AsyncClient, registered_user: dict):
        await auth_client.post("/api/v1/auth/register", json=registered_user)
        resp = await auth_client.post("/api/v1/auth/register", json=registered_user)
        assert resp.status_code == 409

    async def test_register_short_password(self, auth_client: AsyncClient):
        resp = await auth_client.post(
            "/api/v1/auth/register",
            json={"email": f"short-{uuid.uuid4().hex[:8]}@example.com", "password": "short"},
        )
        assert resp.status_code == 409

    async def test_register_invalid_email(self, auth_client: AsyncClient):
        resp = await auth_client.post(
            "/api/v1/auth/register",
            json={"email": "not-an-email", "password": "validpassword123"},
        )
        assert resp.status_code == 422


class TestLogin:
    async def test_login_success(self, auth_client: AsyncClient, registered_user: dict):
        await auth_client.post("/api/v1/auth/register", json=registered_user)
        resp = await auth_client.post(
            "/api/v1/auth/login",
            json={"email": registered_user["email"], "password": registered_user["password"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert "refresh_token" in data

    async def test_login_wrong_password(self, auth_client: AsyncClient, registered_user: dict):
        await auth_client.post("/api/v1/auth/register", json=registered_user)
        resp = await auth_client.post(
            "/api/v1/auth/login",
            json={"email": registered_user["email"], "password": "wrongpassword"},
        )
        assert resp.status_code == 401

    async def test_login_nonexistent_email(self, auth_client: AsyncClient):
        resp = await auth_client.post(
            "/api/v1/auth/login",
            json={"email": "nonexistent@example.com", "password": "whatever123456"},
        )
        assert resp.status_code == 401


class TestTokenRefresh:
    async def test_refresh_success(self, auth_client: AsyncClient, auth_tokens: dict):
        resp = await auth_client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": auth_tokens["refresh_token"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["refresh_token"] != auth_tokens["refresh_token"]

    async def test_refresh_rotation_one_time_use(
        self, auth_client: AsyncClient, auth_tokens: dict
    ):
        resp1 = await auth_client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": auth_tokens["refresh_token"]},
        )
        assert resp1.status_code == 200

        resp2 = await auth_client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": auth_tokens["refresh_token"]},
        )
        assert resp2.status_code == 401

    async def test_refresh_invalid_token(self, auth_client: AsyncClient):
        resp = await auth_client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": "invalid-token"},
        )
        assert resp.status_code == 401


class TestMe:
    async def test_me_success(self, auth_client: AsyncClient, auth_tokens: dict):
        resp = await auth_client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {auth_tokens['access_token']}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "email" in data
        assert data["role"] == "user"
        assert data["auth_provider"] == "local"

    async def test_me_no_token(self, auth_client: AsyncClient):
        resp = await auth_client.get("/api/v1/auth/me")
        assert resp.status_code in (401, 403)

    async def test_me_invalid_token(self, auth_client: AsyncClient):
        resp = await auth_client.get(
            "/api/v1/auth/me",
            headers={"Authorization": "Bearer invalid-token"},
        )
        assert resp.status_code == 401


class TestLogout:
    async def test_logout_success(self, auth_client: AsyncClient, auth_tokens: dict):
        resp = await auth_client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": auth_tokens["refresh_token"]},
            headers={"Authorization": f"Bearer {auth_tokens['access_token']}"},
        )
        assert resp.status_code == 200

    async def test_logout_all_sessions(self, auth_client: AsyncClient, auth_tokens: dict):
        resp = await auth_client.post(
            "/api/v1/auth/logout",
            headers={"Authorization": f"Bearer {auth_tokens['access_token']}"},
        )
        assert resp.status_code == 200


class TestJWT:
    def test_create_and_decode_token(self, _set_test_settings):
        token = create_access_token(
            sub=str(uuid.uuid4()),
            email="test@example.com",
            role="user",
            provider="local",
        )
        payload = decode_token(token)
        assert payload is not None
        assert payload["email"] == "test@example.com"
        assert payload["role"] == "user"
        assert payload["type"] == "access"

    def test_decode_expired_token(self, _set_test_settings):
        token = create_access_token(
            sub=str(uuid.uuid4()),
            email="test@example.com",
            role="user",
            expires_delta=timedelta(seconds=-1),
        )
        payload = decode_token(token)
        assert payload is None

    def test_decode_invalid_token(self, _set_test_settings):
        payload = decode_token("totally-invalid-token")
        assert payload is None


class TestRBAC:
    async def test_user_cannot_install_marketplace(
        self, auth_client: AsyncClient, auth_tokens: dict
    ):
        resp = await auth_client.post(
            "/api/v1/marketplace/install",
            json={"strategy_id": "test-strategy"},
            headers={"Authorization": f"Bearer {auth_tokens['access_token']}"},
        )
        assert resp.status_code == 403


class TestProtectedRoutes:
    async def test_backtest_requires_auth(self, auth_client: AsyncClient):
        resp = await auth_client.post(
            "/api/v1/backtest/run",
            json={
                "strategy_name": "test",
                "symbol": "AAPL",
                "start_date": "2024-01-01",
                "end_date": "2024-12-31",
            },
        )
        assert resp.status_code in (401, 403)

    async def test_backtest_with_auth(self, auth_client: AsyncClient, auth_tokens: dict):
        resp = await auth_client.post(
            "/api/v1/backtest/run",
            json={
                "strategy_name": "test",
                "symbol": "AAPL",
                "start_date": "2024-01-01",
                "end_date": "2024-12-31",
            },
            headers={"Authorization": f"Bearer {auth_tokens['access_token']}"},
        )
        assert resp.status_code == 200
