"""Tests for engine.reference.seed, engine.api.routes.reference Yahoo autosuggest,
and engine.reference.model edge cases — the most recently changed code paths.

Coverage targets:
  - seed_index() — bootstrap population of SearchIndex from curated seed data
  - _serialize_yahoo() / _map_quote_type() — Yahoo Finance result mapping
  - _yahoo_search() — external HTTP integration with timeout/error handling
  - Listing.is_active — active/inactive listing property
  - RefInstrument._ticker_no_whitespace — whitespace rejection validator
  - Suggest endpoint Yahoo fallback flow — when local index is empty
"""

from __future__ import annotations

from datetime import date
from http import HTTPStatus
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from engine.api.routes.reference import (
    _MAX_LIMIT,
    _MAX_QUERY_LEN,
    _map_quote_type,
    _serialize_yahoo,
    _yahoo_search,
    get_search_index,
)
from engine.api.routes.reference import (
    router as reference_router,
)
from engine.reference.model import Listing, RefInstrument
from engine.reference.search import SearchIndex
from engine.reference.seed import _INSTRUMENTS, seed_index


class TestSeedIndex:
    def test_returns_instrument_count(self):
        idx = SearchIndex()
        count = seed_index(idx)
        assert count == len(_INSTRUMENTS)
        assert count > 0

    def test_populates_index_records(self):
        idx = SearchIndex()
        seed_index(idx)
        results = idx.search("AAPL")
        tickers = [r.primary_ticker for r in results]
        assert "AAPL" in tickers

    def test_all_seed_data_validates_as_refinstrument(self):
        for row in _INSTRUMENTS:
            inst = RefInstrument(**row)
            assert inst.primary_ticker
            assert inst.primary_venue
            assert inst.asset_class
            assert inst.name

    def test_seed_covers_equity_asset_class(self):
        classes = {r["asset_class"] for r in _INSTRUMENTS}
        assert "equity" in classes

    def test_seed_covers_etf_asset_class(self):
        classes = {r["asset_class"] for r in _INSTRUMENTS}
        assert "etf" in classes

    def test_seed_covers_crypto_asset_class(self):
        classes = {r["asset_class"] for r in _INSTRUMENTS}
        assert "crypto" in classes

    def test_seed_covers_forex_asset_class(self):
        classes = {r["asset_class"] for r in _INSTRUMENTS}
        assert "forex" in classes

    def test_tickers_are_unique(self):
        tickers = [r["primary_ticker"] for r in _INSTRUMENTS]
        assert len(tickers) == len(set(tickers))

    def test_seeded_index_searchable_by_name(self):
        idx = SearchIndex()
        seed_index(idx)
        results = idx.search("Apple")
        assert any(r.primary_ticker == "AAPL" for r in results)

    def test_seeded_index_typeahead_suggest(self):
        idx = SearchIndex()
        seed_index(idx)
        suggestions = idx.suggest("GO")
        tickers = [s.record.primary_ticker for s in suggestions]
        assert any(t in tickers for t in ("GOOGL", "GOOG", "GOLD"))

    def test_seeded_index_asset_class_filter(self):
        idx = SearchIndex()
        seed_index(idx)
        results = idx.search("BTC", asset_class="crypto")
        for r in results:
            assert r.asset_class == "crypto"

    def test_idempotent_double_seed(self):
        idx = SearchIndex()
        c1 = seed_index(idx)
        c2 = seed_index(idx)
        assert c1 == c2
        assert len(idx._records) == c1 + c2

    def test_instruments_have_valid_venues(self):
        for row in _INSTRUMENTS:
            inst = RefInstrument(**row)
            assert len(inst.primary_venue) == 4

    def test_instruments_have_non_empty_names(self):
        for row in _INSTRUMENTS:
            inst = RefInstrument(**row)
            assert len(inst.name) >= 1

    def test_major_equities_present(self):
        tickers = {r["primary_ticker"] for r in _INSTRUMENTS}
        for expected in ("AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA"):
            assert expected in tickers, f"{expected} missing from seed data"

    def test_major_etfs_present(self):
        tickers = {r["primary_ticker"] for r in _INSTRUMENTS}
        for expected in ("SPY", "QQQ", "VOO", "IWM"):
            assert expected in tickers, f"{expected} missing from seed data"

    def test_major_crypto_present(self):
        tickers = {r["primary_ticker"] for r in _INSTRUMENTS}
        for expected in ("BTC-USD", "ETH-USD", "SOL-USD"):
            assert expected in tickers, f"{expected} missing from seed data"

    def test_major_forex_present(self):
        tickers = {r["primary_ticker"] for r in _INSTRUMENTS}
        for expected in ("EURUSD=X", "GBPUSD=X", "USDJPY=X"):
            assert expected in tickers, f"{expected} missing from seed data"


class TestSerializeYahoo:
    def test_full_data_mapping(self):
        item = {
            "symbol": "AAPL",
            "shortname": "Apple Inc.",
            "longname": "Apple Inc. (Full)",
            "quoteType": "EQUITY",
            "exchange": "XNAS",
            "currency": "USD",
        }
        result = _serialize_yahoo(item)
        assert result["symbol"] == "AAPL"
        assert result["name"] == "Apple Inc."
        assert "AAPL" in result["display"]
        assert "Apple" in result["display"]
        assert result["record"]["asset_class"] == "equity"
        assert result["record"]["primary_ticker"] == "AAPL"
        assert result["record"]["currency"] == "USD"

    def test_falls_back_to_longname(self):
        item = {
            "symbol": "TSLA",
            "longname": "Tesla Inc.",
            "quoteType": "EQUITY",
            "exchange": "XNAS",
        }
        result = _serialize_yahoo(item)
        assert result["name"] == "Tesla Inc."

    def test_falls_back_to_name_field(self):
        item = {
            "symbol": "X",
            "name": "US Dollar Index",
            "quoteType": "CURRENCY",
            "exchange": "XFXS",
        }
        result = _serialize_yahoo(item)
        assert result["name"] == "US Dollar Index"

    def test_empty_name_fields_uses_symbol(self):
        item = {
            "symbol": "FOO",
            "quoteType": "EQUITY",
            "exchange": "XNAS",
        }
        result = _serialize_yahoo(item)
        assert result["name"] == ""
        assert result["display"] == "FOO"

    def test_equity_score_higher_than_non_equity(self):
        equity = _serialize_yahoo(
            {"symbol": "A", "quoteType": "EQUITY", "exchange": "XNAS"}
        )
        crypto = _serialize_yahoo(
            {"symbol": "B", "quoteType": "CRYPTOCURRENCY", "exchange": "XCRY"}
        )
        assert equity["score"] > crypto["score"]

    def test_equity_score_is_80(self):
        result = _serialize_yahoo(
            {"symbol": "AAPL", "quoteType": "EQUITY", "exchange": "XNAS"}
        )
        assert result["score"] == 80

    def test_non_equity_score_is_60(self):
        for qt in ("ETF", "CRYPTOCURRENCY", "CURRENCY", "INDEX"):
            result = _serialize_yahoo(
                {"symbol": "X", "quoteType": qt, "exchange": "XNYS"}
            )
            assert result["score"] == 60

    def test_completion_uses_name_or_symbol(self):
        with_name = _serialize_yahoo(
            {"symbol": "A", "shortname": "Alpha", "quoteType": "EQUITY", "exchange": "XNAS"}
        )
        assert with_name["completion"] == "Alpha"

        no_name = _serialize_yahoo(
            {"symbol": "B", "quoteType": "EQUITY", "exchange": "XNAS"}
        )
        assert no_name["completion"] == "B"

    def test_record_id_is_empty_string(self):
        result = _serialize_yahoo(
            {"symbol": "X", "quoteType": "EQUITY", "exchange": "XNAS"}
        )
        assert result["record"]["id"] == ""


class TestMapQuoteType:
    @pytest.mark.parametrize(
        ("quote_type", "expected"),
        [
            ("EQUITY", "equity"),
            ("ETF", "etf"),
            ("MUTUALFUND", "etf"),
            ("CRYPTOCURRENCY", "crypto"),
            ("CURRENCY", "forex"),
            ("INDEX", "etf"),
            ("FUTURE", "future"),
            ("OPTION", "option"),
        ],
    )
    def test_known_mappings(self, quote_type, expected):
        assert _map_quote_type(quote_type) == expected

    def test_unknown_defaults_to_equity(self):
        assert _map_quote_type("UNKNOWN_TYPE") == "equity"

    def test_empty_string_defaults_to_equity(self):
        assert _map_quote_type("") == "equity"


class TestYahooSearch:
    async def test_successful_search_returns_results(self):
        mock_response = httpx.Response(
            200,
            json={
                "quotes": [
                    {
                        "symbol": "AAPL",
                        "shortname": "Apple Inc.",
                        "quoteType": "EQUITY",
                        "exchange": "XNAS",
                    },
                    {
                        "symbol": "MSFT",
                        "shortname": "Microsoft Corp.",
                        "quoteType": "EQUITY",
                        "exchange": "XNAS",
                    },
                ]
            },
        )
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response):
            results = await _yahoo_search("AAPL", 10)
            assert len(results) == 2
            assert results[0]["symbol"] == "AAPL"

    async def test_filters_none_quote_type(self):
        mock_response = httpx.Response(
            200,
            json={
                "quotes": [
                    {
                        "symbol": "BAD1",
                        "quoteType": "NONE",
                        "exchange": "XNAS",
                    },
                    {
                        "symbol": "BAD2",
                        "quoteType": None,
                        "exchange": "XNAS",
                    },
                    {
                        "symbol": "GOOD",
                        "shortname": "Good Corp.",
                        "quoteType": "EQUITY",
                        "exchange": "XNAS",
                    },
                ]
            },
        )
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response):
            results = await _yahoo_search("test", 10)
            assert len(results) == 1
            assert results[0]["symbol"] == "GOOD"

    async def test_filters_items_without_symbol(self):
        mock_response = httpx.Response(
            200,
            json={
                "quotes": [
                    {"quoteType": "EQUITY", "exchange": "XNAS"},
                    {"symbol": "", "quoteType": "EQUITY", "exchange": "XNAS"},
                    {
                        "symbol": "AAPL",
                        "shortname": "Apple Inc.",
                        "quoteType": "EQUITY",
                        "exchange": "XNAS",
                    },
                ]
            },
        )
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response):
            results = await _yahoo_search("test", 10)
            assert len(results) == 1
            assert results[0]["symbol"] == "AAPL"

    async def test_respects_limit(self):
        quotes = [
            {"symbol": f"T{i}", "quoteType": "EQUITY", "exchange": "XNAS"}
            for i in range(20)
        ]
        mock_response = httpx.Response(200, json={"quotes": quotes})
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response):
            results = await _yahoo_search("test", 5)
            assert len(results) == 5

    async def test_http_non_200_returns_empty(self):
        mock_response = httpx.Response(500, json={"error": "internal"})
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response):
            results = await _yahoo_search("test", 10)
            assert results == []

    async def test_timeout_returns_empty(self):
        with patch(
            "httpx.AsyncClient.get",
            new_callable=AsyncMock,
            side_effect=httpx.TimeoutException("timeout"),
        ):
            results = await _yahoo_search("test", 10)
            assert results == []

    async def test_request_error_returns_empty(self):
        with patch(
            "httpx.AsyncClient.get",
            new_callable=AsyncMock,
            side_effect=httpx.RequestError("connection failed"),
        ):
            results = await _yahoo_search("test", 10)
            assert results == []

    async def test_unexpected_exception_returns_empty(self):
        with patch(
            "httpx.AsyncClient.get",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            results = await _yahoo_search("test", 10)
            assert results == []

    async def test_empty_quotes_returns_empty(self):
        mock_response = httpx.Response(200, json={"quotes": []})
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response):
            results = await _yahoo_search("test", 10)
            assert results == []

    async def test_null_quotes_returns_empty(self):
        mock_response = httpx.Response(200, json={"quotes": None})
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response):
            results = await _yahoo_search("test", 10)
            assert results == []


class TestListingIsActive:
    def test_active_when_no_end_date(self):
        listing = Listing(
            venue="XNAS",
            ticker="AAPL",
            currency="USD",
            active_from=date(2020, 1, 1),
        )
        assert listing.is_active is True

    def test_inactive_with_end_date(self):
        listing = Listing(
            venue="XNAS",
            ticker="AAPL",
            currency="USD",
            active_from=date(2020, 1, 1),
            active_to=date(2024, 1, 1),
        )
        assert listing.is_active is False


class TestTickerWhitespaceValidator:
    def test_leading_whitespace_rejected(self):
        with pytest.raises(ValidationError):
            RefInstrument(
                primary_ticker=" AAPL",
                primary_venue="XNAS",
                asset_class="equity",
                name="Apple",
            )

    def test_trailing_whitespace_rejected(self):
        with pytest.raises(ValidationError):
            RefInstrument(
                primary_ticker="AAPL ",
                primary_venue="XNAS",
                asset_class="equity",
                name="Apple",
            )

    def test_whitespace_only_rejected(self):
        with pytest.raises(ValidationError):
            RefInstrument(
                primary_ticker="   ",
                primary_venue="XNAS",
                asset_class="equity",
                name="Apple",
            )

    def test_tab_in_ticker_rejected(self):
        with pytest.raises(ValidationError):
            RefInstrument(
                primary_ticker="\tAAPL",
                primary_venue="XNAS",
                asset_class="equity",
                name="Apple",
            )

    def test_clean_ticker_accepted(self):
        inst = RefInstrument(
            primary_ticker="AAPL",
            primary_venue="XNAS",
            asset_class="equity",
            name="Apple",
        )
        assert inst.primary_ticker == "AAPL"


class TestSuggestYahooFallback:
    @pytest.fixture
    async def fallback_client(self):
        app = FastAPI()
        app.include_router(reference_router, prefix="/api/v1/reference")
        app.dependency_overrides[get_search_index] = SearchIndex
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    async def test_local_empty_falls_back_to_yahoo(self, fallback_client: AsyncClient):
        yahoo_results = [
            {
                "symbol": "ZZZZ",
                "name": "Zebra Corp.",
                "display": "ZZZZ — Zebra Corp.",
                "completion": "Zebra Corp.",
                "score": 80,
                "record": {
                    "id": "",
                    "primary_ticker": "ZZZZ",
                    "primary_venue": "XNAS",
                    "asset_class": "equity",
                    "name": "Zebra Corp.",
                    "currency": "USD",
                },
            }
        ]
        with patch(
            "engine.api.routes.reference._yahoo_search",
            new_callable=AsyncMock,
            return_value=yahoo_results,
        ):
            r = await fallback_client.get(
                "/api/v1/reference/suggest", params={"q": "ZZZZ"}
            )
            assert r.status_code == HTTPStatus.OK
            body = r.json()
            assert len(body["suggestions"]) == 1
            assert body["suggestions"][0]["symbol"] == "ZZZZ"

    async def test_yahoo_results_filtered_by_asset_class(
        self, fallback_client: AsyncClient
    ):
        yahoo_results = [
            {
                "symbol": "AAPL",
                "name": "Apple",
                "display": "AAPL — Apple",
                "completion": "Apple",
                "score": 80,
                "record": {
                    "id": "",
                    "primary_ticker": "AAPL",
                    "primary_venue": "XNAS",
                    "asset_class": "equity",
                    "name": "Apple",
                    "currency": "USD",
                },
            },
            {
                "symbol": "BTC-USD",
                "name": "Bitcoin",
                "display": "BTC-USD — Bitcoin",
                "completion": "Bitcoin",
                "score": 60,
                "record": {
                    "id": "",
                    "primary_ticker": "BTC-USD",
                    "primary_venue": "XCRY",
                    "asset_class": "crypto",
                    "name": "Bitcoin",
                    "currency": "USD",
                },
            },
        ]
        with patch(
            "engine.api.routes.reference._yahoo_search",
            new_callable=AsyncMock,
            return_value=yahoo_results,
        ):
            r = await fallback_client.get(
                "/api/v1/reference/suggest",
                params={"q": "test", "asset_class": "crypto"},
            )
            assert r.status_code == HTTPStatus.OK
            body = r.json()
            for s in body["suggestions"]:
                assert s["record"]["asset_class"] == "crypto"

    async def test_both_local_and_yahoo_empty_returns_empty(
        self, fallback_client: AsyncClient
    ):
        with patch(
            "engine.api.routes.reference._yahoo_search",
            new_callable=AsyncMock,
            return_value=[],
        ):
            r = await fallback_client.get(
                "/api/v1/reference/suggest", params={"q": "zzznonexistent"}
            )
            assert r.status_code == HTTPStatus.OK
            assert r.json()["suggestions"] == []

    async def test_local_hit_skips_yahoo(self, fallback_client: AsyncClient):
        idx = SearchIndex()
        idx.add(
            RefInstrument(
                primary_ticker="AAPL",
                primary_venue="XNAS",
                asset_class="equity",
                name="Apple Inc.",
            )
        )
        fallback_client._transport.app.dependency_overrides[get_search_index] = lambda: idx

        with patch(
            "engine.api.routes.reference._yahoo_search",
            new_callable=AsyncMock,
            side_effect=AssertionError("Yahoo should not be called"),
        ):
            r = await fallback_client.get(
                "/api/v1/reference/suggest", params={"q": "AAPL"}
            )
            assert r.status_code == HTTPStatus.OK
            assert len(r.json()["suggestions"]) >= 1

    async def test_whitespace_only_query_returns_400(self, fallback_client: AsyncClient):
        r = await fallback_client.get(
            "/api/v1/reference/suggest", params={"q": "   "}
        )
        assert r.status_code == HTTPStatus.BAD_REQUEST


class TestSerializeYahooEdgeCases:
    def test_display_format_with_name(self):
        result = _serialize_yahoo(
            {"symbol": "AAPL", "shortname": "Apple Inc.", "quoteType": "EQUITY", "exchange": "XNAS"}
        )
        assert result["display"] == "AAPL — Apple Inc."

    def test_display_format_without_name(self):
        result = _serialize_yahoo(
            {"symbol": "FOO", "quoteType": "EQUITY", "exchange": "XNAS"}
        )
        assert result["display"] == "FOO"

    def test_default_currency_is_usd(self):
        result = _serialize_yahoo(
            {"symbol": "X", "quoteType": "EQUITY", "exchange": "XNAS"}
        )
        assert result["record"]["currency"] == "USD"

    def test_custom_currency_preserved(self):
        result = _serialize_yahoo(
            {"symbol": "X", "quoteType": "EQUITY", "exchange": "XNAS", "currency": "EUR"}
        )
        assert result["record"]["currency"] == "EUR"


class TestMaxQueryLength:
    async def test_max_query_len_boundary(self):
        app = FastAPI()
        app.include_router(reference_router, prefix="/api/v1/reference")
        app.dependency_overrides[get_search_index] = SearchIndex
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.get(
                "/api/v1/reference/suggest",
                params={"q": "x" * _MAX_QUERY_LEN},
            )
            assert r.status_code == HTTPStatus.OK

            r = await ac.get(
                "/api/v1/reference/suggest",
                params={"q": "x" * (_MAX_QUERY_LEN + 1)},
            )
            assert r.status_code == HTTPStatus.BAD_REQUEST


class TestLimitClamping:
    async def test_limit_capped_to_max(self):
        app = FastAPI()
        app.include_router(reference_router, prefix="/api/v1/reference")
        idx = SearchIndex()
        for i in range(60):
            idx.add(
                RefInstrument(
                    primary_ticker=f"T{i:03d}",
                    primary_venue="XNAS",
                    asset_class="equity",
                    name=f"Test Corp {i}",
                )
            )
        app.dependency_overrides[get_search_index] = lambda: idx
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.get(
                "/api/v1/reference/suggest",
                params={"q": "T", "limit": 999},
            )
            assert r.status_code == HTTPStatus.OK
            assert len(r.json()["suggestions"]) <= _MAX_LIMIT
