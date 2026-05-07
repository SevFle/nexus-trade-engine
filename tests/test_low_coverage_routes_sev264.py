"""Targeted tests for low-coverage route modules — SEV-264.

Covers:
- engine/api/routes/health.py (provider_health, ready)
- engine/api/auth/local.py (authenticate, create_user)
- engine/api/auth/dependency.py (scope enforcement, role checks)
- engine/api/routes/websocket.py (helper functions)
- engine/api/routes/api_keys.py (create, list, revoke)
- engine/api/routes/portfolio.py (CRUD)
- engine/api/routes/strategies.py (list, get, activate, deactivate, health)
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from engine.api.auth.dependency import (
    ROLE_HIERARCHY,
    _SCOPE_HIERARCHY,
    _scope_satisfied,
    require_api_scope,
    require_role,
)
from engine.api.auth.dependency import get_current_user
from engine.api.routes.websocket import _coerce_topic_list
from engine.app import create_app
from engine.api.websocket.manager import VALID_TOPICS
from engine.db.models import User
from engine.deps import get_db
from tests.conftest import _fake_authenticated_user


def _make_db_client(db_session, user=None):
    if user is None:
        user = _fake_authenticated_user()
    app = create_app()

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = lambda: user
    return app, user


# ---------- engine/api/routes/health.py ----------


class TestHealthProviderEndpoint:
    @pytest.mark.asyncio
    async def test_provider_health_all_up(self, db_session):
        from engine.data.providers import get_registry
        from engine.data.providers.base import HealthCheckResult, HealthStatus

        registry = get_registry()
        saved = list(registry._registrations.items())
        registry._registrations.clear()

        mock_result = HealthCheckResult(
            name="mock_provider",
            status=HealthStatus.UP,
            latency_ms=10,
            detail="",
        )
        registry.health = AsyncMock(return_value=[mock_result])

        app, _ = _make_db_client(db_session)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/health/providers")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"

        registry._registrations.clear()
        for k, v in saved:
            registry._registrations[k] = v

    @pytest.mark.asyncio
    async def test_provider_health_empty(self, db_session):
        from engine.data.providers import get_registry

        registry = get_registry()
        saved = list(registry._registrations.items())
        registry._registrations.clear()
        registry.health = AsyncMock(return_value=[])

        app, _ = _make_db_client(db_session)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/health/providers")
            assert resp.status_code == 200

        registry._registrations.clear()
        for k, v in saved:
            registry._registrations[k] = v

    @pytest.mark.asyncio
    async def test_provider_health_degraded(self, db_session):
        from engine.data.providers import get_registry
        from engine.data.providers.base import HealthCheckResult, HealthStatus

        registry = get_registry()
        saved = list(registry._registrations.items())
        registry._registrations.clear()

        registry.health = AsyncMock(return_value=[
            HealthCheckResult(name="a", status=HealthStatus.UP, latency_ms=5, detail=""),
            HealthCheckResult(name="b", status=HealthStatus.DOWN, latency_ms=None, detail="fail"),
        ])

        app, _ = _make_db_client(db_session)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/health/providers")
            assert resp.json()["status"] == "degraded"

        registry._registrations.clear()
        for k, v in saved:
            registry._registrations[k] = v

    @pytest.mark.asyncio
    async def test_provider_health_all_down(self, db_session):
        from engine.data.providers import get_registry
        from engine.data.providers.base import HealthCheckResult, HealthStatus

        registry = get_registry()
        saved = list(registry._registrations.items())
        registry._registrations.clear()

        registry.health = AsyncMock(return_value=[
            HealthCheckResult(name="a", status=HealthStatus.DOWN, latency_ms=None, detail="err"),
            HealthCheckResult(name="b", status=HealthStatus.DOWN, latency_ms=None, detail="err"),
        ])

        app, _ = _make_db_client(db_session)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/health/providers")
            assert resp.json()["status"] == "down"

        registry._registrations.clear()
        for k, v in saved:
            registry._registrations[k] = v


class TestReadyEndpoint:
    @pytest.mark.asyncio
    async def test_ready_with_db_ok(self, db_session):
        app, _ = _make_db_client(db_session)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/ready")
            assert resp.status_code == 200
            data = resp.json()
            assert data["db"] == "ok"

    @pytest.mark.asyncio
    async def test_ready_valkey_error(self, db_session):
        app, _ = _make_db_client(db_session)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/ready")
            assert resp.status_code == 200
            data = resp.json()
            assert "db" in data


# ---------- engine/api/auth/local.py ----------


class TestLocalAuthProvider:
    @pytest.mark.asyncio
    async def test_authenticate_missing_email(self):
        from engine.api.auth.local import LocalAuthProvider

        provider = LocalAuthProvider()
        result = await provider.authenticate(password="pw", db=MagicMock())
        assert not result.success

    @pytest.mark.asyncio
    async def test_authenticate_missing_password(self):
        from engine.api.auth.local import LocalAuthProvider

        provider = LocalAuthProvider()
        result = await provider.authenticate(email="a@b.com", db=MagicMock())
        assert not result.success

    @pytest.mark.asyncio
    async def test_authenticate_missing_db(self):
        from engine.api.auth.local import LocalAuthProvider

        provider = LocalAuthProvider()
        result = await provider.authenticate(email="a@b.com", password="pw")
        assert not result.success

    @pytest.mark.asyncio
    async def test_authenticate_user_not_found(self):
        from engine.api.auth.local import LocalAuthProvider

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)

        provider = LocalAuthProvider()
        result = await provider.authenticate(email="a@b.com", password="pw", db=mock_db)
        assert not result.success

    @pytest.mark.asyncio
    async def test_authenticate_wrong_provider(self):
        from engine.api.auth.local import LocalAuthProvider

        user = MagicMock()
        user.auth_provider = "google"
        user.hashed_password = None

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = user
        mock_db.execute = AsyncMock(return_value=mock_result)

        provider = LocalAuthProvider()
        result = await provider.authenticate(email="a@b.com", password="pw", db=mock_db)
        assert not result.success

    @pytest.mark.asyncio
    async def test_authenticate_wrong_password(self):
        from engine.api.auth.local import LocalAuthProvider, _hash_password

        user = MagicMock()
        user.auth_provider = "local"
        user.hashed_password = _hash_password("correct_password")

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = user
        mock_db.execute = AsyncMock(return_value=mock_result)

        provider = LocalAuthProvider()
        result = await provider.authenticate(email="a@b.com", password="wrong", db=mock_db)
        assert not result.success

    @pytest.mark.asyncio
    async def test_authenticate_inactive_user(self):
        from engine.api.auth.local import LocalAuthProvider, _hash_password

        user = MagicMock()
        user.auth_provider = "local"
        user.hashed_password = _hash_password("pw")
        user.is_active = False

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = user
        mock_db.execute = AsyncMock(return_value=mock_result)

        provider = LocalAuthProvider()
        result = await provider.authenticate(email="a@b.com", password="pw", db=mock_db)
        assert not result.success
        assert "disabled" in result.error

    @pytest.mark.asyncio
    async def test_authenticate_success(self):
        from engine.api.auth.local import LocalAuthProvider, _hash_password

        user = MagicMock()
        user.auth_provider = "local"
        user.hashed_password = _hash_password("pw")
        user.is_active = True
        user.email = "a@b.com"
        user.display_name = "Test"
        user.role = "user"
        user.id = uuid.uuid4()

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = user
        mock_db.execute = AsyncMock(return_value=mock_result)

        provider = LocalAuthProvider()
        result = await provider.authenticate(email="a@b.com", password="pw", db=mock_db)
        assert result.success
        assert result.user_info.email == "a@b.com"

    @pytest.mark.asyncio
    async def test_create_user_missing_db(self):
        from engine.api.auth.local import LocalAuthProvider

        provider = LocalAuthProvider()
        result = await provider.create_user(user_info=MagicMock(), password="pw")
        assert not result.success

    @pytest.mark.asyncio
    async def test_create_user_missing_password(self):
        from engine.api.auth.local import LocalAuthProvider

        provider = LocalAuthProvider()
        result = await provider.create_user(user_info=MagicMock(), db=MagicMock())
        assert not result.success

    @pytest.mark.asyncio
    async def test_create_user_registration_disabled(self, monkeypatch):
        from engine.api.auth.local import LocalAuthProvider
        from engine import config

        monkeypatch.setattr(config.settings, "auth_local_allow_registration", False)

        provider = LocalAuthProvider()
        result = await provider.create_user(
            user_info=MagicMock(email="a@b.com"),
            password="password123",
            db=MagicMock(),
        )
        assert not result.success

    @pytest.mark.asyncio
    async def test_create_user_short_password(self, monkeypatch):
        from engine.api.auth.local import LocalAuthProvider
        from engine import config

        monkeypatch.setattr(config.settings, "auth_local_allow_registration", True)

        provider = LocalAuthProvider()
        result = await provider.create_user(
            user_info=MagicMock(email="a@b.com"),
            password="short",
            db=MagicMock(),
        )
        assert not result.success

    @pytest.mark.asyncio
    async def test_create_user_email_exists(self, monkeypatch):
        from engine.api.auth.local import LocalAuthProvider
        from engine import config

        monkeypatch.setattr(config.settings, "auth_local_allow_registration", True)

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = MagicMock()
        mock_db.execute = AsyncMock(return_value=mock_result)

        provider = LocalAuthProvider()
        result = await provider.create_user(
            user_info=MagicMock(email="a@b.com"),
            password="password123",
            db=mock_db,
        )
        assert not result.success

    @pytest.mark.asyncio
    async def test_create_user_success(self, monkeypatch):
        from engine.api.auth.local import LocalAuthProvider
        from engine import config

        monkeypatch.setattr(config.settings, "auth_local_allow_registration", True)

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        provider = LocalAuthProvider()
        result = await provider.create_user(
            user_info=MagicMock(email="new@b.com", display_name="New User"),
            password="password123",
            db=mock_db,
        )
        assert result.success
        mock_db.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_user_default_display_name(self, monkeypatch):
        from engine.api.auth.local import LocalAuthProvider
        from engine import config

        monkeypatch.setattr(config.settings, "auth_local_allow_registration", True)

        created_user = None

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)
        mock_db.flush = AsyncMock()
        mock_db.refresh = AsyncMock()

        def fake_add(user):
            nonlocal created_user
            created_user = user

        mock_db.add = fake_add

        provider = LocalAuthProvider()
        result = await provider.create_user(
            user_info=MagicMock(email="new@b.com", display_name=None),
            password="password123",
            db=mock_db,
        )
        assert result.success
        assert created_user is not None
        assert created_user.display_name == "new"


# ---------- engine/api/auth/dependency.py ----------


class TestDependencyHelpers:
    def test_scope_satisfied_read_by_read(self):
        assert _scope_satisfied(["read"], "read") is True

    def test_scope_satisfied_read_by_trade(self):
        assert _scope_satisfied(["trade"], "read") is True

    def test_scope_satisfied_read_by_admin(self):
        assert _scope_satisfied(["admin"], "read") is True

    def test_scope_satisfied_trade_not_by_read(self):
        assert _scope_satisfied(["read"], "trade") is False

    def test_scope_satisfied_admin_not_by_trade(self):
        assert _scope_satisfied(["trade"], "admin") is False

    def test_scope_satisfied_empty_granted(self):
        assert _scope_satisfied([], "read") is False

    def test_scope_satisfied_none_granted(self):
        assert _scope_satisfied(None, "read") is False

    def test_require_role_passes(self):
        user = MagicMock()
        user.role = "admin"
        check = require_role("admin")
        assert check is not None

    def test_require_role_unknown_scope_raises(self):
        with pytest.raises(ValueError, match="unknown scope"):
            require_api_scope("nonexistent_scope")


# ---------- engine/api/routes/websocket.py ----------


class TestWebSocketHelpers:
    def test_coerce_topic_list_valid(self):
        result = _coerce_topic_list(list(VALID_TOPICS)[:3])
        assert len(result) == 3

    def test_coerce_topic_list_invalid_topics(self):
        result = _coerce_topic_list(["invalid_topic", "also_invalid"])
        assert result == []

    def test_coerce_topic_list_mixed(self):
        topics = list(VALID_TOPICS)[:1] + ["invalid"]
        result = _coerce_topic_list(topics)
        assert all(t in VALID_TOPICS for t in result)

    def test_coerce_topic_list_non_list(self):
        assert _coerce_topic_list("not_a_list") == []
        assert _coerce_topic_list(42) == []
        assert _coerce_topic_list(None) == []

    def test_coerce_topic_list_non_string_items(self):
        result = _coerce_topic_list([123, None, True])
        assert result == []


# ---------- engine/api/routes/api_keys.py ----------


class TestApiKeyRoutes:
    @pytest.mark.asyncio
    async def test_create_api_key(self, db_session):
        fake_user = _fake_authenticated_user()
        user = User(
            id=fake_user.id,
            email=fake_user.email,
            display_name=fake_user.display_name,
            is_active=True,
            role="admin",
            auth_provider="local",
        )
        db_session.add(user)
        await db_session.flush()

        app, _ = _make_db_client(db_session, user)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/api/v1/auth/api-keys",
                json={"name": "test-key", "scopes": ["read"]},
            )
            assert resp.status_code == 201
            data = resp.json()
            assert "token" in data
            assert data["token"].startswith("nxs_")

    @pytest.mark.asyncio
    async def test_create_api_key_invalid_scopes(self, db_session):
        app, _ = _make_db_client(db_session)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/api/v1/auth/api-keys",
                json={"name": "bad-key", "scopes": ["superuser"]},
            )
            assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_list_api_keys(self, db_session):
        fake_user = _fake_authenticated_user()
        user = User(
            id=fake_user.id,
            email=fake_user.email,
            display_name=fake_user.display_name,
            is_active=True,
            role="admin",
            auth_provider="local",
        )
        db_session.add(user)
        await db_session.flush()

        app, _ = _make_db_client(db_session, user)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/auth/api-keys")
            assert resp.status_code == 200
            assert isinstance(resp.json(), list)

    @pytest.mark.asyncio
    async def test_revoke_api_key_not_found(self, db_session):
        app, _ = _make_db_client(db_session)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.delete(f"/api/v1/auth/api-keys/{uuid.uuid4()}")
            assert resp.status_code == 404


# ---------- engine/api/routes/portfolio.py ----------


class TestPortfolioRoutes:
    @pytest.mark.asyncio
    async def test_create_portfolio(self, db_session):
        fake_user = _fake_authenticated_user()
        user = User(
            id=fake_user.id,
            email=fake_user.email,
            display_name=fake_user.display_name,
            is_active=True,
            role="admin",
            auth_provider="local",
        )
        db_session.add(user)
        await db_session.flush()

        app, _ = _make_db_client(db_session, user)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/api/v1/portfolio/",
                json={"name": "Test Portfolio", "description": "desc", "initial_capital": 50000},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["name"] == "Test Portfolio"

    @pytest.mark.asyncio
    async def test_list_portfolios(self, db_session):
        app, _ = _make_db_client(db_session)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/portfolio/")
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_get_portfolio_invalid_id(self, db_session):
        app, _ = _make_db_client(db_session)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/portfolio/not-a-uuid")
            assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_get_portfolio_not_found(self, db_session):
        app, _ = _make_db_client(db_session)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get(f"/api/v1/portfolio/{uuid.uuid4()}")
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_archive_portfolio_invalid_id(self, db_session):
        app, _ = _make_db_client(db_session)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.delete("/api/v1/portfolio/not-a-uuid")
            assert resp.status_code == 400


# ---------- engine/api/routes/strategies.py ----------


class TestStrategiesRoutes:
    @pytest.mark.asyncio
    async def test_list_strategies(self, db_session):
        app = create_app()
        mock_registry = MagicMock()
        mock_registry.list_all.return_value = []
        app.state.plugin_registry = mock_registry

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/strategies/")
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_get_strategy_not_found(self, db_session):
        app = create_app()
        mock_registry = MagicMock()
        mock_registry.get.return_value = None
        app.state.plugin_registry = mock_registry

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/strategies/nonexistent")
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_strategy_found(self, db_session):
        app = create_app()
        mock_entry = MagicMock()
        mock_entry.manifest.id = "sma_crossover"
        mock_entry.manifest.name = "SMA Crossover"
        mock_entry.manifest.version = "1.0.0"
        mock_entry.manifest.author = "test"
        mock_entry.manifest.description = "SMA strategy"
        mock_entry.manifest.config_schema = {}
        mock_entry.manifest.data_feeds = []
        mock_entry.manifest.watchlist = []
        mock_entry.manifest.requires_network.return_value = False
        mock_entry.manifest.requires_gpu.return_value = False
        mock_entry.is_loaded = True
        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_entry
        app.state.plugin_registry = mock_registry

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/strategies/sma_crossover")
            assert resp.status_code == 200
            assert resp.json()["id"] == "sma_crossover"

    @pytest.mark.asyncio
    async def test_activate_strategy_not_found(self, db_session):
        app = create_app()
        mock_registry = MagicMock()
        mock_registry.get.return_value = None
        app.state.plugin_registry = mock_registry

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/api/v1/strategies/nonexistent/activate",
                json={"params": {}},
            )
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_deactivate_strategy(self, db_session):
        app = create_app()
        mock_registry = AsyncMock()
        app.state.plugin_registry = mock_registry

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post("/api/v1/strategies/test/deactivate")
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_reload_strategy_success(self, db_session):
        app = create_app()
        mock_registry = AsyncMock()
        mock_registry.reload.return_value = True
        app.state.plugin_registry = mock_registry

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post("/api/v1/strategies/test/reload")
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_reload_strategy_failure(self, db_session):
        app = create_app()
        mock_registry = AsyncMock()
        mock_registry.reload.return_value = False
        app.state.plugin_registry = mock_registry

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post("/api/v1/strategies/test/reload")
            assert resp.status_code == 500

    @pytest.mark.asyncio
    async def test_strategy_health_not_active(self, db_session):
        app = create_app()
        mock_entry = MagicMock()
        mock_entry.is_loaded = False
        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_entry
        app.state.plugin_registry = mock_registry

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/strategies/test/health")
            assert resp.status_code == 404


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

    def test_split_token_too_short(self):
        from engine.api.auth.api_keys import split_token, ApiKeyError

        with pytest.raises(ApiKeyError):
            split_token("nxs_short")

    def test_split_token_not_engine(self):
        from engine.api.auth.api_keys import split_token, ApiKeyError

        with pytest.raises(ApiKeyError):
            split_token("not_engine_token")

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

        assert normalise_scopes(["read", "read", "trade"]) == ["read", "trade"]

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
