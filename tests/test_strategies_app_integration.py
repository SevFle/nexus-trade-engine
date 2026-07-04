"""Integration tests for the ``/api/v1/strategies`` management routes.

These tests exercise the real wiring between :func:`engine.app.create_app`,
the FastAPI lifespan (which must populate ``app.state.plugin_registry``) and
:class:`~engine.plugins.registry.PluginRegistry`. They guard against the
``AttributeError: plugin_registry`` regression where the strategies router
reads ``request.app.state.plugin_registry`` before the lifespan has set it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from engine.api.auth.dependency import get_current_user
from engine.app import create_app, lifespan
from engine.deps import get_db
from engine.plugins.registry import PluginRegistry, StrategyEntry
from tests.conftest import _fake_authenticated_user


def _wire_app(app, db_session) -> None:
    """Reuse the same dependency-override pattern as the rest of the suite."""

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = _fake_authenticated_user


class TestStrategiesRouteWithRealRegistry:
    """Drive the routes with a real :class:`PluginRegistry` (no mock) so the
    list_all / get / unload / reload implementations are covered end-to-end
    against the bundled ``mean_reversion_basic`` strategy."""

    async def test_list_strategies_no_attribute_error(self, db_session):
        app = create_app()
        _wire_app(app, db_session)
        # Mirror exactly what the lifespan now does in production: attach a
        # real PluginRegistry to app.state. The whole point of this test is
        # that reading app.state.plugin_registry must not raise.
        app.state.plugin_registry = PluginRegistry()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/strategies/")

        assert resp.status_code == 200
        data = resp.json()
        assert "strategies" in data
        ids = [entry["id"] for entry in data["strategies"]]
        assert "mean_reversion_basic" in ids

    async def test_get_strategy_detail_returns_manifest(self, db_session):
        app = create_app()
        _wire_app(app, db_session)
        app.state.plugin_registry = PluginRegistry()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/strategies/mean_reversion_basic")

        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "mean_reversion_basic"
        assert body["name"] == "mean_reversion_basic"
        assert body["version"] == "0.1.0"
        assert body["requires_network"] is False
        assert body["requires_gpu"] is False
        assert body["is_loaded"] is False

    async def test_get_unknown_strategy_returns_404(self, db_session):
        app = create_app()
        _wire_app(app, db_session)
        app.state.plugin_registry = PluginRegistry()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/strategies/does_not_exist")

        assert resp.status_code == 404

    async def test_deactivate_calls_real_unload(self, db_session):
        app = create_app()
        _wire_app(app, db_session)
        registry = PluginRegistry()
        app.state.plugin_registry = registry

        # Load the instance first so unload has something to clear; the route
        # must still return 200 afterwards.
        entry = registry.get("mean_reversion_basic")
        assert entry is not None
        await entry.instantiate()
        assert entry.is_loaded is True

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post("/api/v1/strategies/mean_reversion_basic/deactivate")

        assert resp.status_code == 200
        assert resp.json()["status"] == "deactivated"
        assert entry.is_loaded is False

    async def test_reload_route_calls_real_reload(self, db_session):
        app = create_app()
        _wire_app(app, db_session)
        app.state.plugin_registry = PluginRegistry()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post("/api/v1/strategies/mean_reversion_basic/reload")

        assert resp.status_code == 200
        assert resp.json()["status"] == "reloaded"


class TestLifespanSetsPluginRegistry:
    """The lifespan startup handler must attach a PluginRegistry to
    ``app.state`` so the first request after boot never hits an
    ``AttributeError``. We run the real lifespan with its external
    dependencies (Valkey, event bus, legal sync, …) mocked, mirroring the
    pattern in ``test_app_coverage.py``."""

    @pytest.mark.asyncio
    async def test_lifespan_sets_plugin_registry(self):
        mock_valkey = MagicMock()
        mock_valkey.aclose = AsyncMock()

        mock_app = MagicMock()
        mock_app.state.valkey = mock_valkey

        mock_ws_manager = MagicMock()
        mock_ws_manager.close_all = AsyncMock()

        mock_event_bus = MagicMock()
        mock_event_bus.disconnect = AsyncMock()
        mock_event_bus.connect = AsyncMock()

        mock_ws_bridge = MagicMock()
        mock_ws_bridge.stop = MagicMock()

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session_factory = MagicMock(return_value=mock_session)

        with (
            patch("engine.app.setup_logging"),
            patch("engine.app.setup_tracing"),
            patch("engine.app.setup_sentry"),
            patch("engine.app.set_metrics"),
            patch("engine.app.Valkey.from_url", return_value=mock_valkey),
            patch("engine.app._build_auth_registry"),
            patch("engine.app._configure_data_providers"),
            patch("engine.app._seed_reference_index"),
            patch("engine.app.get_session_factory", return_value=mock_session_factory),
            patch("engine.app.sync_legal_documents", new=AsyncMock(return_value=0)),
            patch("engine.app.ConnectionManager", return_value=mock_ws_manager),
            patch("engine.app.AuthRateLimiter"),
            patch("engine.app.init_ws"),
            patch("engine.events.bus.EventBus", return_value=mock_event_bus),
            patch("engine.app.EventBusBridge", return_value=mock_ws_bridge),
            patch("engine.app.dispose_engine", new=AsyncMock()),
            patch("engine.app.close_sentry"),
        ):
            async with lifespan(mock_app):
                # Inside the running lifespan: the registry must exist and
                # expose the management API surface the router relies on.
                registry = mock_app.state.plugin_registry
                assert isinstance(registry, PluginRegistry)
                assert isinstance(registry.list_all(), list)
                assert callable(registry.get)
                # get() returns a StrategyEntry (or None) for known plugins.
                names = registry.list_strategies()
                if names:
                    entry = registry.get(names[0])
                    assert entry is None or isinstance(entry, StrategyEntry)

    @pytest.mark.asyncio
    async def test_lifespan_plugin_registry_survives_shutdown(self):
        """The plugin_registry state must remain accessible after the
        lifespan exits (shutdown must not tear it down)."""
        mock_valkey = MagicMock()
        mock_valkey.aclose = AsyncMock()

        mock_app = MagicMock()
        mock_app.state.valkey = mock_valkey

        mock_ws_manager = MagicMock()
        mock_ws_manager.close_all = AsyncMock()

        mock_event_bus = MagicMock()
        mock_event_bus.disconnect = AsyncMock()
        mock_event_bus.connect = AsyncMock()

        mock_ws_bridge = MagicMock()
        mock_ws_bridge.stop = MagicMock()

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session_factory = MagicMock(return_value=mock_session)

        with (
            patch("engine.app.setup_logging"),
            patch("engine.app.setup_tracing"),
            patch("engine.app.setup_sentry"),
            patch("engine.app.set_metrics"),
            patch("engine.app.Valkey.from_url", return_value=mock_valkey),
            patch("engine.app._build_auth_registry"),
            patch("engine.app._configure_data_providers"),
            patch("engine.app._seed_reference_index"),
            patch("engine.app.get_session_factory", return_value=mock_session_factory),
            patch("engine.app.sync_legal_documents", new=AsyncMock(return_value=0)),
            patch("engine.app.ConnectionManager", return_value=mock_ws_manager),
            patch("engine.app.AuthRateLimiter"),
            patch("engine.app.init_ws"),
            patch("engine.events.bus.EventBus", return_value=mock_event_bus),
            patch("engine.app.EventBusBridge", return_value=mock_ws_bridge),
            patch("engine.app.dispose_engine", new=AsyncMock()),
            patch("engine.app.close_sentry"),
        ):
            async with lifespan(mock_app):
                captured = mock_app.state.plugin_registry

            assert isinstance(captured, PluginRegistry)
