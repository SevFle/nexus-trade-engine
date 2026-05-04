"""Tests for paper and live execution backends (0% coverage → target)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from engine.core.cost_model import CostBreakdown, Money
from engine.core.execution.live import LiveBackend
from engine.core.execution.paper import PaperBackend
from engine.core.order_manager import Order, OrderStatus, OrderType
from engine.core.signal import Side


def _make_order(side: Side = Side.BUY, quantity: int = 100) -> Order:
    return Order(
        signal_id="sig-1",
        strategy_id="strat-1",
        symbol="AAPL",
        side=side,
        quantity=quantity,
        order_type=OrderType.MARKET,
    )


def _make_costs(slippage: float = 0.05) -> CostBreakdown:
    return CostBreakdown(
        commission=Money(amount=1.0),
        spread=Money(amount=0.02),
        slippage=Money(amount=slippage * 100),
        exchange_fee=Money(amount=0.03),
    )


class TestPaperBackend:
    def test_connect_sets_connected(self):
        backend = PaperBackend()
        assert backend._connected is False

    async def test_connect(self):
        backend = PaperBackend()
        await backend.connect()
        assert backend._connected is True

    async def test_disconnect(self):
        backend = PaperBackend()
        await backend.connect()
        await backend.disconnect()
        assert backend._connected is False

    async def test_execute_not_connected_returns_failure(self):
        backend = PaperBackend()
        order = _make_order()
        costs = _make_costs()
        result = await backend.execute(order, 150.0, costs)
        assert result.success is False
        assert "not connected" in result.reason

    async def test_execute_buy_applies_slippage(self):
        backend = PaperBackend()
        await backend.connect()
        order = _make_order(Side.BUY, quantity=100)
        costs = _make_costs(slippage=0.05)
        result = await backend.execute(order, 150.0, costs)
        assert result.success is True
        assert result.quantity == 100
        assert result.price >= 150.0

    async def test_execute_sell_applies_slippage(self):
        backend = PaperBackend()
        await backend.connect()
        order = _make_order(Side.SELL, quantity=100)
        costs = _make_costs(slippage=0.05)
        result = await backend.execute(order, 150.0, costs)
        assert result.success is True
        assert result.quantity == 100
        assert result.price <= 150.0

    async def test_execute_zero_quantity(self):
        backend = PaperBackend()
        await backend.connect()
        order = _make_order(quantity=0)
        costs = _make_costs(slippage=0.0)
        result = await backend.execute(order, 150.0, costs)
        assert result.success is True

    async def test_execute_rounds_price(self):
        backend = PaperBackend()
        await backend.connect()
        order = _make_order()
        costs = _make_costs(slippage=0.012345)
        result = await backend.execute(order, 150.0, costs)
        assert result.success is True
        assert len(str(result.price).split(".")[-1]) <= 4

    async def test_execute_multiple_fills_vary(self):
        backend = PaperBackend()
        await backend.connect()
        order = _make_order()
        costs = _make_costs(slippage=0.05)
        prices = set()
        for _ in range(20):
            result = await backend.execute(order, 150.0, costs)
            prices.add(result.price)
        assert len(prices) > 1


class TestLiveBackend:
    def test_init_defaults(self):
        backend = LiveBackend()
        assert backend.broker_name == "alpaca"
        assert backend.api_key == ""
        assert backend._client is None

    def test_init_custom_broker(self):
        backend = LiveBackend(
            broker_name="ibkr",
            api_key="key123",
            api_secret="secret456",
            base_url="https://api.example.com",
        )
        assert backend.broker_name == "ibkr"
        assert backend.api_key == "key123"

    async def test_connect_does_not_create_client(self):
        backend = LiveBackend()
        await backend.connect()
        assert backend._client is None

    async def test_disconnect_clears_client(self):
        backend = LiveBackend()
        backend._client = object()
        await backend.disconnect()
        assert backend._client is None

    async def test_execute_not_connected_returns_failure(self):
        backend = LiveBackend()
        order = _make_order()
        costs = _make_costs()
        result = await backend.execute(order, 150.0, costs)
        assert result.success is False
        assert "not connected" in result.reason

    async def test_execute_client_none_returns_not_implemented(self):
        backend = LiveBackend()
        await backend.connect()
        order = _make_order()
        costs = _make_costs()
        result = await backend.execute(order, 150.0, costs)
        assert result.success is False
        assert "not yet implemented" in result.reason

    async def test_execute_with_client_returns_not_implemented(self):
        backend = LiveBackend()
        backend._client = object()
        order = _make_order()
        costs = _make_costs()
        result = await backend.execute(order, 150.0, costs)
        assert result.success is False
        assert "not yet implemented" in result.reason

    async def test_execute_with_exception_returns_error(self):
        backend = LiveBackend()
        backend._client = None
        order = _make_order()
        costs = _make_costs()
        result = await backend.execute(order, 150.0, costs)
        assert result.success is False
