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
from engine.marketplace.search import (
    InMemoryStrategyCatalog,
    StrategyListing,
    get_strategy_catalog,
)


def _fake_user(role: str = "developer") -> User:
    return User(
        id=uuid.uuid4(),
        email="test@example.com",
        display_name="Test",
        is_active=True,
        role=role,
    )


def _listing(
    listing_id: str,
    name: str,
    *,
    description: str = "",
    author: str = "Tester",
    category: str = "algorithmic",
    tags: list[str] | None = None,
    rating: float = 0.0,
    downloads: int = 0,
    backtest_sharpe: float | None = None,
    min_capital: float = 0.0,
    created_at=None,
) -> StrategyListing:
    """Concise builder for :class:`StrategyListing` in tests."""
    return StrategyListing(
        id=listing_id,
        name=name,
        version="1.0.0",
        author=author,
        description=description,
        category=category,
        tags=list(tags) if tags is not None else [],
        rating=rating,
        downloads=downloads,
        backtest_sharpe=backtest_sharpe,
        min_capital=min_capital,
        created_at=created_at,
    )


@pytest.fixture
async def marketplace_client():
    app = FastAPI()
    app.include_router(marketplace_router, prefix="/api/v1/marketplace")
    app.dependency_overrides[get_current_user] = _fake_user
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


# ---------------------------------------------------------------------------
# Marketplace search (GET /search)
# ---------------------------------------------------------------------------


@pytest.fixture
async def search_client():
    """An isolated client wired to a fresh, empty in-memory catalog.

    Yields ``(client, catalog)`` so each test seeds its own deterministic data
    without touching the process-wide default catalog.
    """
    catalog = InMemoryStrategyCatalog()
    app = FastAPI()
    app.include_router(marketplace_router, prefix="/api/v1/marketplace")
    app.dependency_overrides[get_strategy_catalog] = lambda: catalog
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, catalog


class TestMarketplaceSearchEmptyQuery:
    async def test_empty_query_returns_all_listings(
        self, search_client: tuple[AsyncClient, InMemoryStrategyCatalog]
    ):
        client, catalog = search_client
        catalog.add_many(
            [
                _listing("a", "Alpha", downloads=10),
                _listing("b", "Beta", downloads=30),
                _listing("c", "Gamma", downloads=20),
            ]
        )
        response = await client.get("/api/v1/marketplace/search")
        assert response.status_code == HTTPStatus.OK
        data = response.json()
        assert data["total"] == 3
        assert len(data["results"]) == 3
        assert data["has_more"] is False
        assert data["query"] == ""

    async def test_empty_query_falls_back_to_downloads_sort(
        self, search_client: tuple[AsyncClient, InMemoryStrategyCatalog]
    ):
        client, catalog = search_client
        catalog.add_many(
            [
                _listing("low", "Low", downloads=1),
                _listing("high", "High", downloads=100),
                _listing("mid", "Mid", downloads=50),
            ]
        )
        response = await client.get("/api/v1/marketplace/search")
        assert response.status_code == HTTPStatus.OK
        data = response.json()
        # Relevance is meaningless with no query, so the endpoint should
        # transparently fall back to a downloads ordering.
        assert data["sort"] == "downloads"
        names = [r["name"] for r in data["results"]]
        assert names == ["High", "Mid", "Low"]

    async def test_empty_catalog_returns_empty_page(
        self, search_client: tuple[AsyncClient, InMemoryStrategyCatalog]
    ):
        client, _catalog = search_client
        response = await client.get("/api/v1/marketplace/search", params={"q": "anything"})
        assert response.status_code == HTTPStatus.OK
        data = response.json()
        assert data["total"] == 0
        assert data["results"] == []
        assert data["has_more"] is False


class TestMarketplaceSearchKeywordMatch:
    async def test_match_in_name(
        self, search_client: tuple[AsyncClient, InMemoryStrategyCatalog]
    ):
        client, catalog = search_client
        catalog.add_many(
            [
                _listing("mom", "Momentum Breakout", description="unrelated"),
                _listing("div", "Dividend Wheel"),
            ]
        )
        response = await client.get("/api/v1/marketplace/search", params={"q": "momentum"})
        assert response.status_code == HTTPStatus.OK
        data = response.json()
        assert data["total"] == 1
        assert data["results"][0]["id"] == "mom"

    async def test_match_in_description(
        self, search_client: tuple[AsyncClient, InMemoryStrategyCatalog]
    ):
        client, catalog = search_client
        catalog.add_many(
            [
                _listing("a", "Alpha", description="Uses statistical arbitrage"),
                _listing("b", "Beta", description="unrelated text"),
            ]
        )
        response = await client.get("/api/v1/marketplace/search", params={"q": "arbitrage"})
        data = response.json()
        assert data["total"] == 1
        assert data["results"][0]["id"] == "a"

    async def test_match_in_tags(
        self, search_client: tuple[AsyncClient, InMemoryStrategyCatalog]
    ):
        client, catalog = search_client
        catalog.add_many(
            [
                _listing("a", "Alpha", tags=["mean-reversion", "equities"]),
                _listing("b", "Beta", tags=["momentum"]),
            ]
        )
        response = await client.get("/api/v1/marketplace/search", params={"q": "equities"})
        data = response.json()
        assert data["total"] == 1
        assert data["results"][0]["id"] == "a"

    async def test_match_in_author(
        self, search_client: tuple[AsyncClient, InMemoryStrategyCatalog]
    ):
        client, catalog = search_client
        catalog.add_many(
            [
                _listing("a", "Alpha", author="Nexus Labs"),
                _listing("b", "Beta", author="Someone Else"),
            ]
        )
        response = await client.get("/api/v1/marketplace/search", params={"q": "nexus"})
        data = response.json()
        assert data["total"] == 1
        assert data["results"][0]["id"] == "a"

    async def test_no_match_returns_empty(
        self, search_client: tuple[AsyncClient, InMemoryStrategyCatalog]
    ):
        client, catalog = search_client
        catalog.add(_listing("a", "Alpha"))
        response = await client.get("/api/v1/marketplace/search", params={"q": "zzznomatch"})
        data = response.json()
        assert data["total"] == 0
        assert data["results"] == []

    async def test_multi_token_or_semantics(
        self, search_client: tuple[AsyncClient, InMemoryStrategyCatalog]
    ):
        client, catalog = search_client
        catalog.add_many(
            [
                _listing("mom", "Momentum"),
                _listing("inc", "Income"),
                _listing("nope", "Unrelated"),
            ]
        )
        response = await client.get("/api/v1/marketplace/search", params={"q": "momentum income"})
        data = response.json()
        ids = sorted(r["id"] for r in data["results"])
        assert ids == ["inc", "mom"]
        assert data["total"] == 2


class TestMarketplaceSearchCaseInsensitive:
    async def test_uppercase_query_matches_lowercase_name(
        self, search_client: tuple[AsyncClient, InMemoryStrategyCatalog]
    ):
        client, catalog = search_client
        catalog.add(_listing("mom", "Momentum Breakout"))
        response = await client.get("/api/v1/marketplace/search", params={"q": "MOMENTUM"})
        data = response.json()
        assert data["total"] == 1
        assert data["results"][0]["id"] == "mom"

    async def test_mixed_case_query_matches(
        self, search_client: tuple[AsyncClient, InMemoryStrategyCatalog]
    ):
        client, catalog = search_client
        catalog.add(_listing("mom", "Momentum Breakout"))
        response = await client.get("/api/v1/marketplace/search", params={"q": "MoMeNtUm"})
        data = response.json()
        assert data["total"] == 1
        assert data["results"][0]["id"] == "mom"

    async def test_category_filter_is_case_insensitive(
        self, search_client: tuple[AsyncClient, InMemoryStrategyCatalog]
    ):
        client, catalog = search_client
        catalog.add(_listing("a", "Alpha", category="algorithmic"))
        catalog.add(_listing("b", "Beta", category="income"))
        response = await client.get("/api/v1/marketplace/search", params={"category": "ALGORITHMIC"})
        data = response.json()
        assert data["total"] == 1
        assert data["results"][0]["id"] == "a"


class TestMarketplaceSearchPagination:
    @pytest.fixture
    def seeded_catalog(self, search_client):
        _, catalog = search_client
        # Distinct downloads so the default downloads ordering is stable.
        catalog.add_many(
            [
                _listing("s1", "One", downloads=100),
                _listing("s2", "Two", downloads=90),
                _listing("s3", "Three", downloads=80),
                _listing("s4", "Four", downloads=70),
                _listing("s5", "Five", downloads=60),
            ]
        )
        return search_client

    async def test_first_page_has_more(self, seeded_catalog):
        client, _ = seeded_catalog
        response = await client.get(
            "/api/v1/marketplace/search", params={"limit": 2, "page": 1}
        )
        data = response.json()
        assert len(data["results"]) == 2
        assert data["total"] == 5
        assert data["has_more"] is True
        assert data["page"] == 1
        assert data["limit"] == 2

    async def test_middle_page_has_more(self, seeded_catalog):
        client, _ = seeded_catalog
        response = await client.get(
            "/api/v1/marketplace/search", params={"limit": 2, "page": 2}
        )
        data = response.json()
        assert len(data["results"]) == 2
        assert data["total"] == 5
        assert data["has_more"] is True

    async def test_last_partial_page_no_more(self, seeded_catalog):
        client, _ = seeded_catalog
        response = await client.get(
            "/api/v1/marketplace/search", params={"limit": 2, "page": 3}
        )
        data = response.json()
        assert len(data["results"]) == 1
        assert data["total"] == 5
        assert data["has_more"] is False

    async def test_page_beyond_range_returns_empty_with_total(self, seeded_catalog):
        client, _ = seeded_catalog
        response = await client.get(
            "/api/v1/marketplace/search", params={"limit": 2, "page": 4}
        )
        data = response.json()
        assert data["results"] == []
        assert data["total"] == 5
        assert data["has_more"] is False

    async def test_exact_multiple_last_page_no_more(self, search_client):
        client, catalog = search_client
        catalog.add_many(
            [
                _listing("s1", "One", downloads=40),
                _listing("s2", "Two", downloads=30),
                _listing("s3", "Three", downloads=20),
                _listing("s4", "Four", downloads=10),
            ]
        )
        response = await client.get(
            "/api/v1/marketplace/search", params={"limit": 2, "page": 2}
        )
        data = response.json()
        assert len(data["results"]) == 2
        assert data["total"] == 4
        assert data["has_more"] is False

    async def test_limit_one_paginates_through_all(self, seeded_catalog):
        client, _ = seeded_catalog
        seen = []
        for page in range(1, 7):
            response = await client.get(
                "/api/v1/marketplace/search", params={"limit": 1, "page": page}
            )
            data = response.json()
            seen.extend(r["id"] for r in data["results"])
            assert data["has_more"] is (page < 5)
        # Every strategy appears exactly once across pages, in rank order.
        assert seen == ["s1", "s2", "s3", "s4", "s5"]


class TestMarketplaceSearchSorting:
    async def test_sort_by_downloads_descending(
        self, search_client: tuple[AsyncClient, InMemoryStrategyCatalog]
    ):
        client, catalog = search_client
        catalog.add_many(
            [
                _listing("low", "Low", downloads=1),
                _listing("high", "High", downloads=100),
                _listing("mid", "Mid", downloads=50),
            ]
        )
        response = await client.get("/api/v1/marketplace/search", params={"sort": "downloads"})
        data = response.json()
        assert [r["id"] for r in data["results"]] == ["high", "mid", "low"]

    async def test_sort_by_rating_descending(
        self, search_client: tuple[AsyncClient, InMemoryStrategyCatalog]
    ):
        client, catalog = search_client
        catalog.add_many(
            [
                _listing("a", "Alpha", rating=3.0, downloads=5),
                _listing("b", "Beta", rating=4.5, downloads=5),
                _listing("c", "Gamma", rating=4.0, downloads=5),
            ]
        )
        response = await client.get("/api/v1/marketplace/search", params={"sort": "rating"})
        data = response.json()
        assert [r["id"] for r in data["results"]] == ["b", "c", "a"]

    async def test_sort_by_name_ascending_case_insensitive(
        self, search_client: tuple[AsyncClient, InMemoryStrategyCatalog]
    ):
        client, catalog = search_client
        catalog.add_many(
            [
                _listing("z", "zebra"),
                _listing("a", "Apple"),
                _listing("m", "mango"),
            ]
        )
        response = await client.get("/api/v1/marketplace/search", params={"sort": "name"})
        data = response.json()
        assert [r["id"] for r in data["results"]] == ["a", "m", "z"]

    async def test_sort_by_newest_descending(
        self, search_client: tuple[AsyncClient, InMemoryStrategyCatalog]
    ):
        from datetime import UTC, datetime, timedelta

        base = datetime(2024, 1, 1, tzinfo=UTC)
        client, catalog = search_client
        catalog.add_many(
            [
                _listing("old", "Old", created_at=base),
                _listing("new", "New", created_at=base + timedelta(days=30)),
                _listing("mid", "Mid", created_at=base + timedelta(days=10)),
            ]
        )
        response = await client.get("/api/v1/marketplace/search", params={"sort": "newest"})
        data = response.json()
        assert [r["id"] for r in data["results"]] == ["new", "mid", "old"]

    async def test_sort_relevance_ranks_name_above_description(
        self, search_client: tuple[AsyncClient, InMemoryStrategyCatalog]
    ):
        client, catalog = search_client
        catalog.add_many(
            [
                # Only matches "momentum" in the description (low weight).
                _listing("desc", "Daily Driver", description="mentions momentum here"),
                # Matches "momentum" in the name (high weight + exact bonus).
                _listing("named", "Momentum", description="unrelated", downloads=0),
            ]
        )
        response = await client.get(
            "/api/v1/marketplace/search", params={"q": "momentum", "sort": "relevance"}
        )
        data = response.json()
        assert data["sort"] == "relevance"
        ids = [r["id"] for r in data["results"]]
        assert ids == ["named", "desc"]
        # The name-match should carry a strictly higher score.
        by_id = {r["id"]: r for r in data["results"]}
        assert by_id["named"]["score"] > by_id["desc"]["score"]

    async def test_invalid_sort_returns_400(
        self, search_client: tuple[AsyncClient, InMemoryStrategyCatalog]
    ):
        client, _catalog = search_client
        response = await client.get(
            "/api/v1/marketplace/search", params={"sort": "bogus"}
        )
        assert response.status_code == HTTPStatus.BAD_REQUEST


class TestMarketplaceSearchFilters:
    async def test_category_filter(
        self, search_client: tuple[AsyncClient, InMemoryStrategyCatalog]
    ):
        client, catalog = search_client
        catalog.add_many(
            [
                _listing("a", "Alpha", category="algorithmic"),
                _listing("b", "Beta", category="income"),
                _listing("c", "Gamma", category="algorithmic"),
            ]
        )
        response = await client.get(
            "/api/v1/marketplace/search", params={"category": "algorithmic"}
        )
        data = response.json()
        assert data["total"] == 2
        assert sorted(r["id"] for r in data["results"]) == ["a", "c"]

    async def test_tag_filter(
        self, search_client: tuple[AsyncClient, InMemoryStrategyCatalog]
    ):
        client, catalog = search_client
        catalog.add_many(
            [
                _listing("a", "Alpha", tags=["momentum", "futures"]),
                _listing("b", "Beta", tags=["income"]),
            ]
        )
        response = await client.get("/api/v1/marketplace/search", params={"tag": "futures"})
        data = response.json()
        assert data["total"] == 1
        assert data["results"][0]["id"] == "a"

    async def test_category_and_keyword_combine(
        self, search_client: tuple[AsyncClient, InMemoryStrategyCatalog]
    ):
        client, catalog = search_client
        catalog.add_many(
            [
                _listing("a", "Momentum Alpha", category="algorithmic"),
                _listing("b", "Momentum Beta", category="income"),
            ]
        )
        response = await client.get(
            "/api/v1/marketplace/search",
            params={"q": "momentum", "category": "income"},
        )
        data = response.json()
        assert data["total"] == 1
        assert data["results"][0]["id"] == "b"


class TestMarketplaceSearchResultShape:
    async def test_result_item_contains_all_fields(
        self, search_client: tuple[AsyncClient, InMemoryStrategyCatalog]
    ):
        from datetime import UTC, datetime

        client, catalog = search_client
        catalog.add(
            _listing(
                "mom",
                "Momentum",
                description="A momentum strategy",
                author="Nexus Labs",
                category="algorithmic",
                tags=["momentum"],
                rating=4.2,
                downloads=99,
                backtest_sharpe=1.3,
                min_capital=10000.0,
                created_at=datetime(2024, 5, 1, tzinfo=UTC),
            )
        )
        response = await client.get("/api/v1/marketplace/search", params={"q": "momentum"})
        data = response.json()
        item = data["results"][0]
        assert item["id"] == "mom"
        assert item["name"] == "Momentum"
        assert item["version"] == "1.0.0"
        assert item["author"] == "Nexus Labs"
        assert item["description"] == "A momentum strategy"
        assert item["category"] == "algorithmic"
        assert item["tags"] == ["momentum"]
        assert item["rating"] == 4.2
        assert item["downloads"] == 99
        assert item["backtest_sharpe"] == 1.3
        assert item["min_capital"] == 10000.0
        assert item["created_at"].startswith("2024-05-01")
        assert item["score"] > 0.0

    async def test_response_top_level_fields(
        self, search_client: tuple[AsyncClient, InMemoryStrategyCatalog]
    ):
        client, catalog = search_client
        catalog.add(_listing("a", "Alpha"))
        response = await client.get(
            "/api/v1/marketplace/search",
            params={"q": "alpha", "sort": "name", "page": 1, "limit": 5},
        )
        data = response.json()
        assert set(data) >= {
            "query", "sort", "results", "total", "page", "limit", "has_more"
        }
        assert data["query"] == "alpha"
        assert data["sort"] == "name"
        assert data["page"] == 1
        assert data["limit"] == 5


class TestMarketplaceSearchValidation:
    async def test_page_zero_returns_422(
        self, search_client: tuple[AsyncClient, InMemoryStrategyCatalog]
    ):
        client, _catalog = search_client
        response = await client.get("/api/v1/marketplace/search", params={"page": 0})
        assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY

    async def test_limit_zero_returns_422(
        self, search_client: tuple[AsyncClient, InMemoryStrategyCatalog]
    ):
        client, _catalog = search_client
        response = await client.get("/api/v1/marketplace/search", params={"limit": 0})
        assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY

    async def test_limit_above_max_returns_422(
        self, search_client: tuple[AsyncClient, InMemoryStrategyCatalog]
    ):
        client, _catalog = search_client
        response = await client.get("/api/v1/marketplace/search", params={"limit": 101})
        assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


class TestMarketplaceSearchNullCoalescing:
    """``_hit_to_item`` must never crash on ``None`` optional fields.

    Listings may be sourced from external systems (DB rows, remote APIs,
    hand-built fixtures) where a field the dataclass declares as a non-null
    ``str``/``float``/``int`` still arrives as ``None``. The route must
    coalesce these to sane defaults and — crucially — preserve legitimate
    falsy values (a real ``0.0`` rating, ``0`` downloads) instead of
    conflating them with "missing".
    """

    async def test_all_optional_fields_none_coerced_to_defaults(
        self, search_client: tuple[AsyncClient, InMemoryStrategyCatalog]
    ):
        client, catalog = search_client
        # Build the listing by hand (the ``_listing`` helper would pre-coerce
        # some fields), explicitly setting every optional field to ``None``.
        catalog.add(
            StrategyListing(
                id="nulls",
                name="Nullable Fields",
                version="1.0.0",
                author=None,
                description=None,
                category=None,
                tags=None,
                rating=None,
                downloads=None,
                backtest_sharpe=None,
                min_capital=None,
                created_at=None,
            )
        )
        # An empty query returns every listing without keyword scoring, which
        # would otherwise call ``.lower()`` on the None description/author.
        response = await client.get("/api/v1/marketplace/search")
        assert response.status_code == HTTPStatus.OK
        item = response.json()["results"][0]
        assert item["id"] == "nulls"
        assert item["author"] == ""
        assert item["description"] == ""
        assert item["category"] == ""
        assert item["tags"] == []
        assert item["rating"] == 0.0
        assert item["downloads"] == 0
        assert item["backtest_sharpe"] is None
        assert item["min_capital"] == 0.0
        assert item["created_at"] is None

    @pytest.mark.parametrize(
        ("field", "default"),
        [
            ("author", ""),
            ("description", ""),
            ("category", ""),
            ("rating", 0.0),
            ("downloads", 0),
            ("min_capital", 0.0),
        ],
    )
    async def test_each_optional_field_none_individually(
        self,
        search_client: tuple[AsyncClient, InMemoryStrategyCatalog],
        field: str,
        default,
    ):
        client, catalog = search_client
        catalog.reset()
        catalog.add(_listing("x", "Probe", **{field: None}))
        response = await client.get("/api/v1/marketplace/search")
        assert response.status_code == HTTPStatus.OK
        assert response.json()["results"][0][field] == default

    async def test_backtest_sharpe_none_round_trips(
        self, search_client: tuple[AsyncClient, InMemoryStrategyCatalog]
    ):
        client, catalog = search_client
        catalog.add(_listing("x", "Probe", backtest_sharpe=None))
        response = await client.get("/api/v1/marketplace/search")
        assert response.json()["results"][0]["backtest_sharpe"] is None

    async def test_backtest_sharpe_value_round_trips(
        self, search_client: tuple[AsyncClient, InMemoryStrategyCatalog]
    ):
        client, catalog = search_client
        catalog.add(_listing("x", "Probe", backtest_sharpe=2.5))
        response = await client.get("/api/v1/marketplace/search")
        assert response.json()["results"][0]["backtest_sharpe"] == 2.5

    async def test_legitimate_zero_values_are_preserved(
        self, search_client: tuple[AsyncClient, InMemoryStrategyCatalog]
    ):
        """A real 0.0 rating is NOT the same as "unrated" and must survive.

        Guards the medium-severity ``rating or 0.0`` regression: ``or`` would
        silently rewrite a legitimate ``0.0`` to the default, hiding the
        distinction between "rated zero" and "unrated".
        """
        client, catalog = search_client
        catalog.add(
            StrategyListing(
                id="zeros",
                name="Zero Rated",
                version="1.0.0",
                author="Tester",
                description="",
                category="algorithmic",
                rating=0.0,
                downloads=0,
                min_capital=0.0,
            )
        )
        response = await client.get("/api/v1/marketplace/search")
        assert response.status_code == HTTPStatus.OK
        item = response.json()["results"][0]
        assert item["rating"] == 0.0
        assert item["downloads"] == 0
        assert item["min_capital"] == 0.0

    async def test_none_tags_coerced_to_empty_list(
        self, search_client: tuple[AsyncClient, InMemoryStrategyCatalog]
    ):
        client, catalog = search_client
        # ``list(None)`` would raise TypeError; the route must guard it.
        catalog.add(
            StrategyListing(
                id="x",
                name="Probe",
                version="1.0.0",
                author="Tester",
                description="",
                category="algorithmic",
                tags=None,
            )
        )
        response = await client.get("/api/v1/marketplace/search")
        assert response.status_code == HTTPStatus.OK
        assert response.json()["results"][0]["tags"] == []
