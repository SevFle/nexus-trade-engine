"""Tests for engine.api.routes.auth — authentication endpoints."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from engine.api.auth.base import AuthResult, UserInfo
from engine.api.auth.dependency import get_current_user
from engine.api.routes.auth import _aware, _build_token_response
from engine.app import create_app
from engine.config import settings
from engine.db.models import User
from engine.deps import get_db
from tests.conftest import _fake_authenticated_user


class TestAware:
    def test_naive_datetime_gets_utc(self):
        from datetime import UTC, datetime

        naive = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        result = _aware(naive)
        assert result.tzinfo is not None

    def test_aware_datetime_unchanged(self):
        from datetime import UTC, datetime

        aware = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        result = _aware(aware)
        assert result is aware


class TestBuildTokenResponse:
    def test_returns_correct_structure(self):
        resp = _build_token_response("access123", "refresh456")
        assert resp.access_token == "access123"
        assert resp.refresh_token == "refresh456"
        assert resp.token_type == "bearer"
        assert resp.expires_in == settings.jwt_access_token_expire_minutes * 60


class TestAuthRoutes:
    @pytest.mark.asyncio
    async def test_register_local_not_available(self, db_session):
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = _fake_authenticated_user

        mock_registry = MagicMock()
        mock_registry.get.return_value = None
        app.state.auth_registry = mock_registry

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/api/v1/auth/register",
                json={
                    "email": "new@example.com",
                    "password": "password123",
                    "display_name": "New User",
                },
            )
            assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_login_invalid_credentials(self, db_session):
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = _fake_authenticated_user

        mock_registry = MagicMock()
        mock_registry.authenticate = AsyncMock(
            return_value=AuthResult(success=False, error="Invalid credentials")
        )
        app.state.auth_registry = mock_registry

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/api/v1/auth/login",
                json={
                    "email": "test@example.com",
                    "password": "wrong",
                },
            )
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_get_me(self, db_session):
        app = create_app()

        async def override_get_db():
            yield db_session

        fake_user = _fake_authenticated_user()
        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: fake_user

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/auth/me")
            assert resp.status_code == 200
            data = resp.json()
            assert data["email"] == fake_user.email
            assert data["role"] == fake_user.role

    @pytest.mark.asyncio
    async def test_logout(self, db_session):
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = _fake_authenticated_user

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post("/api/v1/auth/logout")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "logged_out"

    @pytest.mark.asyncio
    async def test_refresh_invalid_token(self, db_session):
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = _fake_authenticated_user

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/api/v1/auth/refresh",
                json={"refresh_token": "invalid_token"},
            )
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_authorize_provider_not_configured(self, db_session):
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = _fake_authenticated_user

        mock_registry = MagicMock()
        mock_registry.get.return_value = None
        app.state.auth_registry = mock_registry

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/auth/nonexistent/authorize")
            assert resp.status_code == 404


class TestOAuthAuthorizeEndpoint:
    @pytest.mark.asyncio
    async def test_authorize_returns_url_and_state(self, db_session):
        app = create_app()

        async def override_get_db():
            yield db_session

        mock_provider = MagicMock()
        mock_provider.get_authorize_url = AsyncMock(
            return_value="https://accounts.google.com/o/oauth2/v2/auth?client_id=abc"
        )

        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_provider
        app.state.auth_registry = mock_registry
        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = _fake_authenticated_user

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/auth/google/authorize")
            assert resp.status_code == 200
            data = resp.json()
            assert "authorize_url" in data
            assert "state" in data
            assert len(data["state"]) > 0

    @pytest.mark.asyncio
    async def test_authorize_sets_state_cookie(self, db_session):
        app = create_app()

        async def override_get_db():
            yield db_session

        mock_provider = MagicMock()
        mock_provider.get_authorize_url = AsyncMock(
            return_value="https://github.com/login/oauth/authorize?client_id=xyz"
        )

        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_provider
        app.state.auth_registry = mock_registry
        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = _fake_authenticated_user

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/auth/github/authorize")
            assert resp.status_code == 200
            cookie_header = resp.headers.get("set-cookie", "")
            assert "oauth_state_github" in cookie_header

    @pytest.mark.asyncio
    async def test_authorize_provider_returns_empty_url(self, db_session):
        app = create_app()

        async def override_get_db():
            yield db_session

        mock_provider = MagicMock()
        mock_provider.get_authorize_url = AsyncMock(return_value="")

        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_provider
        app.state.auth_registry = mock_registry
        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = _fake_authenticated_user

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/auth/ldap/authorize")
            assert resp.status_code == 500


class TestOAuthCallbackEndpoint:
    @pytest.mark.asyncio
    async def test_callback_missing_state(self, db_session):
        app = create_app()

        mock_registry = MagicMock()
        app.state.auth_registry = mock_registry

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = _fake_authenticated_user

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/auth/google/callback?code=abc")
            assert resp.status_code == 401
            assert "Missing" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_callback_invalid_state(self, db_session):
        app = create_app()

        mock_registry = MagicMock()
        app.state.auth_registry = mock_registry

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = _fake_authenticated_user

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get(
                "/api/v1/auth/google/callback?code=abc&state=right-state",
                cookies={"oauth_state_google": "wrong-state"},
            )
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_callback_auth_failure(self, db_session):
        app = create_app()

        mock_registry = MagicMock()
        mock_registry.authenticate = AsyncMock(
            return_value=AuthResult(success=False, error="Google authentication failed")
        )
        app.state.auth_registry = mock_registry

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = _fake_authenticated_user

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get(
                "/api/v1/auth/google/callback?code=abc&state=valid-state",
                cookies={"oauth_state_google": "valid-state"},
            )
            assert resp.status_code == 401
            assert "Google authentication failed" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_callback_auth_succeeds_but_no_user_info(self, db_session):
        app = create_app()

        mock_registry = MagicMock()
        mock_registry.authenticate = AsyncMock(
            return_value=AuthResult(success=True, user_info=None)
        )
        app.state.auth_registry = mock_registry

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = _fake_authenticated_user

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get(
                "/api/v1/auth/google/callback?code=abc&state=valid-state",
                cookies={"oauth_state_google": "valid-state"},
            )
            assert resp.status_code == 500
            assert "no user info" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_callback_user_not_found_after_auth(self, db_session):
        app = create_app()

        user_info = UserInfo(
            external_id="ext-123",
            email="user@example.com",
            display_name="Test User",
            provider="google",
            roles=["user"],
        )
        mock_registry = MagicMock()
        mock_registry.authenticate = AsyncMock(
            return_value=AuthResult(success=True, user_info=user_info)
        )
        app.state.auth_registry = mock_registry

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = _fake_authenticated_user

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get(
                "/api/v1/auth/google/callback?code=abc&state=valid-state",
                cookies={"oauth_state_google": "valid-state"},
            )
            assert resp.status_code == 500
            assert "User not found" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_callback_full_flow_success(self, db_session):
        user_id = uuid.uuid4()
        user = User(
            id=user_id,
            email="oauthuser@example.com",
            display_name="OAuth User",
            is_active=True,
            role="user",
            auth_provider="google",
            external_id="ext-456",
        )
        db_session.add(user)
        await db_session.flush()

        user_info = UserInfo(
            external_id="ext-456",
            email="oauthuser@example.com",
            display_name="OAuth User",
            provider="google",
            roles=["user"],
        )

        app = create_app()

        mock_registry = MagicMock()
        mock_registry.authenticate = AsyncMock(
            return_value=AuthResult(success=True, user_info=user_info)
        )
        app.state.auth_registry = mock_registry

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = _fake_authenticated_user

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get(
                "/api/v1/auth/google/callback?code=abc&state=valid-state",
                cookies={"oauth_state_google": "valid-state"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert "access_token" in data
            assert "refresh_token" in data
            assert data["token_type"] == "bearer"

    @pytest.mark.asyncio
    async def test_callback_deletes_state_cookie(self, db_session):
        user_id = uuid.uuid4()
        user = User(
            id=user_id,
            email="oauthuser2@example.com",
            display_name="OAuth User 2",
            is_active=True,
            role="user",
            auth_provider="github",
            external_id="gh-789",
        )
        db_session.add(user)
        await db_session.flush()

        user_info = UserInfo(
            external_id="gh-789",
            email="oauthuser2@example.com",
            display_name="OAuth User 2",
            provider="github",
            roles=["user"],
        )

        app = create_app()

        mock_registry = MagicMock()
        mock_registry.authenticate = AsyncMock(
            return_value=AuthResult(success=True, user_info=user_info)
        )
        app.state.auth_registry = mock_registry

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = _fake_authenticated_user

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get(
                "/api/v1/auth/github/callback?code=abc&state=my-state",
                cookies={"oauth_state_github": "my-state"},
            )
            assert resp.status_code == 200
            cookie_header = resp.headers.get("set-cookie", "")
            assert "oauth_state_github" in cookie_header
            assert 'Max-Age=0' in cookie_header or 'max-age=0' in cookie_header.lower()

    @pytest.mark.asyncio
    async def test_callback_oidc_provider(self, db_session):
        user_id = uuid.uuid4()
        user = User(
            id=user_id,
            email="oidcuser@example.com",
            display_name="OIDC User",
            is_active=True,
            role="admin",
            auth_provider="oidc",
            external_id="oidc-999",
        )
        db_session.add(user)
        await db_session.flush()

        user_info = UserInfo(
            external_id="oidc-999",
            email="oidcuser@example.com",
            display_name="OIDC User",
            provider="oidc",
            roles=["admin"],
        )

        app = create_app()

        mock_registry = MagicMock()
        mock_registry.authenticate = AsyncMock(
            return_value=AuthResult(success=True, user_info=user_info)
        )
        app.state.auth_registry = mock_registry

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = _fake_authenticated_user

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get(
                "/api/v1/auth/oidc/callback?code=abc&state=oidc-state",
                cookies={"oauth_state_oidc": "oidc-state"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert "access_token" in data
            assert "refresh_token" in data
