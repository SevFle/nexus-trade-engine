"""Tests for engine.api.routes.auth — authentication endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

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

    @pytest.mark.asyncio
    async def test_authorize_persists_authoritative_state_from_tuple(self, db_session):
        # When a provider exposes ``get_authorize_url_with_state`` and returns a
        # state that differs from the one the route minted (e.g. the provider
        # mints its own CSRF token), the route MUST surface and persist THAT
        # authoritative state -- the value embedded in the URL the IdP echoes
        # back -- so the callback's session-cookie check succeeds.
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = _fake_authenticated_user

        class StubStateProvider:
            name = "stub"

            def get_authorize_url_with_state(self, state: str = "") -> tuple[str, str]:
                authoritative = "provider-issued-state"
                url = f"https://idp.example.com/auth?state={authoritative}"
                return url, authoritative

        mock_registry = MagicMock()
        mock_registry.get.return_value = StubStateProvider()
        app.state.auth_registry = mock_registry

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/auth/stub/authorize")
            assert resp.status_code == 200, resp.text
            data = resp.json()

            # The route surfaces the provider-issued state, not a locally
            # minted token, and it is the one embedded in the authorize URL.
            assert data["state"] == "provider-issued-state"
            assert "provider-issued-state" in data["authorize_url"]
            # And it is persisted in the session cookie for callback validation.
            assert resp.cookies.get("oauth_state_stub") == "provider-issued-state"

    @pytest.mark.asyncio
    async def test_authorize_persists_minted_state_for_string_provider(self, db_session):
        # Providers that only return a plain URL string (Google/OIDC) keep the
        # route's locally minted state -- the tuple-capture path must not regress
        # their behaviour.
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = _fake_authenticated_user

        class StubStringProvider:
            name = "stubstr"

            def get_authorize_url(self, state: str = "") -> str:
                return f"https://idp.example.com/auth?state={state}"

        mock_registry = MagicMock()
        mock_registry.get.return_value = StubStringProvider()
        app.state.auth_registry = mock_registry

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/auth/stubstr/authorize")
            assert resp.status_code == 200, resp.text
            data = resp.json()

            assert data["state"]
            assert data["state"] in data["authorize_url"]
            assert resp.cookies.get("oauth_state_stubstr") == data["state"]

    @pytest.mark.asyncio
    async def test_authorize_awaits_async_get_authorize_url(self, db_session):
        # A provider whose ``get_authorize_url`` is ``async`` (e.g. the OIDC
        # provider) returns a *coroutine* when called. The route MUST await it
        # via ``inspect.isawaitable`` rather than rely on ``callable()`` -- a
        # coroutine object is not callable, so the old check left it un-awaited
        # and stringified the coroutine into the response body.
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = _fake_authenticated_user

        awaited = {"flag": False}

        class StubAsyncProvider:
            name = "stubasync"

            async def get_authorize_url(self, state: str = "") -> str:
                # Prove the coroutine is actually awaited (not stringified).
                import asyncio

                await asyncio.sleep(0)
                awaited["flag"] = True
                return f"https://idp.example.com/auth?state={state}"

        mock_registry = MagicMock()
        mock_registry.get.return_value = StubAsyncProvider()
        app.state.auth_registry = mock_registry

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/auth/stubasync/authorize")
            assert resp.status_code == 200, resp.text
            data = resp.json()

            # The coroutine was awaited, not stringified.
            assert awaited["flag"] is True
            assert "<coroutine" not in data["authorize_url"]
            assert data["authorize_url"].startswith("https://idp.example.com/auth")
            assert data["state"]
            assert data["state"] in data["authorize_url"]
            assert resp.cookies.get("oauth_state_stubasync") == data["state"]

    @pytest.mark.asyncio
    async def test_authorize_async_get_authorize_url_returning_tuple(self, db_session):
        # An ``async get_authorize_url`` may also return a ``(url, state)`` tuple
        # once awaited; the route must both await *and* destructure it.
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = _fake_authenticated_user

        class StubAsyncTupleProvider:
            name = "stubasynctuple"

            async def get_authorize_url(self, state: str = ""):
                return "https://idp.example.com/auth?state=provider-state", "provider-state"

        mock_registry = MagicMock()
        mock_registry.get.return_value = StubAsyncTupleProvider()
        app.state.auth_registry = mock_registry

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/auth/stubasynctuple/authorize")
            assert resp.status_code == 200, resp.text
            data = resp.json()

            assert data["authorize_url"].startswith("https://idp.example.com/auth")
            # The AUTHORITATIVE state from the tuple wins over the route-minted one.
            assert data["state"] == "provider-state"
            assert "provider-state" in data["authorize_url"]
            assert resp.cookies.get("oauth_state_stubasynctuple") == "provider-state"
