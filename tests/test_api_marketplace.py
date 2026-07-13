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
from engine.marketplace.ratings import (
    InvalidRatingError,
    get_ratings_store,
    reset_default_store,
)


def _fake_user(role: str = "developer") -> User:
    # A fresh user per call — each request is treated as a distinct rater,
    # which is what the aggregate / review-listing tests rely on.
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
    app.dependency_overrides[get_current_user] = _fake_user
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(autouse=True)
def _isolate_ratings_store():
    """Reset the process-wide ratings singleton before every test.

    The store is a module-level singleton shared across tests; without a
    reset, ratings submitted by one test would leak into the aggregate /
    review listings of another.
    """
    reset_default_store()
    yield
    reset_default_store()


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


class TestMarketplaceRatings:
    """Coverage for the real ``/strategies/{id}/ratings`` endpoints.

    These replaced the previous ``/{id}/rate`` stub. The route delegates to
    the in-memory :class:`~engine.marketplace.ratings.InMemoryRatingsStore`;
    these tests pin the HTTP contract (status codes, response shapes,
    upsert/aggregation/review-listing semantics).
    """

    _RATINGS = "/api/v1/marketplace/strategies/{strategy}/ratings"

    async def test_submit_rating_returns_201(self, marketplace_client: AsyncClient):
        response = await marketplace_client.post(
            self._RATINGS.format(strategy="alpha-1"),
            json={"stars": 5, "review": "Great strategy!"},
        )
        assert response.status_code == HTTPStatus.CREATED
        body = response.json()
        assert body["strategy_id"] == "alpha-1"
        assert body["stars"] == 5
        assert body["review"] == "Great strategy!"
        assert body["user_id"]  # serialized UUID string
        # created_at / updated_at are populated and equal on first submit.
        assert body["created_at"] == body["updated_at"]

    async def test_submit_rating_without_review_defaults_to_empty(
        self, marketplace_client: AsyncClient
    ):
        response = await marketplace_client.post(
            self._RATINGS.format(strategy="alpha-1"),
            json={"stars": 3},
        )
        assert response.status_code == HTTPStatus.CREATED
        assert response.json()["review"] == ""

    async def test_invalid_stars_rejected(self, marketplace_client: AsyncClient):
        # Pydantic Field(ge=1, le=5) rejects out-of-range stars with 422
        # before the handler/store ever runs.
        for bad in (0, 6, -1):
            response = await marketplace_client.post(
                self._RATINGS.format(strategy="alpha-1"),
                json={"stars": bad},
            )
            assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, bad

    async def test_get_ratings_aggregate_and_distribution(
        self, marketplace_client: AsyncClient
    ):
        # Two distinct users (the override mints a new user per request).
        await marketplace_client.post(
            self._RATINGS.format(strategy="beta-2"), json={"stars": 5}
        )
        await marketplace_client.post(
            self._RATINGS.format(strategy="beta-2"), json={"stars": 3}
        )

        response = await marketplace_client.get(self._RATINGS.format(strategy="beta-2"))
        assert response.status_code == HTTPStatus.OK
        body = response.json()
        agg = body["aggregate"]
        assert agg["strategy_id"] == "beta-2"
        assert agg["count"] == 2
        assert agg["average"] == 4.0  # (5 + 3) / 2
        assert agg["distribution"] == {
            "1": 0,
            "2": 0,
            "3": 1,
            "4": 0,
            "5": 1,
        }
        # No reviews were submitted, so the review listing is empty even
        # though the aggregate covers both ratings.
        assert body["reviews"] == []
        assert body["total"] == 0

    async def test_reviews_only_include_records_with_text(
        self, marketplace_client: AsyncClient
    ):
        await marketplace_client.post(
            self._RATINGS.format(strategy="gamma-3"),
            json={"stars": 4, "review": "Solid"},
        )
        await marketplace_client.post(
            self._RATINGS.format(strategy="gamma-3"),
            json={"stars": 2},
        )

        response = await marketplace_client.get(self._RATINGS.format(strategy="gamma-3"))
        assert response.status_code == HTTPStatus.OK
        body = response.json()
        # Aggregate covers both ratings...
        assert body["aggregate"]["count"] == 2
        # ...but only the text-bearing one shows up as a review.
        assert body["total"] == 1
        assert len(body["reviews"]) == 1
        assert body["reviews"][0]["review"] == "Solid"
        assert body["reviews"][0]["stars"] == 4

    async def test_get_ratings_for_unknown_strategy_is_empty(
        self, marketplace_client: AsyncClient
    ):
        response = await marketplace_client.get(self._RATINGS.format(strategy="brand-new"))
        assert response.status_code == HTTPStatus.OK
        body = response.json()
        assert body["aggregate"]["count"] == 0
        assert body["aggregate"]["average"] == 0.0
        assert body["reviews"] == []
        assert body["total"] == 0

    async def test_pagination_passed_through(self, marketplace_client: AsyncClient):
        for _ in range(3):
            await marketplace_client.post(
                self._RATINGS.format(strategy="delta-4"),
                json={"stars": 5, "review": "x"},
            )
        response = await marketplace_client.get(
            self._RATINGS.format(strategy="delta-4"),
            params={"limit": 1, "offset": 0},
        )
        assert response.status_code == HTTPStatus.OK
        body = response.json()
        assert body["total"] == 3
        assert body["limit"] == 1
        assert body["offset"] == 0
        assert len(body["reviews"]) == 1


class TestRatingsStoreUpsert:
    """Store-level upsert semantics that are awkward to drive over HTTP.

    The route override mints a *new* user per request, so to verify that
    re-submitting as the *same* user updates in place (preserving
    ``created_at``) we exercise the store directly with a fixed UUID.
    """

    def test_resubmit_updates_in_place_preserving_created_at(self):
        store = get_ratings_store()
        user = uuid.uuid4()
        first = store.submit_rating("eps-5", user, stars=2, review="meh")
        second = store.submit_rating("eps-5", user, stars=5, review="great")

        # Still a single record (upsert, not append).
        agg = store.get_aggregate("eps-5")
        assert agg.count == 1
        assert agg.average == 5.0

        # The original creation timestamp is preserved; updated_at moves on.
        assert second.created_at == first.created_at
        assert second.updated_at >= first.updated_at
        assert second.stars == 5
        assert second.review == "great"

        # The review listing reflects the updated payload, not the old one.
        page = store.list_reviews("eps-5")
        assert page.total == 1
        assert page.reviews[0].stars == 5

    def test_store_rejects_invalid_inputs(self):
        store = get_ratings_store()
        user = uuid.uuid4()
        # Non-integer / boolean stars are rejected at the store layer even
        # though the JSON route guards them via Pydantic first.
        with pytest.raises(InvalidRatingError):
            store.submit_rating("eps-5", user, stars=True)  # type: ignore[arg-type]
        with pytest.raises(InvalidRatingError):
            store.submit_rating("", user, stars=3)
