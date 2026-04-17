"""Tests for marketplace API routes."""

from __future__ import annotations

from http import HTTPStatus

from httpx import AsyncClient


class TestMarketplaceBrowse:
    async def test_browse_returns_empty_list(self, client: AsyncClient):
        response = await client.get("/api/v1/marketplace/browse")
        assert response.status_code == HTTPStatus.OK
        data = response.json()
        assert data["strategies"] == []
        assert data["total"] == 0

    async def test_browse_with_filters(self, client: AsyncClient):
        response = await client.get(
            "/api/v1/marketplace/browse",
            params={"category": "algorithmic", "search": "mean", "sort_by": "rating"},
        )
        assert response.status_code == HTTPStatus.OK
        data = response.json()
        assert data["filters"]["category"] == "algorithmic"
        assert data["filters"]["search"] == "mean"
        assert data["filters"]["sort_by"] == "rating"


class TestMarketplaceCategories:
    async def test_list_categories(self, client: AsyncClient):
        response = await client.get("/api/v1/marketplace/categories")
        assert response.status_code == HTTPStatus.OK
        data = response.json()
        assert len(data["categories"]) > 0
        assert any(c["id"] == "algorithmic" for c in data["categories"])


class TestMarketplaceInstall:
    async def test_install_returns_not_implemented(self, client: AsyncClient):
        response = await client.post(
            "/api/v1/marketplace/install",
            json={"strategy_id": "test-strategy"},
        )
        assert response.status_code == HTTPStatus.OK
        data = response.json()
        assert data["status"] == "not_implemented"


class TestMarketplaceUninstall:
    async def test_uninstall_returns_not_implemented(self, client: AsyncClient):
        response = await client.delete("/api/v1/marketplace/uninstall/test-strategy")
        assert response.status_code == HTTPStatus.OK
        data = response.json()
        assert data["status"] == "not_implemented"


class TestMarketplaceRate:
    async def test_rate_invalid_rating_returns_400(self, client: AsyncClient):
        response = await client.post(
            "/api/v1/marketplace/test-strategy/rate",
            params={"rating": 6},
        )
        assert response.status_code == HTTPStatus.BAD_REQUEST

    async def test_rate_valid_rating(self, client: AsyncClient):
        response = await client.post(
            "/api/v1/marketplace/test-strategy/rate",
            params={"rating": 5, "review": "Great strategy!"},
        )
        assert response.status_code == HTTPStatus.OK
        data = response.json()
        assert data["status"] == "not_implemented"
