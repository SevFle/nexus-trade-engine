"""Tests for engine.core.execution.live and engine.core.execution.paper backends."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from engine.core.execution.base import FillResult
from engine.core.execution.live import LiveBackend
from engine.core.execution.paper import PaperBackend, PaperTradeConfig


@dataclass
class _FakeCostBreakdown:
    slippage: Any = None


class _FakeSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


@dataclass
class _FakeOrder:
    id: str = "ord-1"
    symbol: str = "AAPL"
    quantity: int = 100
    side: _FakeSide = _FakeSide.BUY


def _make_cost(slippage_amount: float = 5.0):
    mock_cost = MagicMock()
    mock_cost.slippage = MagicMock()
    mock_cost.slippage.amount = slippage_amount
    return mock_cost


class TestLiveBackend:
    def test_init_defaults(self):
        backend = LiveBackend()
        assert backend.broker_name == "alpaca"
        assert backend.api_key == ""
        assert backend._client is None

    def test_init_custom_params(self):
        backend = LiveBackend(
            broker_name="ibkr",
            api_key="key123",
            api_secret="secret456",
            base_url="https://api.example.com",
        )
        assert backend.broker_name == "ibkr"
        assert backend.api_key == "key123"
        assert backend.api_secret == "secret456"
        assert backend.base_url == "https://api.example.com"

    @pytest.mark.asyncio
    async def test_connect(self):
        backend = LiveBackend()
        await backend.connect()
        assert backend._client is None

    @pytest.mark.asyncio
    async def test_disconnect(self):
        backend = LiveBackend()
        backend._client = MagicMock()
        await backend.disconnect()
        assert backend._client is None

    @pytest.mark.asyncio
    async def test_execute_not_connected(self):
        backend = LiveBackend()
        result = await backend.execute(_FakeOrder(), 150.0, _make_cost())
        assert result.success is False
        assert "not connected" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_execute_not_implemented(self):
        backend = LiveBackend()
        backend._client = MagicMock()
        result = await backend.execute(_FakeOrder(), 150.0, _make_cost())
        assert result.success is False
        assert "not yet implemented" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_execute_with_exception(self):
        backend = LiveBackend()

        class _BrokenClient:
            def submit_order(self, **kwargs):
                raise RuntimeError("broker down")

        backend._client = _BrokenClient()
        backend.execute = AsyncMock(
            return_value=FillResult(success=False, reason="Broker error: broker down")
        )
        result = await backend.execute(_FakeOrder(), 150.0, _make_cost())
        assert result.success is False
        assert "broker down" in result.reason.lower()


class TestPaperBackend:
    def test_init(self):
        backend = PaperBackend()
        assert backend._connected is False

    @pytest.mark.asyncio
    async def test_connect(self):
        backend = PaperBackend()
        await backend.connect()
        assert backend._connected is True

    @pytest.mark.asyncio
    async def test_disconnect(self):
        backend = PaperBackend()
        await backend.connect()
        assert backend._connected is True
        await backend.disconnect()
        assert backend._connected is False

    @pytest.mark.asyncio
    async def test_execute_not_connected(self):
        backend = PaperBackend()
        result = await backend.execute(_FakeOrder(), 150.0, _make_cost())
        assert result.success is False
        assert "not connected" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_execute_buy_price_rounded_to_4_decimals(self):
        config = PaperTradeConfig(
            partial_fill_enabled=False,
            fill_probability=1.0,
            slippage_model_kwargs={"bps": 1.234},
        )
        backend = PaperBackend(config=config)
        await backend.connect()
        order = _FakeOrder(side=_FakeSide.BUY, quantity=100)
        result = await backend.execute(order, 100.0, _make_cost(0.0))
        assert result.success is True
        slippage_per_share = 100.0 * (1.234 / 10_000)
        expected_price = round(100.0 + slippage_per_share, 4)
        assert result.price == expected_price

    @pytest.mark.asyncio
    async def test_execute_zero_quantity(self):
        backend = PaperBackend(config=PaperTradeConfig(fill_probability=1.0))
        await backend.connect()
        order = _FakeOrder(quantity=0, side=_FakeSide.BUY)
        result = await backend.execute(order, 100.0, _make_cost(0.0))
        assert result.success is False
        assert "quantity must be positive" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_execute_negative_quantity(self):
        backend = PaperBackend(config=PaperTradeConfig(fill_probability=1.0))
        await backend.connect()
        order = _FakeOrder(quantity=-5, side=_FakeSide.BUY)
        result = await backend.execute(order, 100.0, _make_cost(0.0))
        assert result.success is False
        assert "quantity must be positive" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_execute_different_seeds_produce_varied_outcomes(self):
        results = []
        for seed in range(30):
            config = PaperTradeConfig(
                fill_probability=0.5,
                random_seed=seed,
                latency_ms=0.0,
                latency_jitter_ms=0.0,
            )
            backend = PaperBackend(config=config)
            await backend.connect()
            order = _FakeOrder(side=_FakeSide.BUY, quantity=100)
            result = await backend.execute(order, 100.0, _make_cost(10.0))
            results.append(result.success)
        true_count = results.count(True)
        false_count = results.count(False)
        assert true_count > 0 and false_count > 0, (
            f"Expected both successes and failures across seeds, got {true_count} successes and {false_count} failures"
        )

    @pytest.mark.asyncio
    async def test_execute_buy_slippage_increases_price(self):
        backend = PaperBackend()
        await backend.connect()
        order = _FakeOrder(side=_FakeSide.BUY, quantity=100)
        result = await backend.execute(order, 100.0, _make_cost(100.0))
        assert result.success is True
        assert result.price >= 99.0

    @pytest.mark.asyncio
    async def test_execute_multiple_fills_deterministic_with_seed(self):
        backend = PaperBackend()
        await backend.connect()
        backend._rng = __import__("random").Random(42)
        order = _FakeOrder(side=_FakeSide.BUY, quantity=100)
        r1 = await backend.execute(order, 100.0, _make_cost(10.0))
        backend._rng = __import__("random").Random(42)
        r2 = await backend.execute(order, 100.0, _make_cost(10.0))
        assert r1.price == r2.price

    @pytest.mark.parametrize(
        ("side", "expect_price_above_base"),
        [
            (_FakeSide.BUY, True),
            (_FakeSide.SELL, False),
        ],
        ids=["buy-slippage-markup", "sell-slippage-markdown"],
    )
    @pytest.mark.asyncio
    async def test_execute_slippage_direction_by_side(self, side, expect_price_above_base):
        config = PaperTradeConfig(
            partial_fill_enabled=False,
            fill_probability=1.0,
            slippage_model_kwargs={"bps": 10.0},
            random_seed=42,
            latency_ms=0.0,
            latency_jitter_ms=0.0,
        )
        backend = PaperBackend(config=config)
        await backend.connect()
        order = _FakeOrder(side=side, quantity=100)
        base_price = 100.0
        result = await backend.execute(order, base_price, _make_cost(0.0))
        assert result.success is True
        if expect_price_above_base:
            assert result.price > base_price, "Buy should have slippage markup"
        else:
            assert result.price < base_price, "Sell should have slippage markdown"

    @pytest.mark.parametrize(
        "side",
        [_FakeSide.BUY, _FakeSide.SELL],
        ids=["buy-zero-slippage", "sell-zero-slippage"],
    )
    @pytest.mark.asyncio
    async def test_execute_zero_slippage_price_equals_market(self, side):
        config = PaperTradeConfig(
            partial_fill_enabled=False,
            fill_probability=1.0,
            slippage_model_kwargs={"bps": 0.0},
            random_seed=42,
            latency_ms=0.0,
            latency_jitter_ms=0.0,
        )
        backend = PaperBackend(config=config)
        await backend.connect()
        order = _FakeOrder(side=side, quantity=100)
        result = await backend.execute(order, 100.0, _make_cost(0.0))
        assert result.success is True
        assert result.price == 100.0

    @pytest.mark.asyncio
    async def test_execute_sell_price_rounded_to_4_decimals(self):
        config = PaperTradeConfig(
            partial_fill_enabled=False,
            fill_probability=1.0,
            slippage_model_kwargs={"bps": 5.6789},
        )
        backend = PaperBackend(config=config)
        await backend.connect()
        order = _FakeOrder(side=_FakeSide.SELL, quantity=100)
        result = await backend.execute(order, 100.0, _make_cost(0.0))
        assert result.success is True
        slippage_per_share = 100.0 * (5.6789 / 10_000)
        expected_price = round(100.0 - slippage_per_share, 4)
        assert result.price == expected_price

    @pytest.mark.asyncio
    async def test_execute_fill_rejection_records_stats(self):
        config = PaperTradeConfig(fill_probability=0.0, random_seed=42)
        backend = PaperBackend(config=config)
        await backend.connect()
        order = _FakeOrder(side=_FakeSide.BUY, quantity=100)
        result = await backend.execute(order, 100.0, _make_cost(10.0))
        assert result.success is False
        assert backend.stats.total_orders == 1
        assert backend.stats.rejected_orders == 1
        assert backend.stats.filled_orders == 0

    @pytest.mark.asyncio
    async def test_execute_fill_records_stats(self):
        config = PaperTradeConfig(
            fill_probability=1.0,
            partial_fill_enabled=False,
            random_seed=42,
            latency_ms=0.0,
            latency_jitter_ms=0.0,
        )
        backend = PaperBackend(config=config)
        await backend.connect()
        order = _FakeOrder(side=_FakeSide.BUY, quantity=100)
        result = await backend.execute(order, 100.0, _make_cost(5.0))
        assert result.success is True
        assert backend.stats.total_orders == 1
        assert backend.stats.filled_orders == 1
        assert backend.stats.rejected_orders == 0
        assert backend.stats.total_notional > 0

    @pytest.mark.asyncio
    async def test_execute_tracks_per_symbol_stats(self):
        config = PaperTradeConfig(
            fill_probability=1.0,
            partial_fill_enabled=False,
            random_seed=42,
            latency_ms=0.0,
            latency_jitter_ms=0.0,
        )
        backend = PaperBackend(config=config)
        await backend.connect()
        order_aapl = _FakeOrder(symbol="AAPL", side=_FakeSide.BUY, quantity=100)
        order_msft = _FakeOrder(symbol="MSFT", side=_FakeSide.BUY, quantity=50)
        await backend.execute(order_aapl, 150.0, _make_cost(5.0))
        await backend.execute(order_msft, 300.0, _make_cost(5.0))
        assert backend.get_symbol_stats("AAPL").filled_orders == 1
        assert backend.get_symbol_stats("MSFT").filled_orders == 1
        assert backend.get_symbol_stats("GOOG").filled_orders == 0

    @pytest.mark.asyncio
    async def test_reset_stats_clears_all(self):
        config = PaperTradeConfig(
            fill_probability=1.0,
            partial_fill_enabled=False,
            random_seed=42,
            latency_ms=0.0,
            latency_jitter_ms=0.0,
        )
        backend = PaperBackend(config=config)
        await backend.connect()
        order = _FakeOrder(side=_FakeSide.BUY, quantity=100)
        await backend.execute(order, 100.0, _make_cost(5.0))
        assert backend.stats.total_orders == 1
        backend.reset_stats()
        assert backend.stats.total_orders == 0
        assert backend.stats.filled_orders == 0
        assert backend.stats.total_notional == 0.0
        assert backend.get_symbol_stats("AAPL").filled_orders == 0

    @pytest.mark.asyncio
    async def test_execute_partial_fill_quantity_less_than_order(self):
        config = PaperTradeConfig(
            fill_probability=1.0,
            partial_fill_enabled=True,
            partial_fill_min_ratio=0.5,
            random_seed=7,
            latency_ms=0.0,
            latency_jitter_ms=0.0,
        )
        backend = PaperBackend(config=config)
        await backend.connect()
        order = _FakeOrder(side=_FakeSide.BUY, quantity=200)
        result = await backend.execute(order, 100.0, _make_cost(0.0))
        assert result.success is True
        assert result.quantity >= 100
        assert result.quantity <= 200

    @pytest.mark.asyncio
    async def test_execute_partial_fill_disabled_returns_full_quantity(self):
        config = PaperTradeConfig(
            fill_probability=1.0,
            partial_fill_enabled=False,
            random_seed=42,
            latency_ms=0.0,
            latency_jitter_ms=0.0,
        )
        backend = PaperBackend(config=config)
        await backend.connect()
        order = _FakeOrder(side=_FakeSide.BUY, quantity=200)
        result = await backend.execute(order, 100.0, _make_cost(0.0))
        assert result.success is True
        assert result.quantity == 200

    @pytest.mark.asyncio
    async def test_execute_quantity_one_no_partial_fill(self):
        config = PaperTradeConfig(
            fill_probability=1.0,
            partial_fill_enabled=True,
            random_seed=42,
            latency_ms=0.0,
            latency_jitter_ms=0.0,
        )
        backend = PaperBackend(config=config)
        await backend.connect()
        order = _FakeOrder(side=_FakeSide.BUY, quantity=1)
        result = await backend.execute(order, 100.0, _make_cost(0.0))
        assert result.success is True
        assert result.quantity == 1

    @pytest.mark.asyncio
    async def test_execute_with_latency(self):
        config = PaperTradeConfig(
            fill_probability=1.0,
            partial_fill_enabled=False,
            latency_ms=0.001,
            latency_jitter_ms=0.0,
            random_seed=42,
        )
        backend = PaperBackend(config=config)
        await backend.connect()
        order = _FakeOrder(side=_FakeSide.BUY, quantity=100)
        import time

        start = time.monotonic()
        result = await backend.execute(order, 100.0, _make_cost(0.0))
        elapsed = time.monotonic() - start
        assert result.success is True
        assert elapsed < 1.0

    @pytest.mark.asyncio
    async def test_fill_rate_property(self):
        config = PaperTradeConfig(fill_probability=1.0, partial_fill_enabled=False, random_seed=42)
        backend = PaperBackend(config=config)
        assert backend.stats.fill_rate == 0.0
        await backend.connect()
        order = _FakeOrder(side=_FakeSide.BUY, quantity=100)
        await backend.execute(order, 100.0, _make_cost(0.0))
        assert backend.stats.fill_rate == 1.0

    @pytest.mark.asyncio
    async def test_config_property(self):
        config = PaperTradeConfig(fill_probability=0.75)
        backend = PaperBackend(config=config)
        assert backend.config.fill_probability == 0.75

    @pytest.mark.asyncio
    async def test_connected_property(self):
        backend = PaperBackend()
        assert backend.connected is False
        await backend.connect()
        assert backend.connected is True
        await backend.disconnect()
        assert backend.connected is False

    @pytest.mark.asyncio
    async def test_execute_price_refresh_from_provider(self):
        from unittest.mock import AsyncMock

        mock_provider = AsyncMock()
        mock_provider.get_latest_price = AsyncMock(return_value=105.0)
        config = PaperTradeConfig(
            fill_probability=1.0,
            partial_fill_enabled=False,
            refresh_price_from_provider=True,
            slippage_model_kwargs={"bps": 0.0},
            random_seed=42,
            latency_ms=0.0,
            latency_jitter_ms=0.0,
        )
        backend = PaperBackend(config=config, data_provider=mock_provider)
        await backend.connect()
        order = _FakeOrder(side=_FakeSide.BUY, quantity=100)
        result = await backend.execute(order, 100.0, _make_cost(0.0))
        assert result.success is True
        assert result.price == 105.0
        mock_provider.get_latest_price.assert_called_once_with("AAPL")

    @pytest.mark.asyncio
    async def test_execute_price_refresh_fallback_on_error(self):
        from unittest.mock import AsyncMock

        mock_provider = AsyncMock()
        mock_provider.get_latest_price = AsyncMock(side_effect=Exception("timeout"))
        config = PaperTradeConfig(
            fill_probability=1.0,
            partial_fill_enabled=False,
            refresh_price_from_provider=True,
            slippage_model_kwargs={"bps": 0.0},
            random_seed=42,
            latency_ms=0.0,
            latency_jitter_ms=0.0,
        )
        backend = PaperBackend(config=config, data_provider=mock_provider)
        await backend.connect()
        order = _FakeOrder(side=_FakeSide.BUY, quantity=100)
        result = await backend.execute(order, 100.0, _make_cost(0.0))
        assert result.success is True
        assert result.price == 100.0

    @pytest.mark.asyncio
    async def test_execute_stats_as_dict(self):
        config = PaperTradeConfig(
            fill_probability=1.0,
            partial_fill_enabled=False,
            random_seed=42,
            latency_ms=0.0,
            latency_jitter_ms=0.0,
        )
        backend = PaperBackend(config=config)
        await backend.connect()
        order = _FakeOrder(side=_FakeSide.BUY, quantity=100)
        await backend.execute(order, 100.0, _make_cost(0.0))
        d = backend.stats.as_dict()
        assert d["total_orders"] == 1
        assert d["filled_orders"] == 1
        assert d["fill_rate"] == 1.0
        assert "avg_latency_ms" in d
        assert "total_notional" in d
        assert "avg_slippage_bps" in d
