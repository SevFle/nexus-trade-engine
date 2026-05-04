from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from engine.core.cost_model import CostBreakdown, Money
from engine.core.execution.base import FillResult
from engine.core.execution.live import LiveBackend
from engine.core.execution.paper import PaperBackend
from engine.core.order_manager import Order, OrderType
from engine.core.signal import Side


def _make_order(symbol: str = "AAPL", side: Side = Side.BUY, quantity: int = 100) -> Order:
    return Order(
        signal_id="sig-1",
        strategy_id="strat-1",
        symbol=symbol,
        side=side,
        quantity=quantity,
        order_type=OrderType.MARKET,
    )


def _make_costs(slippage: float = 0.05) -> CostBreakdown:
    return CostBreakdown(slippage=Money(slippage))


class TestLiveBackend:
    def test_init_defaults(self):
        backend = LiveBackend()
        assert backend.broker_name == "alpaca"
        assert backend._client is None

    def test_init_custom(self):
        backend = LiveBackend(
            broker_name="ibkr",
            api_key="key",
            api_secret="secret",
            base_url="https://api.example.com",
        )
        assert backend.broker_name == "ibkr"
        assert backend.api_key == "key"

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
        order = _make_order()
        costs = _make_costs()
        result = await backend.execute(order, market_price=150.0, costs=costs)
        assert result.success is False
        assert "not connected" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_execute_not_implemented(self):
        backend = LiveBackend()
        backend._client = MagicMock()
        order = _make_order()
        costs = _make_costs()
        result = await backend.execute(order, market_price=150.0, costs=costs)
        assert result.success is False
        assert "not yet implemented" in result.reason.lower()


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
        await backend.disconnect()
        assert backend._connected is False

    @pytest.mark.asyncio
    async def test_execute_not_connected(self):
        backend = PaperBackend()
        order = _make_order()
        costs = _make_costs()
        result = await backend.execute(order, market_price=150.0, costs=costs)
        assert result.success is False
        assert "not connected" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_execute_buy(self):
        backend = PaperBackend()
        await backend.connect()
        order = _make_order(side=Side.BUY, quantity=100)
        costs = _make_costs(slippage=0.50)
        result = await backend.execute(order, market_price=150.0, costs=costs)
        assert result.success is True
        assert result.quantity == 100
        assert result.price > 0

    @pytest.mark.asyncio
    async def test_execute_sell(self):
        backend = PaperBackend()
        await backend.connect()
        order = _make_order(side=Side.SELL, quantity=50)
        costs = _make_costs(slippage=0.30)
        result = await backend.execute(order, market_price=100.0, costs=costs)
        assert result.success is True
        assert result.quantity == 50
        assert result.price > 0

    @pytest.mark.asyncio
    async def test_execute_zero_quantity(self):
        backend = PaperBackend()
        await backend.connect()
        order = _make_order(quantity=0)
        costs = _make_costs(slippage=0.0)
        result = await backend.execute(order, market_price=100.0, costs=costs)
        assert result.success is True
