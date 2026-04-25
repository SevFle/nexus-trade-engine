"""Tests for marketplace API routes — uses standalone TestClient with marketplace router."""

from __future__ import annotations

import uuid
from http import HTTPStatus

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from engine.api.auth.dependency import get_current_user
from engine.api.routes.marketplace import router as marketplace_router
from engine.db.models import User


def _fake_user(role: str = "developer") -> User:
    return User(
        id=uuid.uuid4(),
        email="test@example.com",
        display_name="Test",
        is_active=True,
        role=role,
    )


@pytest.fixture
async def marketplace_client():
    app = FastAPI()
    app.include_router(marketplace_router, prefix="/api/v1/marketplace")
    app.dependency_overrides[get_current_user] = lambda: _fake_user()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestMarketplaceBrowse:
    async def test_browse_returns_empty_list(self, marketplace_client: AsyncClient):
        response = await marketplace_client.get("/api/v1/marketplace/browse")
        assert response.status_code == HTTPStatus.OK
        data = response.json()
        assert data["strategies"] == []
        assert data["total"] == 0

    async def test_browse_with_filters(self, marketplace_client: AsyncClient):
        response = await marketplace_client.get(
            "/api/v1/marketplace/browse",
            params={"category": "algorithmic", "search": "mean", "sort_by": "rating"},
        )
        assert response.status_code == HTTPStatus.OK
        data = response.json()
        assert data["filters"]["category"] == "algorithmic"
        assert data["filters"]["search"] == "mean"
        assert data["filters"]["sort_by"] == "rating"


class TestMarketplaceCategories:
    async def test_list_categories(self, marketplace_client: AsyncClient):
        response = await marketplace_client.get("/api/v1/marketplace/categories")
        assert response.status_code == HTTPStatus.OK
        data = response.json()
        assert len(data["categories"]) > 0
        assert any(c["id"] == "algorithmic" for c in data["categories"])


class TestMarketplaceInstall:
    async def test_install_returns_not_implemented(self, marketplace_client: AsyncClient):
        response = await marketplace_client.post(
            "/api/v1/marketplace/install",
            json={"strategy_id": "test-strategy"},
        )
        assert response.status_code == HTTPStatus.OK
        data = response.json()
        assert data["status"] == "not_implemented"


class TestMarketplaceUninstall:
    async def test_uninstall_returns_not_implemented(self, marketplace_client: AsyncClient):
        response = await marketplace_client.delete("/api/v1/marketplace/uninstall/test-strategy")
        assert response.status_code == HTTPStatus.OK
        data = response.json()
        assert data["status"] == "not_implemented"


class TestMarketplaceRate:
    async def test_rate_invalid_rating_returns_400(self, marketplace_client: AsyncClient):
        response = await marketplace_client.post(
            "/api/v1/marketplace/test-strategy/rate",
            params={"rating": 6},
        )
        assert response.status_code == HTTPStatus.BAD_REQUEST

    async def test_rate_valid_rating(self, marketplace_client: AsyncClient):
        response = await marketplace_client.post(
            "/api/v1/marketplace/test-strategy/rate",
            params={"rating": 5, "review": "Great strategy!"},
        )
        assert response.status_code == HTTPStatus.OK
        data = response.json()
        assert data["status"] == "not_implemented"
