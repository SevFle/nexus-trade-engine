"""Tests for engine.api.routes.privacy — privacy/DSR endpoints."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from engine.api.auth.dependency import get_current_user
from engine.app import create_app
from engine.deps import get_db
from tests.conftest import _fake_authenticated_user


class TestPrivacyRoutes:
    @pytest.mark.asyncio
    async def test_supported_kinds(self, db_session):
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/privacy/kinds")
            assert resp.status_code == 200
            data = resp.json()
            assert "kinds" in data
            assert isinstance(data["kinds"], list)

    @pytest.mark.asyncio
    async def test_deletion_status(self, db_session):
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/privacy/delete/status")
            assert resp.status_code == 200
            data = resp.json()
            assert "pending" in data

    @pytest.mark.asyncio
    async def test_cancel_deletion_not_found(self, db_session):
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post("/api/v1/privacy/delete/cancel")
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_request_deletion(self, db_session):
        db_session.add(_fake_authenticated_user())
        await db_session.flush()
        await db_session.commit()

        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/api/v1/privacy/delete",
                json={"note": "Test deletion"},
            )
            assert resp.status_code == 202
            data = resp.json()
            assert data["pending"] is True

    @pytest.mark.asyncio
    async def test_list_dsr_requests(self, db_session):
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/privacy/requests")
            assert resp.status_code == 200
            data = resp.json()
            assert "requests" in data
