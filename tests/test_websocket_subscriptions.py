"""Unit tests for engine.api.websocket.subscriptions (SEV-275)."""

from __future__ import annotations

import uuid

import pytest

from engine.api.websocket.channels import for_market, for_orders, for_portfolio
from engine.api.websocket.constants import MAX_SYMBOL_SUBS_PER_CONNECTION
from engine.api.websocket.exceptions import SubscriptionLimitError
from engine.api.websocket.subscriptions import SubscriptionRegistry


@pytest.fixture
def registry() -> SubscriptionRegistry:
    return SubscriptionRegistry()


class TestSubscribeIdempotency:
    async def test_subscribe_returns_true_on_new(self, registry):
        c = for_portfolio(uuid.uuid4())
        assert await registry.subscribe(c) is True

    async def test_subscribe_returns_false_on_duplicate(self, registry):
        c = for_portfolio(uuid.uuid4())
        assert await registry.subscribe(c) is True
        assert await registry.subscribe(c) is False

    async def test_subscribe_unknown_key_safe(self, registry):
        # unsubscribe of unknown is a no-op (returns False)
        c = for_portfolio(uuid.uuid4())
        assert await registry.unsubscribe(c) is False

    async def test_unsubscribe_after_subscribe_returns_true(self, registry):
        c = for_portfolio(uuid.uuid4())
        await registry.subscribe(c)
        assert await registry.unsubscribe(c) is True
        assert await registry.unsubscribe(c) is False  # gone


class TestCaps:
    async def test_market_cap_enforced(self, registry):
        # Add MAX symbols — should succeed.
        for i in range(MAX_SYMBOL_SUBS_PER_CONNECTION):
            await registry.subscribe(for_market(f"S{i:04d}"))
        # Next one should blow up.
        with pytest.raises(SubscriptionLimitError):
            await registry.subscribe(for_market("OVERFLOW"))

    async def test_market_cap_isolated_from_orders(self, registry):
        # Fill the market bucket; user channels must still be addable.
        for i in range(MAX_SYMBOL_SUBS_PER_CONNECTION):
            await registry.subscribe(for_market(f"S{i:04d}"))
        u = uuid.uuid4()
        # Different family, different cap.
        assert await registry.subscribe(for_portfolio(u)) is True
        assert await registry.subscribe(for_orders(u)) is True

    async def test_unsubscribe_family_drops_all(self, registry):
        for i in range(3):
            await registry.subscribe(for_market(f"S{i:04d}"))
        removed = await registry.unsubscribe_family("market")
        assert removed == {"S0000", "S0001", "S0002"}
        assert registry.count("market") == 0


class TestIntrospection:
    async def test_snapshot_is_isolated_copy(self, registry):
        c = for_portfolio(uuid.uuid4())
        await registry.subscribe(c)
        snap = registry.snapshot()
        assert str(c.key) in snap.portfolio
        # Mutating snapshot doesn't affect registry.
        snap.portfolio.clear()
        assert registry.count("portfolio") == 1

    async def test_channels_list(self, registry):
        u = uuid.uuid4()
        await registry.subscribe(for_portfolio(u))
        await registry.subscribe(for_market("AAPL"))
        names = sorted(c.name for c in registry.channels())
        assert names == sorted([f"portfolio:{u}", "market:AAPL"])

    async def test_total(self, registry):
        u = uuid.uuid4()
        await registry.subscribe(for_portfolio(u))
        await registry.subscribe(for_market("AAPL"))
        await registry.subscribe(for_market("MSFT"))
        assert registry.total() == 3

    async def test_clear_drops_all(self, registry):
        u = uuid.uuid4()
        await registry.subscribe(for_portfolio(u))
        await registry.subscribe(for_market("AAPL"))
        await registry.clear()
        assert registry.total() == 0


class TestIsSubscribed:
    async def test_membership(self, registry):
        u = uuid.uuid4()
        c = for_portfolio(u)
        assert not registry.is_subscribed(c)
        await registry.subscribe(c)
        assert registry.is_subscribed(c)
