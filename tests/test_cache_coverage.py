"""Tests for engine.data.providers._cache — ProviderCache coverage."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from engine.data.providers._cache import CACHE_PAYLOAD_CAP, ProviderCache


class TestMakeKey:
    def test_basic_key(self):
        key = ProviderCache.make_key("yahoo", "bars", symbol="AAPL")
        assert key.startswith("nexus:dp:v1:yahoo:bars:")

    def test_deterministic(self):
        k1 = ProviderCache.make_key("yahoo", "bars", symbol="AAPL")
        k2 = ProviderCache.make_key("yahoo", "bars", symbol="AAPL")
        assert k1 == k2

    def test_different_params_different_keys(self):
        k1 = ProviderCache.make_key("yahoo", "bars", symbol="AAPL")
        k2 = ProviderCache.make_key("yahoo", "bars", symbol="MSFT")
        assert k1 != k2

    def test_none_params_skipped(self):
        key = ProviderCache.make_key("yahoo", "bars", symbol=None, period="1y")
        assert key.startswith("nexus:dp:v1:yahoo:bars:")

    def test_invalid_param_type_rejected(self):
        with pytest.raises(TypeError, match="must be a primitive"):
            ProviderCache.make_key("yahoo", "bars", symbol=object())

    def test_list_param_accepted(self):
        key = ProviderCache.make_key("yahoo", "bars", symbols=["AAPL", "MSFT"])
        assert key.startswith("nexus:dp:v1:yahoo:bars:")

    def test_tuple_param_accepted(self):
        key = ProviderCache.make_key("yahoo", "bars", symbols=("AAPL",))
        assert key.startswith("nexus:dp:v1:yahoo:bars:")


class TestMemoryFallback:
    @pytest.mark.asyncio
    async def test_memory_set_and_get_json(self):
        cache = ProviderCache(url=None)
        key = ProviderCache.make_key("test", "method", x=1)
        await cache.set_json(key, {"val": 42}, ttl_seconds=60)
        result = await cache.get_json(key)
        assert result == {"val": 42}

    @pytest.mark.asyncio
    async def test_memory_get_missing_key(self):
        cache = ProviderCache(url=None)
        result = await cache.get_json("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_memory_set_and_get_dataframe(self):
        cache = ProviderCache(url=None)
        key = ProviderCache.make_key("test", "df", sym="AAPL")
        df = pd.DataFrame({"close": [100.0, 101.0]}, index=pd.date_range("2024-01-01", periods=2, tz=UTC))
        await cache.set_dataframe(key, df, ttl_seconds=60)
        result = await cache.get_dataframe(key)
        assert result is not None
        assert len(result) == 2
        assert list(result["close"]) == [100.0, 101.0]

    @pytest.mark.asyncio
    async def test_memory_get_dataframe_missing(self):
        cache = ProviderCache(url=None)
        result = await cache.get_dataframe("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_memory_empty_df_not_cached(self):
        cache = ProviderCache(url=None)
        key = "test_empty"
        df = pd.DataFrame()
        await cache.set_dataframe(key, df, ttl_seconds=60)
        result = await cache.get_dataframe(key)
        assert result is None

    @pytest.mark.asyncio
    async def test_memory_none_df_not_cached(self):
        cache = ProviderCache(url=None)
        key = "test_none"
        await cache.set_dataframe(key, None, ttl_seconds=60)
        result = await cache.get_dataframe(key)
        assert result is None

    @pytest.mark.asyncio
    async def test_memory_expired_entry(self):
        cache = ProviderCache(url=None)
        key = "test_expire"
        cache._memory[key] = (0.0, b'{"val": 1}')
        result = await cache.get_json(key)
        assert result is None

    @pytest.mark.asyncio
    async def test_memory_ttl_zero_never_expires(self):
        cache = ProviderCache(url=None)
        key = "test_no_expire"
        cache._memory[key] = (0.0, b'{"val": 1}')
        result = await cache.get_json(key)
        assert result == {"val": 1}

    @pytest.mark.asyncio
    async def test_aclose_clears_memory(self):
        cache = ProviderCache(url=None)
        cache._memory["key"] = (9999999.0, b"data")
        await cache.aclose()
        assert len(cache._memory) == 0
        assert cache._redis is None


class TestShared:
    def test_shared_creates_instance(self):
        ProviderCache.reset_for_tests()
        cache = ProviderCache.shared()
        assert isinstance(cache, ProviderCache)
        ProviderCache.reset_for_tests()

    def test_shared_returns_same(self):
        ProviderCache.reset_for_tests()
        c1 = ProviderCache.shared()
        c2 = ProviderCache.shared()
        assert c1 is c2
        ProviderCache.reset_for_tests()


class TestPayloadCap:
    @pytest.mark.asyncio
    async def test_oversized_dataframe_not_cached(self):
        cache = ProviderCache(url=None)
        key = "test_oversized"
        large_df = pd.DataFrame({"x": ["a" * 1000] * 10000})
        await cache.set_dataframe(key, large_df, ttl_seconds=60)
        result = await cache.get_dataframe(key)
        assert result is None

    @pytest.mark.asyncio
    async def test_oversized_get_payload_returns_none(self):
        cache = ProviderCache(url=None)
        key = "test_big_read"
        cache._memory[key] = (9999999.0, b"x" * (CACHE_PAYLOAD_CAP + 1))
        result = await cache.get_dataframe(key)
        assert result is None


class TestJsonDecodeFailure:
    @pytest.mark.asyncio
    async def test_invalid_json_returns_none(self):
        cache = ProviderCache(url=None)
        key = "test_bad_json"
        cache._memory[key] = (9999999.0, b"\xff\xfe")
        result = await cache.get_json(key)
        assert result is None
