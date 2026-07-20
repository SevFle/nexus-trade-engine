"""Tests for :class:`engine.providers.cached.CachedDataProvider`.

Covers the four required behaviours:

1. **Cache miss delegates** — a first call hits the wrapped provider.
2. **Cache hit returns cached data without calling the provider** — a second
   call within the TTL serves from memory and does not touch the provider.
3. **TTL expiry triggers a re-fetch** — once an entry is older than the TTL,
   the next call delegates again.
4. **Different symbols/intervals are cached independently** — adjacent keys
   never collide.

Plus a handful of edge cases that make the contract robust: cached
``None``/empty results are served (so "no data" itself is cached), ``ttl=0``
disables serving from cache, negative TTL is rejected, and the
``get_latest_price`` path mirrors the OHLCV path.

The wrapped provider is mocked with :mod:`unittest.mock` (``MagicMock`` +
``AsyncMock``) so no real I/O occurs.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

from engine.providers.cached import DEFAULT_TTL_SECONDS, CachedDataProvider

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _ohlcv_df(close: float = 100.0, rows: int = 2, start: str = "2026-01-01") -> pd.DataFrame:
    """Build a minimal canonical OHLCV frame, parameterised by ``close``."""
    idx = pd.date_range(start, periods=rows, freq="D", tz="UTC")
    return pd.DataFrame(
        {
            "open": [close] * rows,
            "high": [close + 1] * rows,
            "low": [close - 1] * rows,
            "close": [close] * rows,
            "volume": [1000] * rows,
        },
        index=idx,
    )


def _make_mock_provider() -> MagicMock:
    """A mock :class:`IDataProvider` with async ``get_ohlcv``/``get_latest_price``.

    Using ``MagicMock`` + explicit ``AsyncMock`` attributes (rather than
    ``spec=IDataProvider``) lets each test freely assert call counts and
    reconfigure return values without fighting the ABC spec machinery.
    """
    provider = MagicMock(name="wrapped_provider")
    provider.get_ohlcv = AsyncMock(name="get_ohlcv")
    provider.get_latest_price = AsyncMock(name="get_latest_price")
    return provider


# --------------------------------------------------------------------------- #
# construction
# --------------------------------------------------------------------------- #


def test_default_ttl_is_60_seconds():
    """The documented default TTL is 60s."""
    cached = CachedDataProvider(_make_mock_provider())
    assert cached.ttl == DEFAULT_TTL_SECONDS == 60.0


def test_negative_ttl_is_rejected():
    """A negative TTL is nonsensical and must be rejected up front."""
    with pytest.raises(ValueError, match="ttl must be non-negative"):
        CachedDataProvider(_make_mock_provider(), ttl=-1)


def test_provider_property_exposes_wrapped_provider():
    """The read-only ``provider`` property returns the exact wrapped instance.

    This pins the introspection surface used by tests / diagnostics so that
    the underlying provider is reachable without poking at private attrs.
    """
    wrapped = _make_mock_provider()
    cached = CachedDataProvider(wrapped, ttl=30.0)
    assert cached.provider is wrapped


# --------------------------------------------------------------------------- #
# 1. cache miss delegates to the underlying provider
# --------------------------------------------------------------------------- #


async def test_get_ohlcv_cache_miss_delegates_to_provider():
    """A first ``get_ohlcv`` call (cache miss) must delegate to the provider."""
    provider = _make_mock_provider()
    expected = _ohlcv_df(close=100.0)
    provider.get_ohlcv.return_value = expected

    cached = CachedDataProvider(provider, ttl=60.0)
    result = await cached.get_ohlcv("AAPL", date_range="1y", interval="1d")

    pd.testing.assert_frame_equal(result, expected)
    provider.get_ohlcv.assert_awaited_once_with("AAPL", period="1y", interval="1d")


async def test_get_latest_price_cache_miss_delegates_to_provider():
    """A first ``get_latest_price`` call (cache miss) must delegate."""
    provider = _make_mock_provider()
    provider.get_latest_price.return_value = 150.25

    cached = CachedDataProvider(provider, ttl=60.0)
    result = await cached.get_latest_price("AAPL")

    assert result == 150.25
    provider.get_latest_price.assert_awaited_once_with("AAPL")


# --------------------------------------------------------------------------- #
# 2. cache hit returns cached data without calling the provider
# --------------------------------------------------------------------------- #


async def test_get_ohlcv_cache_hit_does_not_call_provider():
    """A second identical call within the TTL must be served from cache."""
    provider = _make_mock_provider()
    provider.get_ohlcv.return_value = _ohlcv_df(close=100.0)

    cached = CachedDataProvider(provider, ttl=60.0)

    first = await cached.get_ohlcv("AAPL", date_range="1y", interval="1d")
    second = await cached.get_ohlcv("AAPL", date_range="1y", interval="1d")

    # Same object served from cache — no copy, no second fetch.
    assert second is first
    provider.get_ohlcv.assert_awaited_once()


async def test_get_latest_price_cache_hit_does_not_call_provider():
    """A second identical price call within the TTL is served from cache."""
    provider = _make_mock_provider()
    provider.get_latest_price.return_value = 200.0

    cached = CachedDataProvider(provider, ttl=60.0)

    first = await cached.get_latest_price("MSFT")
    second = await cached.get_latest_price("MSFT")

    assert second == first == 200.0
    provider.get_latest_price.assert_awaited_once()


# --------------------------------------------------------------------------- #
# 3. TTL expiry triggers a re-fetch
# --------------------------------------------------------------------------- #


async def test_ohlcv_refetched_after_ttl_expiry():
    """Once an entry is older than the TTL the next call must re-fetch."""
    provider = _make_mock_provider()
    fresh = _ohlcv_df(close=100.0)
    refetched = _ohlcv_df(close=999.0)
    provider.get_ohlcv.side_effect = [fresh, refetched]

    cached = CachedDataProvider(provider, ttl=60.0)

    first = await cached.get_ohlcv("AAPL", date_range="1y", interval="1d")
    assert provider.get_ohlcv.await_count == 1

    # Simulate 61s passing — entry is now 1s past its TTL.
    cached._age_all(61.0)

    second = await cached.get_ohlcv("AAPL", date_range="1y", interval="1d")
    assert provider.get_ohlcv.await_count == 2

    # The re-fetched (stale-evicted → refilled) value is served.
    pd.testing.assert_frame_equal(second, refetched)
    pd.testing.assert_frame_equal(first, fresh)


async def test_latest_price_refetched_after_ttl_expiry():
    """TTL expiry forces a re-fetch on the price path too."""
    provider = _make_mock_provider()
    provider.get_latest_price.side_effect = [100.0, 101.0]

    cached = CachedDataProvider(provider, ttl=60.0)

    assert await cached.get_latest_price("AAPL") == 100.0
    assert provider.get_latest_price.await_count == 1

    cached._age_all(60.0)  # exactly at TTL boundary → stale (uses strict <)

    assert await cached.get_latest_price("AAPL") == 101.0
    assert provider.get_latest_price.await_count == 2


async def test_entry_still_fresh_just_under_ttl_is_not_refetched():
    """An entry aged strictly less than the TTL must remain a hit."""
    provider = _make_mock_provider()
    provider.get_ohlcv.return_value = _ohlcv_df(close=1.0)

    cached = CachedDataProvider(provider, ttl=60.0)
    await cached.get_ohlcv("AAPL")
    assert provider.get_ohlcv.await_count == 1

    cached._age_all(59.9)  # strictly less than TTL → still fresh

    await cached.get_ohlcv("AAPL")
    assert provider.get_ohlcv.await_count == 1


# --------------------------------------------------------------------------- #
# 4. different symbols / intervals cached independently
# --------------------------------------------------------------------------- #


async def test_different_symbols_cached_independently():
    """A hit for one symbol must never satisfy a different symbol's request."""
    provider = _make_mock_provider()
    aapl = _ohlcv_df(close=100.0)
    msft = _ohlcv_df(close=200.0)
    provider.get_ohlcv.side_effect = [aapl, msft]

    cached = CachedDataProvider(provider, ttl=60.0)

    first_aapl = await cached.get_ohlcv("AAPL", date_range="1y", interval="1d")
    first_msft = await cached.get_ohlcv("MSFT", date_range="1y", interval="1d")

    pd.testing.assert_frame_equal(first_aapl, aapl)
    pd.testing.assert_frame_equal(first_msft, msft)
    assert provider.get_ohlcv.await_count == 2

    # Both are now cached — re-asking serves each from its own slot.
    second_aapl = await cached.get_ohlcv("AAPL", date_range="1y", interval="1d")
    second_msft = await cached.get_ohlcv("MSFT", date_range="1y", interval="1d")
    assert provider.get_ohlcv.await_count == 2  # no new fetches
    assert second_aapl is first_aapl
    assert second_msft is first_msft


async def test_different_intervals_cached_independently():
    """Same symbol + date_range but a different interval is a separate key."""
    provider = _make_mock_provider()
    daily = _ohlcv_df(close=10.0)
    hourly = _ohlcv_df(close=20.0)
    provider.get_ohlcv.side_effect = [daily, hourly]

    cached = CachedDataProvider(provider, ttl=60.0)

    out_daily = await cached.get_ohlcv("AAPL", date_range="1mo", interval="1d")
    out_hourly = await cached.get_ohlcv("AAPL", date_range="1mo", interval="1h")

    pd.testing.assert_frame_equal(out_daily, daily)
    pd.testing.assert_frame_equal(out_hourly, hourly)
    assert provider.get_ohlcv.await_count == 2

    # Both keys live side by side in the cache.
    assert ("AAPL", "1d", "1mo") in cached._cache
    assert ("AAPL", "1h", "1mo") in cached._cache


async def test_different_date_ranges_cached_independently():
    """Same symbol + interval but a different date_range is a separate key."""
    provider = _make_mock_provider()
    one_year = _ohlcv_df(close=5.0)
    one_month = _ohlcv_df(close=6.0)
    provider.get_ohlcv.side_effect = [one_year, one_month]

    cached = CachedDataProvider(provider, ttl=60.0)

    out_year = await cached.get_ohlcv("AAPL", date_range="1y", interval="1d")
    out_month = await cached.get_ohlcv("AAPL", date_range="1mo", interval="1d")

    pd.testing.assert_frame_equal(out_year, one_year)
    pd.testing.assert_frame_equal(out_month, one_month)
    assert provider.get_ohlcv.await_count == 2


async def test_latest_price_keys_isolated_from_ohlcv_keys():
    """A price lookup and an OHLCV lookup never share a cache slot."""
    provider = _make_mock_provider()
    provider.get_ohlcv.return_value = _ohlcv_df(close=7.0)
    provider.get_latest_price.return_value = 7.5

    cached = CachedDataProvider(provider, ttl=60.0)
    await cached.get_ohlcv("AAPL", date_range="1y", interval="1d")
    await cached.get_latest_price("AAPL")

    assert provider.get_ohlcv.await_count == 1
    assert provider.get_latest_price.await_count == 1
    # Distinct keys: OHLCV uses the (symbol, interval, date_range) triple,
    # the price path uses (symbol, None, None).
    assert ("AAPL", "1d", "1y") in cached._cache
    assert ("AAPL", None, None) in cached._cache


# --------------------------------------------------------------------------- #
# edge cases: cached None / empty, ttl=0, clear()
# --------------------------------------------------------------------------- #


async def test_cached_none_price_is_served_without_refetch():
    """A cached ``None`` ("no price") is served like any other value.

    This is the whole point of the ``_MISS`` sentinel: without it a cached
    ``None`` would be indistinguishable from an absent entry and every
    polling tick would re-hit the upstream.
    """
    provider = _make_mock_provider()
    provider.get_latest_price.return_value = None

    cached = CachedDataProvider(provider, ttl=60.0)

    assert await cached.get_latest_price("UNKN") is None
    assert provider.get_latest_price.await_count == 1

    # Second call must serve the cached None, not re-fetch.
    assert await cached.get_latest_price("UNKN") is None
    assert provider.get_latest_price.await_count == 1


async def test_cached_empty_dataframe_is_served_without_refetch():
    """A cached empty OHLCV frame ("symbol unknown") is served from cache."""
    provider = _make_mock_provider()
    empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    provider.get_ohlcv.return_value = empty

    cached = CachedDataProvider(provider, ttl=60.0)

    first = await cached.get_ohlcv("DELISTED", date_range="1y", interval="1d")
    second = await cached.get_ohlcv("DELISTED", date_range="1y", interval="1d")

    assert first.empty and second.empty
    assert second is first
    provider.get_ohlcv.assert_awaited_once()


async def test_ttl_zero_disables_serving_from_cache():
    """``ttl=0`` means "never serve from cache" — every call delegates."""
    provider = _make_mock_provider()
    provider.get_ohlcv.return_value = _ohlcv_df(close=1.0)

    cached = CachedDataProvider(provider, ttl=0)

    await cached.get_ohlcv("AAPL")
    await cached.get_ohlcv("AAPL")
    await cached.get_ohlcv("AAPL")

    assert provider.get_ohlcv.await_count == 3


async def test_clear_evicts_all_entries():
    """``clear()`` drops every cached entry so the next call re-fetches."""
    provider = _make_mock_provider()
    provider.get_ohlcv.return_value = _ohlcv_df(close=1.0)

    cached = CachedDataProvider(provider, ttl=60.0)
    await cached.get_ohlcv("AAPL")
    assert provider.get_ohlcv.await_count == 1

    cached.clear()
    assert cached._cache == {}

    await cached.get_ohlcv("AAPL")
    assert provider.get_ohlcv.await_count == 2
