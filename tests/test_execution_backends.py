from __future__ import annotations

import pytest

from engine.core.cost_model import CostBreakdown, Money
from engine.core.execution.base import ExecutionBackend, FillResult
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
    return CostBreakdown(slippage=Money(slippage))


class TestLiveBackend:
    @pytest.fixture
    def backend(self):
        return LiveBackend(broker_name="test-broker")

    def test_init_defaults(self):
        backend = LiveBackend()
        assert backend.broker_name == "alpaca"
        assert backend.api_key == ""
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

    async def test_connect_logs(self, backend):
        await backend.connect()
        assert backend._client is None

    async def test_disconnect(self, backend):
        backend._client = object()
        await backend.disconnect()
        assert backend._client is None

    async def test_execute_without_client_returns_failure(self, backend):
        order = _make_order()
        costs = _make_costs()
        result = await backend.execute(order, 150.0, costs)
        assert result.success is False
        assert "not connected" in result.reason

    async def test_execute_with_client_not_implemented(self, backend):
        backend._client = object()
        order = _make_order()
        costs = _make_costs()
        result = await backend.execute(order, 150.0, costs)
        assert result.success is False
        assert "not yet implemented" in result.reason


class TestPaperBackend:
    @pytest.fixture
    def backend(self):
        return PaperBackend()

    def test_init(self, backend):
        assert backend._connected is False

    async def test_connect(self, backend):
        await backend.connect()
        assert backend._connected is True

    async def test_disconnect(self, backend):
        backend._connected = True
        await backend.disconnect()
        assert backend._connected is False

    async def test_execute_without_connect_returns_failure(self, backend):
        order = _make_order()
        costs = _make_costs()
        result = await backend.execute(order, 150.0, costs)
        assert result.success is False
        assert "not connected" in result.reason

    async def test_execute_buy_fills(self, backend):
        await backend.connect()
        order = _make_order(side=Side.BUY, quantity=100)
        costs = _make_costs(slippage=5.0)
        result = await backend.execute(order, 150.0, costs)
        assert result.success is True
        assert result.quantity == 100
        assert result.price > 0

    async def test_execute_sell_fills(self, backend):
        await backend.connect()
        order = _make_order(side=Side.SELL, quantity=50)
        costs = _make_costs(slippage=2.5)
        result = await backend.execute(order, 100.0, costs)
        assert result.success is True
        assert result.quantity == 50
        assert result.price > 0

    async def test_execute_zero_quantity_no_division_error(self, backend):
        await backend.connect()
        order = _make_order(quantity=0)
        costs = _make_costs(slippage=1.0)
        result = await backend.execute(order, 100.0, costs)
        assert result.success is True

    async def test_execute_fill_price_has_slippage_jitter(self, backend):
        await backend.connect()
        order = _make_order(side=Side.BUY, quantity=100)
        costs = _make_costs(slippage=10.0)
        prices = set()
        for _ in range(20):
            result = await backend.execute(order, 100.0, costs)
            prices.add(result.price)
        assert len(prices) > 1
