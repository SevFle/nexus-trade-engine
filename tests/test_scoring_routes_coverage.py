"""Tests for engine.api.routes.scoring — scoring API routes."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from engine.api.auth.dependency import get_current_user
from engine.app import create_app
from engine.deps import get_db
from tests.conftest import _fake_authenticated_user


class TestScoringRoutes:
    @pytest.mark.asyncio
    async def test_run_scoring_strategy_not_found(self, db_session):
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/api/v1/scoring/nonexistent_strategy/run",
                json={"universe": ["AAPL"]},
            )
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_scoring_results(self, db_session):
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/scoring/some_strategy/results")
            assert resp.status_code == 200
            data = resp.json()
            assert "results" in data
            assert "count" in data
