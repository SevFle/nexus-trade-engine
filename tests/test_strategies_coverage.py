"""Tests for engine.api.routes.strategies — strategy management routes."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from engine.api.auth.dependency import get_current_user
from engine.app import create_app
from engine.deps import get_db
from tests.conftest import _fake_authenticated_user


class TestStrategiesRoutes:
    @pytest.mark.asyncio
    async def test_list_strategies(self, db_session):
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()

        mock_registry = MagicMock()
        mock_registry.list_all.return_value = []
        app.state.plugin_registry = mock_registry

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/strategies/")
            assert resp.status_code == 200
            data = resp.json()
            assert "strategies" in data

    @pytest.mark.asyncio
    async def test_get_strategy_not_found(self, db_session):
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()

        mock_registry = MagicMock()
        mock_registry.get.return_value = None
        app.state.plugin_registry = mock_registry

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/strategies/nonexistent")
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_deactivate_strategy(self, db_session):
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()

        mock_registry = MagicMock()
        mock_registry.unload = AsyncMock()
        app.state.plugin_registry = mock_registry

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post("/api/v1/strategies/test_strat/deactivate")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "deactivated"

    @pytest.mark.asyncio
    async def test_reload_strategy_success(self, db_session):
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()

        mock_registry = MagicMock()
        mock_registry.reload = AsyncMock(return_value=True)
        app.state.plugin_registry = mock_registry

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post("/api/v1/strategies/test_strat/reload")
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_reload_strategy_failure(self, db_session):
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()

        mock_registry = MagicMock()
        mock_registry.reload = AsyncMock(return_value=False)
        app.state.plugin_registry = mock_registry

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post("/api/v1/strategies/test_strat/reload")
            assert resp.status_code == 500

    @pytest.mark.asyncio
    async def test_strategy_health_not_active(self, db_session):
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()

        mock_registry = MagicMock()
        mock_entry = MagicMock()
        mock_entry.is_loaded = False
        mock_registry.get.return_value = mock_entry
        app.state.plugin_registry = mock_registry

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/strategies/test_strat/health")
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_strategy_health_active(self, db_session):
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()

        mock_registry = MagicMock()
        mock_entry = MagicMock()
        mock_entry.is_loaded = True
        mock_registry.get.return_value = mock_entry
        app.state.plugin_registry = mock_registry

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/strategies/test_strat/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["strategy_id"] == "test_strat"
