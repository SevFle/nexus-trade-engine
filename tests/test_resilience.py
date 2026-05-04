"""Tests for engine.data.providers._resilience — TokenBucket and call_with_retry."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from engine.data.providers._resilience import (
    DEFAULT_BASE_DELAY_S,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_DELAY_S,
    TokenBucket,
    call_with_retry,
)
from engine.data.providers.base import (
    FatalProviderError,
    RateLimit,
    TransientProviderError,
)


class TestTokenBucket:
    async def test_zero_rate_disables_limiting(self):
        bucket = TokenBucket(RateLimit(requests_per_minute=0))
        assert bucket._capacity == 0
        start = time.monotonic()
        for _ in range(100):
            await bucket.acquire()
        elapsed = time.monotonic() - start
        assert elapsed < 0.1

    async def test_positive_rate_allows_acquire(self):
        bucket = TokenBucket(RateLimit(requests_per_minute=60))
        assert bucket._capacity >= 1
        await bucket.acquire()

    async def test_burst_zero_gets_min_capacity_one(self):
        bucket = TokenBucket(RateLimit(requests_per_minute=10, burst=0))
        assert bucket._capacity == 1

    async def test_burst_sets_capacity(self):
        bucket = TokenBucket(RateLimit(requests_per_minute=60, burst=5))
        assert bucket._capacity == 5

    async def test_negative_rate_treated_as_zero(self):
        bucket = TokenBucket(RateLimit(requests_per_minute=-10))
        assert bucket._capacity == 0
        await bucket.acquire()

    async def test_refill_rate_calculation(self):
        bucket = TokenBucket(RateLimit(requests_per_minute=60))
        assert bucket._refill_per_second == pytest.approx(1.0)

    async def test_burst_consumption(self):
        bucket = TokenBucket(RateLimit(requests_per_minute=6000, burst=10))
        assert bucket._capacity == 10
        for _ in range(10):
            await bucket.acquire()
        assert bucket._tokens == pytest.approx(0.0, abs=0.01)

    async def test_tokens_refill_over_time(self):
        bucket = TokenBucket(RateLimit(requests_per_minute=60, burst=1))
        await bucket.acquire()
        assert bucket._tokens < 0.5
        await asyncio.sleep(0.05)
        bucket_after = TokenBucket(RateLimit(requests_per_minute=60, burst=1))
        assert bucket_after._tokens == pytest.approx(1.0)


class TestCallWithRetry:
    async def test_success_on_first_attempt(self):
        func = AsyncMock(return_value="ok")
        result = await call_with_retry(func, provider="test")
        assert result == "ok"
        assert func.call_count == 1

    async def test_retry_on_transient_then_success(self):
        func = AsyncMock(
            side_effect=[TransientProviderError("transient"), "ok"]
        )
        with patch("engine.data.providers._resilience.asyncio.sleep", new_callable=AsyncMock):
            result = await call_with_retry(func, provider="test", base_delay_s=0)
        assert result == "ok"
        assert func.call_count == 2

    async def test_all_attempts_exhausted_raises_last(self):
        func = AsyncMock(side_effect=TransientProviderError("fail"))
        with patch("engine.data.providers._resilience.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(TransientProviderError, match="fail"):
                await call_with_retry(func, provider="test", max_attempts=3, base_delay_s=0)
        assert func.call_count == 3

    async def test_fatal_error_propagates_immediately(self):
        func = AsyncMock(side_effect=FatalProviderError("fatal"))
        with pytest.raises(FatalProviderError, match="fatal"):
            await call_with_retry(func, provider="test", max_attempts=5)
        assert func.call_count == 1

    async def test_timeout_error_retried(self):
        func = AsyncMock(side_effect=[TimeoutError("timeout"), "ok"])
        with patch("engine.data.providers._resilience.asyncio.sleep", new_callable=AsyncMock):
            result = await call_with_retry(func, provider="test", base_delay_s=0)
        assert result == "ok"
        assert func.call_count == 2

    async def test_max_attempts_one_no_retry(self):
        func = AsyncMock(side_effect=TransientProviderError("fail"))
        with pytest.raises(TransientProviderError, match="fail"):
            await call_with_retry(func, provider="test", max_attempts=1)
        assert func.call_count == 1

    async def test_default_constants(self):
        assert DEFAULT_MAX_ATTEMPTS == 3
        assert DEFAULT_BASE_DELAY_S == 0.25
        assert DEFAULT_MAX_DELAY_S == 8.0

    async def test_exponential_backoff_delay(self):
        delays: list[float] = []

        async def fake_sleep(d: float) -> None:
            delays.append(d)

        func = AsyncMock(
            side_effect=[TransientProviderError("t"), "ok"]
        )
        with patch("engine.data.providers._resilience.asyncio.sleep", side_effect=fake_sleep):
            await call_with_retry(
                func, provider="test", base_delay_s=1.0, max_delay_s=30.0
            )
        assert len(delays) >= 1
        assert delays[0] >= 1.0

    async def test_delay_capped_at_max(self):
        delays: list[float] = []

        async def fake_sleep(d: float) -> None:
            delays.append(d)

        func = AsyncMock(
            side_effect=[
                TransientProviderError("t"),
                TransientProviderError("t"),
                "ok",
            ]
        )
        with patch("engine.data.providers._resilience.asyncio.sleep", side_effect=fake_sleep):
            await call_with_retry(
                func, provider="test", base_delay_s=10.0, max_delay_s=5.0, max_attempts=3
            )
        for d in delays:
            assert d <= 5.0 + 5.0 * 0.25 + 0.01

    async def test_custom_base_delay(self):
        delays: list[float] = []

        async def fake_sleep(d: float) -> None:
            delays.append(d)

        func = AsyncMock(
            side_effect=[TransientProviderError("t"), "ok"]
        )
        with patch("engine.data.providers._resilience.asyncio.sleep", side_effect=fake_sleep):
            await call_with_retry(
                func, provider="test", base_delay_s=0.5, max_delay_s=8.0
            )
        assert delays[0] >= 0.5

    async def test_generic_exception_not_retried(self):
        func = AsyncMock(side_effect=ValueError("unexpected"))
        with pytest.raises(ValueError, match="unexpected"):
            await call_with_retry(func, provider="test", max_attempts=3)
        assert func.call_count == 1
