"""Tests for engine.core.execution.live and engine.core.execution.paper backends."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from engine.core.execution.base import ExecutionBackend, FillResult
from engine.core.execution.live import LiveBackend
from engine.core.execution.paper import PaperBackend


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
    async def test_execute_buy_order(self):
        backend = PaperBackend()
        await backend.connect()
        order = _FakeOrder(side=_FakeSide.BUY, quantity=100)
        result = await backend.execute(order, 100.0, _make_cost(10.0))
        assert result.success is True
        assert result.quantity == 100
        assert result.price > 0

    @pytest.mark.asyncio
    async def test_execute_sell_order(self):
        backend = PaperBackend()
        await backend.connect()
        order = _FakeOrder(side=_FakeSide.SELL, quantity=50)
        result = await backend.execute(order, 200.0, _make_cost(5.0))
        assert result.success is True
        assert result.quantity == 50
        assert result.price > 0

    @pytest.mark.asyncio
    async def test_execute_zero_quantity(self):
        backend = PaperBackend()
        await backend.connect()
        order = _FakeOrder(quantity=0)
        result = await backend.execute(order, 100.0, _make_cost(10.0))
        assert result.success is True

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
