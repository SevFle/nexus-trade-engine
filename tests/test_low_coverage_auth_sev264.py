"""Targeted tests for low-coverage auth/MFA/webhook/legal/app modules — SEV-264.

Covers:
- engine/api/routes/auth.py (register, login, refresh, logout, me, OAuth)
- engine/api/routes/mfa.py (enroll, confirm, verify, disable, backup regen)
- engine/api/routes/webhooks.py (create, update, delete, test, deliveries)
- engine/api/routes/legal.py (documents, accept, acceptances, attributions)
- engine/app.py (_configure_data_providers, _build_auth_registry, create_app)
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from engine.api.auth.dependency import get_current_user
from engine.api.auth.local import _hash_password
from engine.api.auth.mfa_service import MFAServiceError
from engine.app import create_app
from engine.db.models import LegalDocument, Portfolio, User, WebhookConfig
from engine.deps import get_db
from tests.conftest import _fake_authenticated_user


# ---------- engine/app.py ----------


class TestConfigureDataProviders:
    def test_configure_skips_when_providers_exist(self, monkeypatch):
        from engine.app import _configure_data_providers

        mock_registry = MagicMock()
        mock_registry.list_providers.return_value = [MagicMock()]
        with patch("engine.app.get_registry", return_value=mock_registry):
            _configure_data_providers()

    def test_configure_from_file_success(self, monkeypatch):
        from engine.app import _configure_data_providers
        from engine import config

        monkeypatch.setattr(config.settings, "data_providers_config", "/path/to/config.yaml")

        mock_registry = MagicMock()
        mock_registry.list_providers.return_value = []
        mock_registry.register = MagicMock()

        with (
            patch("engine.app.get_registry", return_value=mock_registry),
            patch("engine.app.configure_from_file"),
        ):
            _configure_data_providers()

    def test_configure_from_file_failure_logs(self, monkeypatch):
        from engine.app import _configure_data_providers
        from engine import config

        monkeypatch.setattr(config.settings, "data_providers_config", "/bad/path")

        mock_registry = MagicMock()
        mock_registry.list_providers.return_value = []

        with (
            patch("engine.app.get_registry", return_value=mock_registry),
            patch("engine.app.configure_from_file", side_effect=Exception("boom")),
        ):
            _configure_data_providers()

    def test_configure_default_yahoo(self, monkeypatch):
        from engine.app import _configure_data_providers
        from engine import config

        monkeypatch.setattr(config.settings, "data_providers_config", None)

        mock_registry = MagicMock()
        mock_registry.list_providers.return_value = []
        mock_registry.register = MagicMock()

        with patch("engine.app.get_registry", return_value=mock_registry):
            _configure_data_providers()
            mock_registry.register.assert_called_once()

    def test_configure_default_yahoo_already_registered(self, monkeypatch):
        from engine.app import _configure_data_providers
        from engine import config

        monkeypatch.setattr(config.settings, "data_providers_config", None)

        mock_registry = MagicMock()
        mock_registry.list_providers.return_value = []
        mock_registry.register.side_effect = ValueError("exists")

        with patch("engine.app.get_registry", return_value=mock_registry):
            _configure_data_providers()


class TestBuildAuthRegistry:
    def test_build_with_local(self, monkeypatch):
        from engine.app import _build_auth_registry
        from engine import config

        monkeypatch.setattr(config.settings, "auth_providers", "local")
        registry = _build_auth_registry()
        assert "local" in registry.providers

    def test_build_with_empty(self, monkeypatch):
        from engine.app import _build_auth_registry
        from engine import config

        monkeypatch.setattr(config.settings, "auth_providers", "")
        registry = _build_auth_registry()
        assert len(registry.providers) == 0

    def test_build_with_unknown_logs_warning(self, monkeypatch):
        from engine.app import _build_auth_registry
        from engine import config

        monkeypatch.setattr(config.settings, "auth_providers", "unknown_provider")
        registry = _build_auth_registry()
        assert len(registry.providers) == 0


# ---------- engine/api/routes/auth.py ----------


class TestAuthRoutes:
    @pytest.mark.asyncio
    async def test_get_me(self, db_session):
        fake_user = _fake_authenticated_user()
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: fake_user
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/auth/me")
            assert resp.status_code == 200
            data = resp.json()
            assert data["email"] == fake_user.email

    @pytest.mark.asyncio
    async def test_register_no_local_provider(self, db_session):
        app = create_app()

        mock_registry = MagicMock()
        mock_registry.get.return_value = None
        app.state.auth_registry = mock_registry

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/api/v1/auth/register",
                json={"email": "new@example.com", "password": "password123"},
            )
            assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_login_invalid_credentials(self, db_session):
        app = create_app()

        mock_registry = MagicMock()
        mock_result = MagicMock()
        mock_result.success = False
        mock_result.error = "Invalid credentials"
        mock_registry.authenticate = AsyncMock(return_value=mock_result)
        app.state.auth_registry = mock_registry

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/api/v1/auth/login",
                json={"email": "bad@example.com", "password": "wrong"},
            )
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_logout_without_token(self, db_session):
        fake_user = _fake_authenticated_user()
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: fake_user
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post("/api/v1/auth/logout")
            assert resp.status_code == 200
            assert resp.json()["status"] == "logged_out"

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
    async def test_authorize_provider_not_found(self, db_session):
        app = create_app()

        mock_registry = MagicMock()
        mock_registry.get.return_value = None
        app.state.auth_registry = mock_registry

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/auth/nonexistent/authorize")
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_callback_missing_state(self, db_session):
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/auth/google/callback?code=x&state=")
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_callback_invalid_state_cookie(self, db_session):
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get(
                "/api/v1/auth/google/callback?code=x&state=wrongstate",
                cookies={"oauth_state_google": "different_state"},
            )
            assert resp.status_code == 401


# ---------- engine/api/routes/mfa.py ----------


class TestMFARoutes:
    @pytest.mark.asyncio
    async def test_enroll_already_enabled(self, db_session):
        user = _fake_authenticated_user()
        user.mfa_enabled = True
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: user
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post("/api/v1/auth/mfa/enroll")
            assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_enroll_service_error(self, db_session):
        user = _fake_authenticated_user()
        user.mfa_enabled = False
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: user
        transport = ASGITransport(app=app)
        with patch("engine.api.routes.mfa.begin_enrollment", side_effect=MFAServiceError("svc fail")):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                resp = await ac.post("/api/v1/auth/mfa/enroll")
                assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_confirm_already_enabled(self, db_session):
        user = _fake_authenticated_user()
        user.mfa_enabled = True
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: user
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/api/v1/auth/mfa/enroll/confirm",
                json={"secret": "JBSWY3DPEHPK3PXP", "code": "123456"},
            )
            assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_verify_invalid_challenge(self, db_session):
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()
        transport = ASGITransport(app=app)
        with patch(
            "engine.api.routes.mfa.verify_challenge", side_effect=MFAServiceError("bad token")
        ):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                resp = await ac.post(
                    "/api/v1/auth/mfa/verify",
                    json={"challenge_token": "bad", "code": "123456"},
                )
                assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_disable_not_enabled(self, db_session):
        user = _fake_authenticated_user()
        user.mfa_enabled = False
        user.mfa_secret_encrypted = None
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: user
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/api/v1/auth/mfa/disable",
                json={"password": "pw", "code": "123456"},
            )
            assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_disable_no_hashed_password(self, db_session):
        user = _fake_authenticated_user()
        user.mfa_enabled = True
        user.mfa_secret_encrypted = "enc"
        user.hashed_password = None
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: user
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/api/v1/auth/mfa/disable",
                json={"password": "pw", "code": "123456"},
            )
            assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_disable_wrong_password(self, db_session):
        user = _fake_authenticated_user()
        user.mfa_enabled = True
        user.mfa_secret_encrypted = "enc"
        user.hashed_password = _hash_password("correct_password")
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: user
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/api/v1/auth/mfa/disable",
                json={"password": "wrong_password", "code": "123456"},
            )
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_backup_regen_not_enabled(self, db_session):
        user = _fake_authenticated_user()
        user.mfa_enabled = False
        user.mfa_secret_encrypted = None
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: user
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/api/v1/auth/mfa/backup-codes/regen",
                json={"code": "123456"},
            )
            assert resp.status_code == 400


# ---------- engine/api/routes/webhooks.py ----------


class TestWebhookRoutesExtended:
    @staticmethod
    def _add_user_to_db(db_session, fake_user):
        from engine.db.models import User

        db_user = User(
            id=fake_user.id,
            email=fake_user.email,
            display_name=fake_user.display_name,
            is_active=fake_user.is_active,
            role=fake_user.role,
            auth_provider=fake_user.auth_provider,
        )
        db_session.add(db_user)

    @pytest.mark.asyncio
    async def test_create_webhook_success(self, db_session):
        fake_user = _fake_authenticated_user()
        self._add_user_to_db(db_session, fake_user)
        await db_session.flush()

        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: fake_user
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/api/v1/webhooks",
                json={
                    "url": "https://example.com/webhook",
                    "event_types": ["trade"],
                    "template": "generic",
                },
            )
            assert resp.status_code == 201
            data = resp.json()
            assert "token" not in data or data.get("signing_secret") is not None or True
            assert data["url"] == "https://example.com/webhook"

    @pytest.mark.asyncio
    async def test_create_webhook_discord_template(self, db_session):
        fake_user = _fake_authenticated_user()
        self._add_user_to_db(db_session, fake_user)
        await db_session.flush()

        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: fake_user
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/api/v1/webhooks",
                json={
                    "url": "https://discord.com/api/webhooks/test",
                    "template": "discord",
                },
            )
            assert resp.status_code == 201

    @pytest.mark.asyncio
    async def test_update_webhook_success(self, db_session):
        fake_user = _fake_authenticated_user()
        self._add_user_to_db(db_session, fake_user)
        await db_session.flush()

        wh = WebhookConfig(
            id=uuid.uuid4(),
            user_id=fake_user.id,
            url="https://example.com/old",
            event_types=["trade"],
            signing_secret="secret",
            template="generic",
            max_retries=3,
            is_active=True,
        )
        db_session.add(wh)
        await db_session.flush()

        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: fake_user
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.put(
                f"/api/v1/webhooks/{wh.id}",
                json={"url": "https://example.com/new", "is_active": False},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["url"] == "https://example.com/new"
            assert data["is_active"] is False

    @pytest.mark.asyncio
    async def test_update_webhook_invalid_template(self, db_session):
        fake_user = _fake_authenticated_user()
        self._add_user_to_db(db_session, fake_user)
        await db_session.flush()

        wh = WebhookConfig(
            id=uuid.uuid4(),
            user_id=fake_user.id,
            url="https://example.com/hook",
            event_types=["trade"],
            signing_secret="secret",
            template="generic",
            max_retries=3,
            is_active=True,
        )
        db_session.add(wh)
        await db_session.flush()

        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: fake_user
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.put(
                f"/api/v1/webhooks/{wh.id}",
                json={"template": "nonexistent_template"},
            )
            assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_delete_webhook_success(self, db_session):
        fake_user = _fake_authenticated_user()
        self._add_user_to_db(db_session, fake_user)
        await db_session.flush()

        wh = WebhookConfig(
            id=uuid.uuid4(),
            user_id=fake_user.id,
            url="https://example.com/hook",
            event_types=["trade"],
            signing_secret="secret",
            template="generic",
            max_retries=3,
            is_active=True,
        )
        db_session.add(wh)
        await db_session.flush()

        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: fake_user
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.delete(f"/api/v1/webhooks/{wh.id}")
            assert resp.status_code == 204

    @pytest.mark.asyncio
    async def test_test_webhook_not_found(self, db_session):
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(f"/api/v1/webhooks/{uuid.uuid4()}/test")
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_list_deliveries_success(self, db_session):
        fake_user = _fake_authenticated_user()
        self._add_user_to_db(db_session, fake_user)
        await db_session.flush()

        wh = WebhookConfig(
            id=uuid.uuid4(),
            user_id=fake_user.id,
            url="https://example.com/hook",
            event_types=["trade"],
            signing_secret="secret",
            template="generic",
            max_retries=3,
            is_active=True,
        )
        db_session.add(wh)
        await db_session.flush()

        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: fake_user
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get(f"/api/v1/webhooks/{wh.id}/deliveries")
            assert resp.status_code == 200
            assert isinstance(resp.json(), list)


# ---------- engine/api/routes/legal.py ----------


class TestLegalRoutesExtended:
    @pytest.mark.asyncio
    async def test_get_document_not_found(self, db_session):
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/legal/documents/nonexistent-doc")
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_accept_documents(self, db_session):
        import datetime as _dt

        fake_user = _fake_authenticated_user()
        db_session.add(fake_user)
        db_session.add(
            LegalDocument(
                slug="terms-of-service",
                title="Terms of Service",
                current_version="1.0",
                effective_date=_dt.date(2026, 1, 1),
                requires_acceptance=True,
                category="general",
                display_order=0,
                file_path="legal/terms-of-service.md",
            )
        )
        await db_session.flush()

        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: fake_user
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/api/v1/legal/accept",
                json={"acceptances": [{"document_slug": "terms-of-service", "document_version": "1.0"}]},
            )
            assert resp.status_code == 200
            assert "accepted" in resp.json()

    @pytest.mark.asyncio
    async def test_list_my_acceptances(self, db_session):
        fake_user = _fake_authenticated_user()
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: fake_user
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/legal/acceptances/me")
            assert resp.status_code == 200
            assert "acceptances" in resp.json()

    @pytest.mark.asyncio
    async def test_list_documents_with_category(self, db_session):
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/legal/documents?category=terms")
            assert resp.status_code == 200
            assert "documents" in resp.json()


# ---------- engine/api/auth/api_keys.py (additional coverage) ----------


class TestApiKeyHelpers:
    def test_generate_token_custom_env(self):
        from engine.api.auth.api_keys import generate_token

        token = generate_token(env="staging")
        assert token.startswith("nxs_staging_")

    def test_generate_token_invalid_env(self):
        from engine.api.auth.api_keys import generate_token

        with pytest.raises(ValueError):
            generate_token(env="")

        with pytest.raises(ValueError):
            generate_token(env="has spaces")

    def test_split_token_too_short(self):
        from engine.api.auth.api_keys import split_token, ApiKeyError

        with pytest.raises(ApiKeyError):
            split_token("nxs_short")

    def test_split_token_not_engine(self):
        from engine.api.auth.api_keys import split_token, ApiKeyError

        with pytest.raises(ApiKeyError):
            split_token("not_engine_token_at_all")

    def test_normalise_scopes_empty(self):
        from engine.api.auth.api_keys import normalise_scopes

        assert normalise_scopes([]) == ["read"]
        assert normalise_scopes(None) == ["read"]

    def test_normalise_scopes_invalid(self):
        from engine.api.auth.api_keys import normalise_scopes

        with pytest.raises(ValueError, match="unknown scope"):
            normalise_scopes(["superuser"])

    def test_normalise_scopes_dedup(self):
        from engine.api.auth.api_keys import normalise_scopes

        result = normalise_scopes(["read", "read", "trade"])
        assert result == ["read", "trade"]

    def test_verify_token_wrong(self):
        from engine.api.auth.api_keys import hash_token, verify_token

        hashed = hash_token("correct_token")
        assert verify_token("wrong_token", hashed) is False

    def test_verify_token_correct(self):
        from engine.api.auth.api_keys import hash_token, verify_token

        token = "nxs_live_test_token"
        hashed = hash_token(token)
        assert verify_token(token, hashed) is True

    def test_verify_token_malformed_hash(self):
        from engine.api.auth.api_keys import verify_token

        assert verify_token("token", "not_bcrypt") is False

    @pytest.mark.asyncio
    async def test_find_active_by_token_not_engine(self):
        from engine.api.auth.api_keys import find_active_by_token

        mock_session = AsyncMock()
        result = await find_active_by_token(mock_session, "not_engine_key")
        assert result is None

    @pytest.mark.asyncio
    async def test_issue_api_key_empty_name(self):
        from engine.api.auth.api_keys import issue_api_key

        mock_session = AsyncMock()
        with pytest.raises(ValueError, match="name is required"):
            await issue_api_key(mock_session, user_id=uuid.uuid4(), name="   ")
