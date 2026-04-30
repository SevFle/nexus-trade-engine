"""Tests for the /api/v1/reference search + suggest endpoints."""

from __future__ import annotations

from http import HTTPStatus

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from engine.api.routes.reference import (
    get_search_index,
)
from engine.api.routes.reference import (
    router as reference_router,
)
from engine.reference.model import RefInstrument
from engine.reference.search import SearchIndex


def _seeded_index() -> SearchIndex:
    idx = SearchIndex()
    idx.add(
        RefInstrument(
            primary_ticker="AAPL",
            primary_venue="XNAS",
            asset_class="equity",
            name="Apple Inc.",
        )
    )
    idx.add(
        RefInstrument(
            primary_ticker="MSFT",
            primary_venue="XNAS",
            asset_class="equity",
            name="Microsoft Corp.",
        )
    )
    idx.add(
        RefInstrument(
            primary_ticker="BRK.B",
            primary_venue="XNYS",
            asset_class="equity",
            name="Berkshire Hathaway Inc.",
        )
    )
    idx.add(
        RefInstrument(
            primary_ticker="ETH",
            primary_venue="XCRY",
            asset_class="crypto",
            name="Ethereum",
        )
    )
    return idx


@pytest.fixture
async def reference_client():
    app = FastAPI()
    app.include_router(reference_router, prefix="/api/v1/reference")
    app.dependency_overrides[get_search_index] = _seeded_index
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestSuggestEndpoint:
    async def test_ticker_prefix_suggests(self, reference_client: AsyncClient):
        r = await reference_client.get("/api/v1/reference/suggest", params={"q": "AA"})
        assert r.status_code == HTTPStatus.OK
        body = r.json()
        tickers = [s["record"]["primary_ticker"] for s in body["suggestions"]]
        assert "AAPL" in tickers

    async def test_name_prefix_suggests(self, reference_client: AsyncClient):
        r = await reference_client.get("/api/v1/reference/suggest", params={"q": "Micro"})
        assert r.status_code == HTTPStatus.OK
        body = r.json()
        assert body["suggestions"]
        assert body["suggestions"][0]["record"]["primary_ticker"] == "MSFT"

    async def test_typo_tolerant(self, reference_client: AsyncClient):
        r = await reference_client.get(
            "/api/v1/reference/suggest", params={"q": "Aple"}
        )
        assert r.status_code == HTTPStatus.OK
        body = r.json()
        tickers = [s["record"]["primary_ticker"] for s in body["suggestions"]]
        assert "AAPL" in tickers

    async def test_empty_query_returns_400(self, reference_client: AsyncClient):
        r = await reference_client.get("/api/v1/reference/suggest", params={"q": ""})
        assert r.status_code == HTTPStatus.BAD_REQUEST

    async def test_query_too_long_returns_400(self, reference_client: AsyncClient):
        r = await reference_client.get(
            "/api/v1/reference/suggest", params={"q": "x" * 200}
        )
        assert r.status_code == HTTPStatus.BAD_REQUEST

    async def test_default_limit(self, reference_client: AsyncClient):
        r = await reference_client.get("/api/v1/reference/suggest", params={"q": "A"})
        assert r.status_code == HTTPStatus.OK
        body = r.json()
        assert len(body["suggestions"]) <= 10

    async def test_explicit_limit_respected(self, reference_client: AsyncClient):
        r = await reference_client.get(
            "/api/v1/reference/suggest", params={"q": "A", "limit": 1}
        )
        assert r.status_code == HTTPStatus.OK
        body = r.json()
        assert len(body["suggestions"]) <= 1

    async def test_limit_clamped(self, reference_client: AsyncClient):
        r = await reference_client.get(
            "/api/v1/reference/suggest", params={"q": "A", "limit": 9999}
        )
        assert r.status_code == HTTPStatus.OK

    async def test_asset_class_filter(self, reference_client: AsyncClient):
        r = await reference_client.get(
            "/api/v1/reference/suggest",
            params={"q": "E", "asset_class": "crypto"},
        )
        assert r.status_code == HTTPStatus.OK
        body = r.json()
        for s in body["suggestions"]:
            assert s["record"]["asset_class"] == "crypto"

    async def test_no_match_returns_empty_list(self, reference_client: AsyncClient):
        r = await reference_client.get(
            "/api/v1/reference/suggest", params={"q": "xyznotapresent"}
        )
        assert r.status_code == HTTPStatus.OK
        assert r.json()["suggestions"] == []

    async def test_response_schema_has_completion_and_score(
        self, reference_client: AsyncClient
    ):
        r = await reference_client.get(
            "/api/v1/reference/suggest", params={"q": "Micro"}
        )
        body = r.json()
        first = body["suggestions"][0]
        assert "completion" in first
        assert "score" in first
        assert "record" in first

    async def test_response_surfaces_symbol_and_name_top_level(
        self, reference_client: AsyncClient
    ):
        # Frontend dropdown needs both ticker and company name visible
        # on every row regardless of which one matched the query.
        r = await reference_client.get(
            "/api/v1/reference/suggest", params={"q": "Micro"}
        )
        body = r.json()
        first = body["suggestions"][0]
        assert first["symbol"] == "MSFT"
        assert first["name"] == "Microsoft Corp."

    async def test_display_combines_symbol_and_name(
        self, reference_client: AsyncClient
    ):
        # When the query matches only the ticker, the dropdown row must
        # still show the company name so the user can confirm.
        r = await reference_client.get(
            "/api/v1/reference/suggest", params={"q": "AAPL"}
        )
        body = r.json()
        first = body["suggestions"][0]
        assert "AAPL" in first["display"]
        assert "Apple" in first["display"]

    async def test_name_match_still_shows_symbol(
        self, reference_client: AsyncClient
    ):
        # Reverse: query matched the company name; the symbol still has
        # to be visible on the row.
        r = await reference_client.get(
            "/api/v1/reference/suggest", params={"q": "Berkshire"}
        )
        body = r.json()
        first = body["suggestions"][0]
        assert first["symbol"] == "BRK.B"
        assert "BRK.B" in first["display"]
        assert "Berkshire" in first["display"]
