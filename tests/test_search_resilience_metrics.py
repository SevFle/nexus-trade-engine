"""Tests for SearchIndex internals, resilience primitives, observability metrics,
and reference API edge cases — targeting uncovered lines identified by coverage analysis.

Coverage targets:
  - engine/reference/search.py — search(), suggest(), _suggest_score(), _fuzzy_match(),
    _score(), _tokenize_name(), _within_one_edit() (17% when run standalone)
  - engine/data/providers/_resilience.py — TokenBucket, call_with_retry (31%)
  - engine/observability/metrics.py — NullBackend, RecordingBackend, _check_name,
    _canonical_tags, set_metrics thread safety (44%)
  - engine/api/routes/reference.py — _serialize(), get_search_index() singleton
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import date
from http import HTTPStatus
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from engine.api.routes.reference import (
    _serialize,
    get_search_index,
)
from engine.api.routes.reference import router as reference_router
from engine.data.providers.base import (
    FatalProviderError,
    RateLimit,
    TransientProviderError,
)
from engine.data.providers._resilience import (
    TokenBucket,
    call_with_retry,
)
from engine.observability.metrics import (
    NullBackend,
    RecordingBackend,
    _canonical_tags,
    _check_name,
    get_metrics,
    set_metrics,
)
from engine.reference.model import RefInstrument
from engine.reference.search import (
    SearchIndex,
    _tokenize_name,
    _within_one_edit,
)
from engine.reference.seed import seed_index


# ---------------------------------------------------------------------------
# SearchIndex.search() — comprehensive edge cases
# ---------------------------------------------------------------------------


class TestSearchIndexSearchEdgeCases:
    def _populated(self) -> SearchIndex:
        idx = SearchIndex()
        idx.add(RefInstrument(primary_ticker="AAPL", primary_venue="XNAS", asset_class="equity", name="Apple Inc."))
        idx.add(RefInstrument(primary_ticker="MSFT", primary_venue="XNAS", asset_class="equity", name="Microsoft Corp."))
        idx.add(RefInstrument(primary_ticker="BRK.B", primary_venue="XNYS", asset_class="equity", name="Berkshire Hathaway Inc. Class B"))
        idx.add(RefInstrument(primary_ticker="BTC-USD", primary_venue="XCRY", asset_class="crypto", name="Bitcoin USD"))
        idx.add(RefInstrument(primary_ticker="ETH-USD", primary_venue="XCRY", asset_class="crypto", name="Ethereum USD"))
        idx.add(RefInstrument(primary_ticker="SPY", primary_venue="XASE", asset_class="etf", name="SPDR S&P 500 ETF Trust"))
        idx.add(RefInstrument(primary_ticker="QQQ", primary_venue="XNAS", asset_class="etf", name="Invesco QQQ Trust"))
        return idx

    def test_empty_query_returns_empty(self):
        assert self._populated().search("") == []

    def test_whitespace_only_query_returns_empty(self):
        assert self._populated().search("   ") == []

    def test_query_over_max_len_returns_empty(self):
        q = "x" * (SearchIndex.MAX_QUERY_LEN + 1)
        assert self._populated().search(q) == []

    def test_query_at_max_len_does_not_error(self):
        q = "a" * SearchIndex.MAX_QUERY_LEN
        results = self._populated().search(q)
        assert isinstance(results, list)

    def test_exact_ticker_match(self):
        results = self._populated().search("AAPL")
        assert results and results[0].primary_ticker == "AAPL"

    def test_exact_name_match(self):
        results = self._populated().search("apple inc.")
        assert results and results[0].primary_ticker == "AAPL"

    def test_ticker_prefix(self):
        results = self._populated().search("AA")
        assert results and any(r.primary_ticker == "AAPL" for r in results)

    def test_name_prefix(self):
        results = self._populated().search("micro")
        assert results and results[0].primary_ticker == "MSFT"

    def test_ticker_contains(self):
        results = self._populated().search("pl")
        assert any(r.primary_ticker == "AAPL" for r in results)

    def test_name_contains(self):
        results = self._populated().search("bitcoin")
        assert results and results[0].primary_ticker == "BTC-USD"

    def test_word_token_prefix(self):
        results = self._populated().search("hath")
        assert any(r.primary_ticker == "BRK.B" for r in results)

    def test_asset_class_filter(self):
        results = self._populated().search("a", asset_class="crypto")
        for r in results:
            assert r.asset_class == "crypto"

    def test_no_match_returns_empty(self):
        assert self._populated().search("zzzznotfound") == []

    def test_limit_is_respected(self):
        results = self._populated().search("a", limit=1)
        assert len(results) <= 1

    def test_case_insensitive(self):
        idx = self._populated()
        assert idx.search("aapl")[0].primary_ticker == idx.search("AAPL")[0].primary_ticker

    def test_whitespace_is_stripped(self):
        results = self._populated().search("  AAPL  ")
        assert results and results[0].primary_ticker == "AAPL"

    def test_empty_index_returns_empty(self):
        assert SearchIndex().search("anything") == []

    def test_heapsort_large_result_set(self):
        idx = SearchIndex()
        for i in range(200):
            idx.add(RefInstrument(primary_ticker=f"T{i:04d}", primary_venue="XNAS", asset_class="equity", name=f"Company {i}"))
        results = idx.search("T", limit=5)
        assert len(results) <= 5
        assert len(results) > 0


# ---------------------------------------------------------------------------
# SearchIndex.suggest() — scoring tiers and fuzzy matching
# ---------------------------------------------------------------------------


class TestSearchIndexSuggestScoring:
    def _idx(self) -> SearchIndex:
        idx = SearchIndex()
        idx.add(RefInstrument(primary_ticker="AAPL", primary_venue="XNAS", asset_class="equity", name="Apple Inc."))
        idx.add(RefInstrument(primary_ticker="GOOGL", primary_venue="XNAS", asset_class="equity", name="Alphabet Inc."))
        idx.add(RefInstrument(primary_ticker="TSLA", primary_venue="XNAS", asset_class="equity", name="Tesla Inc."))
        idx.add(RefInstrument(primary_ticker="NVDA", primary_venue="XNAS", asset_class="equity", name="NVIDIA Corp."))
        idx.add(RefInstrument(primary_ticker="MCK", primary_venue="XNYS", asset_class="equity", name="McKesson Corp."))
        return idx

    def test_empty_query(self):
        assert self._idx().suggest("") == []

    def test_whitespace_query(self):
        assert self._idx().suggest("   ") == []

    def test_over_max_len(self):
        assert self._idx().suggest("x" * (SearchIndex.MAX_QUERY_LEN + 1)) == []

    def test_exact_ticker_score_100(self):
        results = self._idx().suggest("AAPL")
        assert results[0].score == 100

    def test_exact_name_score_90(self):
        idx = SearchIndex()
        idx.add(RefInstrument(primary_ticker="XYZ", primary_venue="XNAS", asset_class="equity", name="Zebra Corp."))
        results = idx.suggest("zebra corp.")
        assert results[0].score == 90

    def test_ticker_prefix_score_80(self):
        results = self._idx().suggest("AAP")
        assert results[0].score == 80

    def test_name_token_exact_score_78(self):
        results = self._idx().suggest("corp")
        assert any(s.score == 78 for s in results)

    def test_first_token_prefix_score_75(self):
        results = self._idx().suggest("appl")
        assert results[0].score == 75

    def test_later_token_prefix_score_70(self):
        idx = SearchIndex()
        idx.add(RefInstrument(primary_ticker="Z", primary_venue="XNAS", asset_class="equity", name="The Zephyr Company"))
        results = idx.suggest("zeph")
        assert results[0].score == 70

    def test_ticker_contains_score_60(self):
        idx = SearchIndex()
        idx.add(RefInstrument(primary_ticker="XAPLX", primary_venue="XNAS", asset_class="equity", name="Random Corp."))
        results = idx.suggest("apl")
        assert results[0].score == 60

    def test_name_contains_score_25(self):
        idx = SearchIndex()
        idx.add(RefInstrument(primary_ticker="ZZZ", primary_venue="XNAS", asset_class="equity", name="Something Xylophonic Related"))
        results = idx.suggest("ylop")
        assert results[0].score == 25

    def test_fuzzy_typo_score_15(self):
        results = self._idx().suggest("aple")
        assert results
        assert results[0].score == 15
        assert results[0].record.primary_ticker == "AAPL"

    def test_fuzzy_not_diluted_when_prefix_exists(self):
        idx = SearchIndex()
        idx.add(RefInstrument(primary_ticker="APL", primary_venue="XNAS", asset_class="equity", name="Apollo"))
        results = idx.suggest("apl")
        assert results[0].score >= 80

    def test_fuzzy_name_token_match(self):
        idx = SearchIndex()
        idx.add(RefInstrument(primary_ticker="MCK", primary_venue="XNYS", asset_class="equity", name="McKesson Corp."))
        results = idx.suggest("mckeeson")
        assert results
        assert results[0].score == 15

    def test_suggestions_sorted_desc(self):
        results = self._idx().suggest("A")
        scores = [s.score for s in results]
        assert scores == sorted(scores, reverse=True)

    def test_default_limit(self):
        idx = SearchIndex()
        for i in range(20):
            idx.add(RefInstrument(primary_ticker=f"T{i:02d}", primary_venue="XNAS", asset_class="equity", name=f"Test {i}"))
        results = idx.suggest("T")
        assert len(results) <= SearchIndex.DEFAULT_SUGGEST_LIMIT

    def test_custom_limit(self):
        idx = SearchIndex()
        for i in range(20):
            idx.add(RefInstrument(primary_ticker=f"T{i:02d}", primary_venue="XNAS", asset_class="equity", name=f"Test {i}"))
        results = idx.suggest("T", limit=3)
        assert len(results) <= 3

    def test_asset_class_filter(self):
        results = self._idx().suggest("A", asset_class="crypto")
        for s in results:
            assert s.record.asset_class == "crypto"

    def test_suggestion_completion_field(self):
        results = self._idx().suggest("AAPL")
        assert results[0].completion == "AAPL"

    def test_suggestion_record_field(self):
        results = self._idx().suggest("AAPL")
        assert results[0].record.primary_ticker == "AAPL"

    def test_no_match_at_all_returns_empty(self):
        idx = SearchIndex()
        idx.add(RefInstrument(primary_ticker="ZZZ", primary_venue="XNAS", asset_class="equity", name="Totally Different"))
        results = idx.suggest("abcd1234xyz")
        assert results == []


# ---------------------------------------------------------------------------
# _tokenize_name — unit tests
# ---------------------------------------------------------------------------


class TestTokenizeName:
    def test_simple(self):
        assert _tokenize_name("Apple Inc.") == ["Apple", "Inc"]

    def test_multiple_spaces(self):
        assert _tokenize_name("  Apple   Inc.  ") == ["Apple", "Inc"]

    def test_hyphen_as_delimiter(self):
        assert _tokenize_name("Berkshire-Hathaway") == ["Berkshire", "Hathaway"]

    def test_comma_as_delimiter(self):
        assert _tokenize_name("Apple, Inc.") == ["Apple", "Inc"]

    def test_empty_string(self):
        assert _tokenize_name("") == []

    def test_single_word(self):
        assert _tokenize_name("Apple") == ["Apple"]

    def test_numbers_preserved(self):
        assert _tokenize_name("S&P 500") == ["S", "P", "500"]

    def test_dot_in_ticker_not_name(self):
        assert _tokenize_name("BRK.B") == ["BRK", "B"]

    def test_trailing_punct(self):
        assert _tokenize_name("Hello!") == ["Hello"]

    def test_leading_punct(self):
        assert _tokenize_name("!Hello") == ["Hello"]

    def test_only_punct(self):
        assert _tokenize_name("!@#$") == []

    def test_ampersand(self):
        assert _tokenize_name("Johnson & Johnson") == ["Johnson", "Johnson"]


# ---------------------------------------------------------------------------
# _within_one_edit — unit tests
# ---------------------------------------------------------------------------


class TestWithinOneEdit:
    def test_identical(self):
        assert _within_one_edit("abc", "abc") is True

    def test_one_insertion(self):
        assert _within_one_edit("abc", "abcd") is True

    def test_one_deletion(self):
        assert _within_one_edit("abcd", "abc") is True

    def test_one_substitution(self):
        assert _within_one_edit("abc", "axc") is True

    def test_two_edits_false(self):
        assert _within_one_edit("abc", "axy") is False

    def test_length_diff_2_false(self):
        assert _within_one_edit("a", "abc") is False

    def test_empty_both(self):
        assert _within_one_edit("", "") is True

    def test_single_char_diff(self):
        assert _within_one_edit("", "a") is True

    def test_single_char_reverse(self):
        assert _within_one_edit("a", "") is True

    def test_trailing_insertion(self):
        assert _within_one_edit("abc", "abcd") is True

    def test_leading_insertion(self):
        assert _within_one_edit("bc", "abc") is True

    def test_middle_insertion(self):
        assert _within_one_edit("ac", "abc") is True

    @pytest.mark.parametrize(
        ("a", "b", "expected"),
        [
            ("aple", "apple", True),
            ("tsla", "tsla", True),
            ("tsla", "tsl", True),
            ("tsla", "tslb", True),
            ("tsla", "txlb", False),
            ("tsla", "txly", False),
            ("a", "bc", False),
            ("abc", "abcde", False),
        ],
    )
    def test_parametrized(self, a, b, expected):
        assert _within_one_edit(a, b) is expected


# ---------------------------------------------------------------------------
# TokenBucket — edge cases
# ---------------------------------------------------------------------------


class TestTokenBucketEdgeCases:
    async def test_zero_rpm_capacity_zero(self):
        b = TokenBucket(RateLimit(requests_per_minute=0))
        assert b._capacity == 0

    async def test_negative_rpm_capacity_zero(self):
        b = TokenBucket(RateLimit(requests_per_minute=-10))
        assert b._capacity == 0

    async def test_zero_burst_defaults_to_one(self):
        b = TokenBucket(RateLimit(requests_per_minute=60, burst=0))
        assert b._capacity == 1

    async def test_burst_sets_capacity(self):
        b = TokenBucket(RateLimit(requests_per_minute=600, burst=10))
        assert b._capacity == 10

    async def test_refill_rate_calculation(self):
        b = TokenBucket(RateLimit(requests_per_minute=600, burst=10))
        assert b._refill_per_second == pytest.approx(10.0)

    async def test_unlimited_acquire_never_blocks(self):
        b = TokenBucket(RateLimit(requests_per_minute=0))
        for _ in range(50):
            await b.acquire()

    async def test_tokens_refill_after_consumption(self):
        b = TokenBucket(RateLimit(requests_per_minute=6000, burst=1))
        await b.acquire()
        assert b._tokens < 1.0
        await asyncio.sleep(0.02)
        await b.acquire()

    async def test_concurrent_acquire_serialized(self):
        b = TokenBucket(RateLimit(requests_per_minute=600, burst=10))
        done = []

        async def worker():
            await b.acquire()
            done.append(1)

        await asyncio.gather(*[worker() for _ in range(10)])
        assert len(done) == 10


# ---------------------------------------------------------------------------
# call_with_retry — edge cases
# ---------------------------------------------------------------------------


class TestCallWithRetryEdgeCases:
    async def test_timeout_error_retried(self):
        n = {"c": 0}

        async def fn():
            n["c"] += 1
            if n["c"] < 2:
                raise TimeoutError("timeout")
            return "ok"

        result = await call_with_retry(fn, provider="t", base_delay_s=0.0)
        assert result == "ok"
        assert n["c"] == 2

    async def test_single_attempt(self):
        async def fn():
            return "done"

        result = await call_with_retry(fn, provider="t", max_attempts=1)
        assert result == "done"

    async def test_mixed_transient_and_timeout(self):
        n = {"c": 0}

        async def fn():
            n["c"] += 1
            if n["c"] == 1:
                raise TransientProviderError("boom")
            if n["c"] == 2:
                raise TimeoutError("timeout")
            return "ok"

        result = await call_with_retry(fn, provider="t", base_delay_s=0.0)
        assert result == "ok"
        assert n["c"] == 3

    async def test_fatal_propagates_immediately(self):
        n = {"c": 0}

        async def fn():
            n["c"] += 1
            raise FatalProviderError("nope")

        with pytest.raises(FatalProviderError):
            await call_with_retry(fn, provider="t")
        assert n["c"] == 1

    async def test_exhausts_attempts(self):
        n = {"c": 0}

        async def fn():
            n["c"] += 1
            raise TransientProviderError("fail")

        with pytest.raises(TransientProviderError):
            await call_with_retry(fn, provider="t", max_attempts=3, base_delay_s=0.0)
        assert n["c"] == 3


# ---------------------------------------------------------------------------
# NullBackend — all methods validate name
# ---------------------------------------------------------------------------


class TestNullBackendNameValidation:
    def test_counter_empty(self):
        with pytest.raises(ValueError):
            NullBackend().counter("")

    def test_counter_whitespace(self):
        with pytest.raises(ValueError):
            NullBackend().counter("   ")

    def test_gauge_empty(self):
        with pytest.raises(ValueError):
            NullBackend().gauge("", 1.0)

    def test_gauge_whitespace(self):
        with pytest.raises(ValueError):
            NullBackend().gauge("   ", 1.0)

    def test_histogram_empty(self):
        with pytest.raises(ValueError):
            NullBackend().histogram("", 1.0)

    def test_histogram_whitespace(self):
        with pytest.raises(ValueError):
            NullBackend().histogram("   ", 1.0)

    def test_timer_empty(self):
        with pytest.raises(ValueError):
            with NullBackend().timer(""):
                pass

    def test_timer_whitespace(self):
        with pytest.raises(ValueError):
            with NullBackend().timer("   "):
                pass

    def test_valid_names_pass(self):
        b = NullBackend()
        b.counter("ok")
        b.gauge("ok", 1)
        b.histogram("ok", 1)
        with b.timer("ok"):
            pass


# ---------------------------------------------------------------------------
# RecordingBackend — comprehensive
# ---------------------------------------------------------------------------


class TestRecordingBackendComprehensive:
    def test_counter_negative(self):
        b = RecordingBackend()
        b.counter("x", -5.0)
        assert b.counters == {("x", ()): -5.0}

    def test_counter_different_tags(self):
        b = RecordingBackend()
        b.counter("x", 1, tags={"a": "1"})
        b.counter("x", 1, tags={"b": "2"})
        assert len(b.counters) == 2

    def test_counter_accumulates(self):
        b = RecordingBackend()
        b.counter("x", 3)
        b.counter("x", 7)
        assert b.counters == {("x", ()): 10.0}

    def test_gauge_last_write_wins(self):
        b = RecordingBackend()
        b.gauge("x", 10)
        b.gauge("x", 5)
        assert b.gauges == {("x", ()): 5.0}

    def test_gauge_with_tags(self):
        b = RecordingBackend()
        b.gauge("x", 1, tags={"env": "prod"})
        b.gauge("x", 2, tags={"env": "dev"})
        assert len(b.gauges) == 2

    def test_histogram_multiple_observations(self):
        b = RecordingBackend()
        b.histogram("x", 1.0)
        b.histogram("x", 2.0)
        assert b.histograms == {("x", ()): [1.0, 2.0]}

    def test_timer_elapsed_ms(self):
        b = RecordingBackend()
        with b.timer("op"):
            time.sleep(0.01)
        obs = b.histograms[("op", ())]
        assert len(obs) == 1
        assert obs[0] >= 10.0

    def test_timer_on_exception(self):
        b = RecordingBackend()
        with pytest.raises(RuntimeError):
            with b.timer("op"):
                raise RuntimeError("boom")
        assert ("op", ()) in b.histograms

    def test_timer_with_tags(self):
        b = RecordingBackend()
        with b.timer("op", tags={"r": "us"}):
            pass
        assert ("op", (("r", "us"),)) in b.histograms

    def test_name_validation_all_methods(self):
        b = RecordingBackend()
        with pytest.raises(ValueError):
            b.counter("")
        with pytest.raises(ValueError):
            b.gauge("", 1)
        with pytest.raises(ValueError):
            b.histogram("", 1)

    def test_tag_order_normalization(self):
        b = RecordingBackend()
        b.counter("x", 1, tags={"b": "2", "a": "1"})
        b.counter("x", 1, tags={"a": "1", "b": "2"})
        assert sum(b.counters.values()) == 2.0
        assert len(b.counters) == 1


# ---------------------------------------------------------------------------
# _check_name and _canonical_tags
# ---------------------------------------------------------------------------


class TestCheckNameUnit:
    def test_valid(self):
        _check_name("orders.placed")

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            _check_name("")

    def test_whitespace_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            _check_name("  ")


class TestCanonicalTagsUnit:
    def test_none(self):
        assert _canonical_tags(None) == ()

    def test_empty_dict(self):
        assert _canonical_tags({}) == ()

    def test_sorted(self):
        assert _canonical_tags({"b": "2", "a": "1"}) == (("a", "1"), ("b", "2"))

    def test_stringifies(self):
        assert _canonical_tags({"x": 42}) == (("x", "42"),)


# ---------------------------------------------------------------------------
# set_metrics / get_metrics singleton
# ---------------------------------------------------------------------------


class TestMetricsSingleton:
    @pytest.fixture(autouse=True)
    def _reset(self):
        yield
        set_metrics(NullBackend())

    def test_default_is_null(self):
        assert isinstance(get_metrics(), NullBackend)

    def test_set_and_get(self):
        rec = RecordingBackend()
        set_metrics(rec)
        assert get_metrics() is rec

    def test_reset_to_null(self):
        set_metrics(RecordingBackend())
        set_metrics(NullBackend())
        assert isinstance(get_metrics(), NullBackend)

    def test_counter_through_singleton(self):
        rec = RecordingBackend()
        set_metrics(rec)
        get_metrics().counter("x", 1)
        get_metrics().counter("x", 2)
        assert rec.counters == {("x", ()): 3.0}

    def test_thread_safety(self):
        errors = []

        def worker():
            try:
                set_metrics(RecordingBackend())
                get_metrics()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors


# ---------------------------------------------------------------------------
# _serialize() from reference.py
# ---------------------------------------------------------------------------


class TestSerialize:
    def test_output_schema(self):
        idx = SearchIndex()
        inst = RefInstrument(primary_ticker="AAPL", primary_venue="XNAS", asset_class="equity", name="Apple Inc.")
        idx.add(inst)
        suggestions = idx.suggest("AAPL")
        result = _serialize(suggestions[0])

        assert result["symbol"] == "AAPL"
        assert result["name"] == "Apple Inc."
        assert "AAPL" in result["display"]
        assert "Apple" in result["display"]
        assert result["completion"] == "AAPL"
        assert result["score"] == 100
        assert result["record"]["primary_ticker"] == "AAPL"
        assert result["record"]["primary_venue"] == "XNAS"
        assert result["record"]["asset_class"] == "equity"
        assert result["record"]["currency"] == "USD"
        assert len(result["record"]["id"]) == 36

    def test_display_format(self):
        idx = SearchIndex()
        inst = RefInstrument(primary_ticker="MSFT", primary_venue="XNAS", asset_class="equity", name="Microsoft Corp.")
        idx.add(inst)
        suggestions = idx.suggest("MSFT")
        result = _serialize(suggestions[0])
        assert result["display"] == "MSFT — Microsoft Corp."


# ---------------------------------------------------------------------------
# get_search_index() singleton
# ---------------------------------------------------------------------------


class TestGetSearchIndexSingleton:
    def test_returns_search_index(self):
        from engine.api.routes import reference as ref_mod
        original = ref_mod._INDEX
        try:
            ref_mod._INDEX = None
            idx = get_search_index()
            assert isinstance(idx, SearchIndex)
            assert ref_mod._INDEX is idx
        finally:
            ref_mod._INDEX = original

    def test_same_instance_returned(self):
        from engine.api.routes import reference as ref_mod
        original = ref_mod._INDEX
        try:
            ref_mod._INDEX = None
            a = get_search_index()
            b = get_search_index()
            assert a is b
        finally:
            ref_mod._INDEX = original


# ---------------------------------------------------------------------------
# Suggest endpoint — Yahoo fallback integration
# ---------------------------------------------------------------------------


class TestSuggestYahooFallbackIntegration:
    @pytest.fixture
    async def client(self):
        app = FastAPI()
        app.include_router(reference_router, prefix="/api/v1/reference")
        app.dependency_overrides[get_search_index] = SearchIndex
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    async def test_local_empty_yahoo_returns(self, client: AsyncClient):
        yahoo = [
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
        with patch("engine.api.routes.reference._yahoo_search", new_callable=AsyncMock, return_value=yahoo):
            r = await client.get("/api/v1/reference/suggest", params={"q": "ZZZZ"})
            assert r.status_code == HTTPStatus.OK
            assert r.json()["suggestions"][0]["symbol"] == "ZZZZ"

    async def test_yahoo_filtered_by_asset_class(self, client: AsyncClient):
        yahoo = [
            {
                "symbol": "AAPL",
                "name": "Apple",
                "display": "AAPL — Apple",
                "completion": "Apple",
                "score": 80,
                "record": {"id": "", "primary_ticker": "AAPL", "primary_venue": "XNAS", "asset_class": "equity", "name": "Apple", "currency": "USD"},
            },
            {
                "symbol": "BTC-USD",
                "name": "Bitcoin",
                "display": "BTC-USD — Bitcoin",
                "completion": "Bitcoin",
                "score": 60,
                "record": {"id": "", "primary_ticker": "BTC-USD", "primary_venue": "XCRY", "asset_class": "crypto", "name": "Bitcoin", "currency": "USD"},
            },
        ]
        with patch("engine.api.routes.reference._yahoo_search", new_callable=AsyncMock, return_value=yahoo):
            r = await client.get("/api/v1/reference/suggest", params={"q": "test", "asset_class": "crypto"})
            assert r.status_code == HTTPStatus.OK
            for s in r.json()["suggestions"]:
                assert s["record"]["asset_class"] == "crypto"

    async def test_both_empty(self, client: AsyncClient):
        with patch("engine.api.routes.reference._yahoo_search", new_callable=AsyncMock, return_value=[]):
            r = await client.get("/api/v1/reference/suggest", params={"q": "xyznonexistent"})
            assert r.status_code == HTTPStatus.OK
            assert r.json()["suggestions"] == []


# ---------------------------------------------------------------------------
# Seeded index — search + suggest integration
# ---------------------------------------------------------------------------


class TestSeededIndexIntegration:
    def _seeded(self) -> SearchIndex:
        idx = SearchIndex()
        seed_index(idx)
        return idx

    def test_search_aapl(self):
        results = self._seeded().search("AAPL")
        assert any(r.primary_ticker == "AAPL" for r in results)

    def test_search_apple_by_name(self):
        results = self._seeded().search("Apple")
        assert any(r.primary_ticker == "AAPL" for r in results)

    def test_search_bitcoin_crypto(self):
        results = self._seeded().search("BTC", asset_class="crypto")
        for r in results:
            assert r.asset_class == "crypto"

    def test_suggest_go_prefix(self):
        results = self._seeded().suggest("GO")
        tickers = [s.record.primary_ticker for s in results]
        assert any(t in tickers for t in ("GOOGL", "GOOG", "GOLD"))

    def test_suggest_typo_fuzzy(self):
        idx = SearchIndex()
        idx.add(RefInstrument(primary_ticker="AAPL", primary_venue="XNAS", asset_class="equity", name="Apple Inc."))
        results = idx.suggest("aple")
        assert any(s.record.primary_ticker == "AAPL" for s in results)

    def test_suggest_etf_filter(self):
        results = self._seeded().suggest("SP", asset_class="etf")
        for s in results:
            assert s.record.asset_class == "etf"

    def test_suggest_forex_filter(self):
        results = self._seeded().suggest("EUR", asset_class="forex")
        for s in results:
            assert s.record.asset_class == "forex"
