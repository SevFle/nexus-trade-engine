"""Tests for engine.api.routes.auth — authentication endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from engine.api.auth.base import AuthResult
from engine.api.auth.dependency import get_current_user
from engine.api.routes.auth import _aware, _build_token_response
from engine.app import create_app
from engine.config import settings
from engine.deps import get_db
from tests.conftest import _fake_authenticated_user


class TestAware:
    def test_naive_datetime_gets_utc(self):
        from datetime import datetime

        naive = datetime(2024, 1, 1, 12, 0, 0)
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
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()

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
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()

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
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()

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
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()

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
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()

        mock_registry = MagicMock()
        mock_registry.get.return_value = None
        app.state.auth_registry = mock_registry

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/auth/nonexistent/authorize")
            assert resp.status_code == 404
