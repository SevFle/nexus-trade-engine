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
    async def test_execute_different_seeds_produce_varied_outcomes(self):
        results = []
        for seed in range(10):
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
