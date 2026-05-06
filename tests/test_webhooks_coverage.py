"""Tests for engine.api.routes.webhooks — webhook CRUD routes."""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from engine.api.auth.dependency import get_current_user
from engine.api.routes.webhooks import _VALID_TEMPLATES, _validate_template
from engine.app import create_app
from engine.deps import get_db
from tests.conftest import _fake_authenticated_user


class TestValidateTemplate:
    def test_valid_templates(self):
        for template in _VALID_TEMPLATES:
            _validate_template(template)

    def test_invalid_template_raises(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            _validate_template("invalid_template")
        assert exc_info.value.status_code == 400


class TestWebhookRoutes:
    @pytest.mark.asyncio
    async def test_list_webhooks(self, db_session):
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/webhooks")
            assert resp.status_code == 200
            assert isinstance(resp.json(), list)

    @pytest.mark.asyncio
    async def test_create_webhook_invalid_template(self, db_session):
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/api/v1/webhooks",
                json={
                    "url": "https://example.com/hook",
                    "template": "invalid_template",
                },
            )
            assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_delete_webhook_not_found(self, db_session):
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.delete(f"/api/v1/webhooks/{uuid.uuid4()}")
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_update_webhook_not_found(self, db_session):
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.put(
                f"/api/v1/webhooks/{uuid.uuid4()}",
                json={"url": "https://example.com/new"},
            )
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_list_deliveries_webhook_not_found(self, db_session):
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get(f"/api/v1/webhooks/{uuid.uuid4()}/deliveries")
            assert resp.status_code == 404
