"""Tests for engine.api.routes.portfolio — portfolio CRUD routes."""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from engine.api.auth.dependency import get_current_user
from engine.app import create_app
from engine.db.models import Portfolio, User
from engine.deps import get_db
from tests.conftest import FAKE_USER_ID, _fake_authenticated_user


class TestPortfolioRoutes:
    @pytest.mark.asyncio
    async def test_create_portfolio(self, db_session):
        db_session.add(_fake_authenticated_user())
        await db_session.flush()

        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/api/v1/portfolio/",
                json={"name": "Test Portfolio", "initial_capital": 50000.0},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["name"] == "Test Portfolio"
            assert float(data["initial_capital"]) == pytest.approx(50000.0)

    @pytest.mark.asyncio
    async def test_list_portfolios(self, db_session):
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/portfolio/")
            assert resp.status_code == 200
            assert isinstance(resp.json(), list)

    @pytest.mark.asyncio
    async def test_get_portfolio_invalid_uuid(self, db_session):
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/portfolio/not-a-uuid")
            assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_get_portfolio_not_found(self, db_session):
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get(f"/api/v1/portfolio/{uuid.uuid4()}")
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_archive_portfolio_invalid_uuid(self, db_session):
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.delete("/api/v1/portfolio/not-a-uuid")
            assert resp.status_code == 400
