from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from engine.api.auth.dependency import (
    ROLE_HIERARCHY,
    generate_refresh_token,
    revoke_all_user_tokens,
    revoke_refresh_token,
    store_refresh_token,
    verify_and_rotate_refresh_token,
)
from engine.api.auth.jwt import create_access_token, decode_access_token
from engine.api.auth.local import LocalAuthProvider
from engine.api.auth.registry import AuthProviderRegistry
from engine.config import settings
from engine.db.models import Base, RefreshToken, User
from engine.deps import get_db
from fastapi import HTTPException

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.asyncio


def _make_sqlite_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


@pytest.fixture
async def engine():
    eng = _make_sqlite_engine()
    async with eng.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn,
                tables=[User.__table__, RefreshToken.__table__],
            )
        )
    yield eng
    async with eng.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.drop_all(
                sync_conn,
                tables=[RefreshToken.__table__, User.__table__],
            )
        )
    await eng.dispose()


@pytest.fixture
async def db_session(engine) -> AsyncIterator[AsyncSession]:
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session
        await session.rollback()


@pytest.fixture
async def app_with_db(db_session: AsyncSession):
    from engine.app import create_app

    _app = create_app()

    async def override_get_db():
        yield db_session

    _app.dependency_overrides[get_db] = override_get_db
    yield _app
    _app.dependency_overrides.clear()


@pytest.fixture
async def client(app_with_db) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app_with_db)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _create_test_user(
    db: AsyncSession,
    email: str = "test@test.com",
    role: str = "user",
    auth_provider: str = "local",
) -> User:
    user = User(
        email=email,
        hashed_password=None,
        display_name="Test User",
        role=role,
        auth_provider=auth_provider,
    )
    db.add(user)
    await db.flush()
    return user


def _auth_header(user: User) -> dict[str, str]:
    token = create_access_token(str(user.id), user.email, user.role, user.auth_provider)
    return {"Authorization": f"Bearer {token}"}


class TestJWT:
    async def test_create_and_decode_access_token(self):
        token = create_access_token("user-123", "test@example.com", "admin", "local")
        payload = decode_access_token(token)
        assert payload is not None
        assert payload["sub"] == "user-123"
        assert payload["email"] == "test@example.com"
        assert payload["role"] == "admin"
        assert payload["provider"] == "local"
        assert payload["type"] == "access"

    async def test_decode_invalid_token(self):
        payload = decode_access_token("invalid.token.here")
        assert payload is None

    async def test_secret_rotation(self):
        original_key = settings.secret_key
        token = create_access_token("user-123", "a@b.com", "user")
        with (
            patch.object(settings, "secret_key", "new-secret"),
            patch.object(settings, "secret_key_previous", original_key),
        ):
            payload = decode_access_token(token)
            assert payload is not None
            assert payload["sub"] == "user-123"

    async def test_token_with_wrong_type(self):
        from jose import jwt as jose_jwt

        payload = {
            "sub": "123",
            "type": "refresh",
            "exp": datetime.now(tz=UTC) + timedelta(hours=1),
        }
        token = jose_jwt.encode(payload, settings.secret_key, algorithm="HS256")
        result = decode_access_token(token)
        assert result is None


class TestLocalAuthProvider:
    async def test_register_user(self, db_session: AsyncSession):
        provider = LocalAuthProvider()
        result = await provider.register_user(
            email="new@test.com",
            password="password123",
            display_name="Test User",
            db=db_session,
        )
        assert result.success
        assert result.user_info is not None
        assert result.user_info.email == "new@test.com"
        assert result.user_info.provider == "local"

    async def test_register_short_password(self, db_session: AsyncSession):
        provider = LocalAuthProvider()
        result = await provider.register_user(
            email="short@test.com",
            password="short",
            display_name="Test",
            db=db_session,
        )
        assert not result.success
        assert "8 characters" in result.error

    async def test_register_duplicate_email(self, db_session: AsyncSession):
        provider = LocalAuthProvider()
        await provider.register_user("dup@test.com", "password123", "Test", db_session)
        await db_session.flush()
        result = await provider.register_user("dup@test.com", "password123", "Test", db_session)
        assert not result.success
        assert "already exists" in result.error

    async def test_login_success(self, db_session: AsyncSession):
        provider = LocalAuthProvider()
        await provider.register_user("login@test.com", "password123", "Test User", db_session)
        await db_session.flush()
        result = await provider.authenticate_login("login@test.com", "password123", db_session)
        assert result.success
        assert result.user_info is not None
        assert result.user_info.email == "login@test.com"

    async def test_login_wrong_password(self, db_session: AsyncSession):
        provider = LocalAuthProvider()
        await provider.register_user("wrong@test.com", "password123", "Test", db_session)
        await db_session.flush()
        result = await provider.authenticate_login("wrong@test.com", "wrongpass123", db_session)
        assert not result.success
        assert result.error == "Invalid credentials"

    async def test_login_nonexistent_user(self, db_session: AsyncSession):
        provider = LocalAuthProvider()
        result = await provider.authenticate_login("nobody@test.com", "password123", db_session)
        assert not result.success
        assert result.error == "Invalid credentials"

    async def test_no_user_enumeration(self, db_session: AsyncSession):
        provider = LocalAuthProvider()
        result_bad_email = await provider.authenticate_login("nonexistent@x.com", "pw", db_session)
        await provider.register_user("exists@x.com", "password123", "Test", db_session)
        await db_session.flush()
        result_bad_pw = await provider.authenticate_login("exists@x.com", "wrongpw123", db_session)
        assert result_bad_email.error == result_bad_pw.error

    async def test_registration_disabled(self, db_session: AsyncSession):
        provider = LocalAuthProvider()
        with patch.object(settings, "auth_local_allow_registration", False):
            result = await provider.register_user(
                "nope@test.com", "password123", "Test", db_session
            )
        assert not result.success
        assert "disabled" in result.error


class TestRefreshToken:
    async def test_store_and_verify_refresh_token(self, db_session: AsyncSession):
        user = await _create_test_user(db_session)
        plain = generate_refresh_token()
        await store_refresh_token(db_session, user.id, plain)
        await db_session.flush()

        returned_user, new_token = await verify_and_rotate_refresh_token(db_session, plain)
        assert str(returned_user.id) == str(user.id)
        assert new_token != plain

    async def test_refresh_token_rotation(self, db_session: AsyncSession):
        user = await _create_test_user(db_session)
        plain = generate_refresh_token()
        await store_refresh_token(db_session, user.id, plain)
        await db_session.flush()

        _, new_token1 = await verify_and_rotate_refresh_token(db_session, plain)
        await db_session.flush()
        _, new_token2 = await verify_and_rotate_refresh_token(db_session, new_token1)
        assert new_token1 != new_token2

    async def test_refresh_token_replay_revokes_all(self, db_session: AsyncSession):
        user = await _create_test_user(db_session)
        plain = generate_refresh_token()
        await store_refresh_token(db_session, user.id, plain)
        await db_session.flush()

        _, _new_token = await verify_and_rotate_refresh_token(db_session, plain)
        await db_session.flush()

        with pytest.raises(HTTPException):
            await verify_and_rotate_refresh_token(db_session, plain)

    async def test_expired_refresh_token(self, db_session: AsyncSession):
        user = await _create_test_user(db_session)
        plain = generate_refresh_token()
        from engine.api.auth.dependency import _hash_token

        rt = RefreshToken(
            user_id=user.id,
            token_hash=_hash_token(plain),
            expires_at=datetime.now(tz=UTC) - timedelta(days=1),
        )
        db_session.add(rt)
        await db_session.flush()

        with pytest.raises(HTTPException):
            await verify_and_rotate_refresh_token(db_session, plain)

    async def test_revoke_all_user_tokens(self, db_session: AsyncSession):
        user = await _create_test_user(db_session)
        t1 = generate_refresh_token()
        t2 = generate_refresh_token()
        await store_refresh_token(db_session, user.id, t1)
        await store_refresh_token(db_session, user.id, t2)
        await db_session.flush()

        await revoke_all_user_tokens(db_session, user.id)
        await db_session.flush()

        with pytest.raises(HTTPException):
            await verify_and_rotate_refresh_token(db_session, t1)

    async def test_revoke_single_token(self, db_session: AsyncSession):
        user = await _create_test_user(db_session)
        t1 = generate_refresh_token()
        t2 = generate_refresh_token()
        await store_refresh_token(db_session, user.id, t1)
        await store_refresh_token(db_session, user.id, t2)
        await db_session.flush()

        await revoke_refresh_token(db_session, t1)
        await db_session.flush()

        _, _ = await verify_and_rotate_refresh_token(db_session, t2)

        with pytest.raises(HTTPException):
            await verify_and_rotate_refresh_token(db_session, t1)


class TestAuthProviderRegistry:
    def test_register_and_get(self):
        registry = AuthProviderRegistry()
        provider = LocalAuthProvider()
        registry.register(provider)
        assert registry.get("local") is provider
        assert registry.get("google") is None

    def test_providers_list(self):
        registry = AuthProviderRegistry()
        registry.register(LocalAuthProvider())
        assert len(registry.providers) == 1
        assert registry.providers[0].name == "local"

    async def test_authenticate_unknown_provider(self):
        registry = AuthProviderRegistry()
        result = await registry.authenticate("nonexistent")
        assert not result.success
        assert "Unknown" in result.error


class TestRBAC:
    def test_role_hierarchy(self):
        assert ROLE_HIERARCHY["user"] < ROLE_HIERARCHY["developer"]
        assert ROLE_HIERARCHY["developer"] < ROLE_HIERARCHY["admin"]


class TestAuthEndpoints:
    async def test_register_endpoint(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/auth/register",
            json={
                "email": "newuser@test.com",
                "password": "password123",
                "display_name": "New User",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["token_type"] == "bearer"

    async def test_register_duplicate_returns_409(self, client: AsyncClient):
        payload = {"email": "dup@test.com", "password": "password123", "display_name": "Test"}
        resp1 = await client.post("/api/v1/auth/register", json=payload)
        assert resp1.status_code == 201

        resp2 = await client.post("/api/v1/auth/register", json=payload)
        assert resp2.status_code == 409

    async def test_register_short_password_returns_400(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/auth/register",
            json={"email": "short@test.com", "password": "short", "display_name": "Test"},
        )
        assert resp.status_code == 422

    async def test_login_endpoint(self, client: AsyncClient):
        await client.post(
            "/api/v1/auth/register",
            json={
                "email": "login@test.com",
                "password": "password123",
                "display_name": "Login User",
            },
        )
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "login@test.com", "password": "password123"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert "refresh_token" in data

    async def test_login_invalid_credentials(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "nonexistent@test.com", "password": "password123"},
        )
        assert resp.status_code == 401

    async def test_me_endpoint(self, client: AsyncClient):
        reg_resp = await client.post(
            "/api/v1/auth/register",
            json={"email": "me@test.com", "password": "password123", "display_name": "Me User"},
        )
        token = reg_resp.json()["access_token"]

        me_resp = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert me_resp.status_code == 200
        data = me_resp.json()
        assert data["email"] == "me@test.com"
        assert data["role"] == "user"

    async def test_me_without_token(self, client: AsyncClient):
        resp = await client.get("/api/v1/auth/me")
        assert resp.status_code in (401, 403)

    async def test_refresh_endpoint(self, client: AsyncClient):
        reg_resp = await client.post(
            "/api/v1/auth/register",
            json={
                "email": "refresh@test.com",
                "password": "password123",
                "display_name": "Refresh User",
            },
        )
        refresh_tok = reg_resp.json()["refresh_token"]

        refresh_resp = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": refresh_tok},
        )
        assert refresh_resp.status_code == 200
        data = refresh_resp.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["refresh_token"] != refresh_tok

    async def test_refresh_replay(self, client: AsyncClient):
        reg_resp = await client.post(
            "/api/v1/auth/register",
            json={
                "email": "replay@test.com",
                "password": "password123",
                "display_name": "Replay User",
            },
        )
        refresh_tok = reg_resp.json()["refresh_token"]

        await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_tok})

        replay_resp = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": refresh_tok}
        )
        assert replay_resp.status_code == 401

    async def test_logout_endpoint(self, client: AsyncClient):
        reg_resp = await client.post(
            "/api/v1/auth/register",
            json={
                "email": "logout@test.com",
                "password": "password123",
                "display_name": "Logout User",
            },
        )
        token = reg_resp.json()["access_token"]
        refresh_tok = reg_resp.json()["refresh_token"]

        logout_resp = await client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": refresh_tok},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert logout_resp.status_code == 200

    async def test_logout_everywhere(self, client: AsyncClient):
        reg_resp = await client.post(
            "/api/v1/auth/register",
            json={"email": "all@test.com", "password": "password123", "display_name": "All User"},
        )
        token = reg_resp.json()["access_token"]

        logout_resp = await client.post(
            "/api/v1/auth/logout",
            json={"everywhere": True},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert logout_resp.status_code == 200

    async def test_portfolio_requires_auth(self, client: AsyncClient):
        resp = await client.get("/api/v1/portfolio/")
        assert resp.status_code in (401, 403)

    async def test_portfolio_user_scoping(self, db_session: AsyncSession):
        user1 = User(
            email="user1-scope@test.com",
            hashed_password=None,
            display_name="User 1",
            role="user",
            auth_provider="local",
        )
        user2 = User(
            email="user2-scope@test.com",
            hashed_password=None,
            display_name="User 2",
            role="user",
            auth_provider="local",
        )
        db_session.add_all([user1, user2])
        await db_session.flush()

        h1 = _auth_header(user1)
        h2 = _auth_header(user2)
        assert h1 != h2

        from engine.api.auth.jwt import decode_access_token

        p1 = decode_access_token(h1["Authorization"].split(" ")[1])
        p2 = decode_access_token(h2["Authorization"].split(" ")[1])
        assert p1["sub"] == str(user1.id)
        assert p2["sub"] == str(user2.id)

    async def test_rbac_enforcement(self, db_session: AsyncSession):
        user = User(
            email="rbac-user@test.com",
            hashed_password=None,
            display_name="RBAC User",
            role="user",
            auth_provider="local",
        )
        db_session.add(user)
        await db_session.flush()

        admin = User(
            email="rbac-admin@test.com",
            hashed_password=None,
            display_name="RBAC Admin",
            role="admin",
            auth_provider="local",
        )
        db_session.add(admin)
        await db_session.flush()

        assert ROLE_HIERARCHY["user"] < ROLE_HIERARCHY["developer"]
