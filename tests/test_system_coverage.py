"""Tests for engine.api.routes.system — system status endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from engine.api.auth.dependency import get_current_user
from engine.api.routes.system import _check_database, _engine_version, _gather_counts
from engine.app import create_app
from tests.conftest import _fake_authenticated_user


class TestEngineVersion:
    def test_returns_string(self):
        version = _engine_version()
        assert isinstance(version, str)

    def test_fallback_on_missing_metadata(self):
        with patch("engine.api.routes.system.version", side_effect=Exception("nope")):
            version = _engine_version()
            assert version == "0.0.0+unknown"


class TestCheckDatabase:
    @pytest.mark.asyncio
    async def test_database_ok(self):
        db = AsyncMock()
        db.execute = AsyncMock(return_value=MagicMock())
        ok, detail = await _check_database(db)
        assert ok is True
        assert detail is None

    @pytest.mark.asyncio
    async def test_database_failure(self):
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=Exception("connection lost"))
        ok, detail = await _check_database(db)
        assert ok is False
        assert "connection lost" in detail


class TestGatherCounts:
    @pytest.mark.asyncio
    async def test_gather_counts_success(self):
        db = AsyncMock()

        async def mock_execute(stmt):
            result = MagicMock()
            result.scalar_one.return_value = 5
            return result

        db.execute = mock_execute
        counts = await _gather_counts(db)
        assert "users" in counts
        assert "portfolios" in counts
        assert "backtests" in counts
        assert counts["users"] == 5


class TestSystemStatusEndpoint:
    @pytest.mark.asyncio
    async def test_system_status(self, db_session):
        app = create_app()

        from engine.deps import get_db

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/system/status")
            assert resp.status_code == 200
            data = resp.json()
            assert "engine_version" in data
            assert "uptime_seconds" in data
            assert "components" in data
            assert "counts" in data
