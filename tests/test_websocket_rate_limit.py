"""Unit tests for engine.api.websocket.rate_limit (SEV-275)."""

from __future__ import annotations

import pytest

from engine.api.websocket.exceptions import RateLimitedError
from engine.api.websocket.rate_limit import OutboundRateLimiter


class TestRateLimiter:
    async def test_allows_under_capacity(self):
        rl = OutboundRateLimiter(capacity=3, window_seconds=1.0)
        assert await rl.acquire("k") is True
        assert await rl.acquire("k") is True
        assert await rl.acquire("k") is True

    async def test_blocks_over_capacity(self):
        rl = OutboundRateLimiter(capacity=2, window_seconds=1.0)
        await rl.acquire("k")
        await rl.acquire("k")
        assert await rl.acquire("k") is False

    async def test_require_raises_on_block(self):
        rl = OutboundRateLimiter(capacity=1, window_seconds=1.0)
        await rl.require("k")
        with pytest.raises(RateLimitedError):
            await rl.require("k")

    async def test_keys_are_isolated(self):
        rl = OutboundRateLimiter(capacity=1, window_seconds=1.0)
        await rl.acquire("a")
        # Different key — independent bucket.
        assert await rl.acquire("b") is True

    async def test_reset_drops_bucket(self):
        rl = OutboundRateLimiter(capacity=1, window_seconds=1.0)
        await rl.acquire("k")
        await rl.reset("k")
        # Bucket recreated fresh — allow again.
        assert await rl.acquire("k") is True

    async def test_window_evicts_old_events(self):
        rl = OutboundRateLimiter(capacity=2, window_seconds=0.05)
        await rl.acquire("k")
        await rl.acquire("k")
        # Wait past the window.
        import asyncio

        await asyncio.sleep(0.06)
        assert await rl.acquire("k") is True

    def test_invalid_capacity_rejected(self):
        with pytest.raises(ValueError):
            OutboundRateLimiter(capacity=0)

    def test_invalid_window_rejected(self):
        with pytest.raises(ValueError):
            OutboundRateLimiter(window_seconds=0)
