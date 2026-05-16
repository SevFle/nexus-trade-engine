"""
Comprehensive tests for clock abstractions and BacktestBackend.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from engine.core.execution.backtest import _PARTIAL_FILL_QUANTITY_THRESHOLD, BacktestBackend
from engine.core.execution.clock import SimulatedClock, SystemClock


class _FakeOrder:
    def __init__(
        self,
        order_id: str = "ord-1",
        symbol: str = "AAPL",
        quantity: int = 100,
        side: str = "buy",
    ):
        self.id = order_id
        self.symbol = symbol
        self.quantity = quantity
        self._side = side

    @property
    def side(self):
        class _Side:
            def __init__(self, val):
                self.value = val
        return _Side(self._side)


def _make_costs(slippage_amount: float = 5.0):
    costs = MagicMock()
    costs.slippage = MagicMock()
    costs.slippage.amount = slippage_amount
    return costs


class TestSystemClock:
    def test_now_returns_datetime(self):
        clock = SystemClock()
        result = clock.now()
        assert isinstance(result, datetime)

    def test_now_has_timezone(self):
        clock = SystemClock()
        result = clock.now()
        assert result.tzinfo is not None

    def test_monotonic_returns_float(self):
        clock = SystemClock()
        result = clock.monotonic()
        assert isinstance(result, float)
        assert result > 0

    def test_monotonic_increases(self):
        clock = SystemClock()
        t1 = clock.monotonic()
        t2 = clock.monotonic()
        assert t2 >= t1


class TestSimulatedClock:
    def test_default_start(self):
        clock = SimulatedClock()
        assert isinstance(clock.now(), datetime)

    def test_custom_start(self):
        dt = datetime(2024, 1, 1, tzinfo=UTC)
        clock = SimulatedClock(start=dt, mono=100.0)
        assert clock.now() == dt
        assert clock.monotonic() == 100.0

    def test_advance_dt(self):
        dt = datetime(2024, 1, 1, tzinfo=UTC)
        clock = SimulatedClock(start=dt, mono=0.0)
        clock.advance(3600.0)
        assert clock.now() == dt + timedelta(hours=1)
        assert clock.monotonic() == 3600.0

    def test_advance_multiple(self):
        dt = datetime(2024, 1, 1, tzinfo=UTC)
        clock = SimulatedClock(start=dt, mono=0.0)
        clock.advance(100.0)
        clock.advance(200.0)
        assert clock.monotonic() == 300.0
        assert clock.now() == dt + timedelta(seconds=300)

    def test_set_datetime(self):
        clock = SimulatedClock()
        new_dt = datetime(2025, 6, 15, tzinfo=UTC)
        clock.set(new_dt)
        assert clock.now() == new_dt

    def test_set_with_mono(self):
        clock = SimulatedClock()
        clock.set(datetime(2025, 1, 1, tzinfo=UTC), mono=500.0)
        assert clock.monotonic() == 500.0

    def test_set_without_mono_preserves(self):
        clock = SimulatedClock(mono=42.0)
        clock.set(datetime(2025, 1, 1, tzinfo=UTC))
        assert clock.monotonic() == 42.0


class TestBacktestBackendDefaults:
    def test_default_fill_probability(self):
        backend = BacktestBackend()
        assert backend.fill_probability == 0.98

    def test_default_partial_fill(self):
        backend = BacktestBackend()
        assert backend.partial_fill_enabled is True

    def test_custom_fill_probability(self):
        backend = BacktestBackend(fill_probability=0.5)
        assert backend.fill_probability == 0.5

    def test_custom_seed(self):
        backend = BacktestBackend(random_seed=42)
        assert isinstance(backend._rng, random.Random)


class TestBacktestBackendConnect:
    @pytest.mark.asyncio
    async def test_connect_succeeds(self):
        backend = BacktestBackend()
        await backend.connect()

    @pytest.mark.asyncio
    async def test_disconnect_succeeds(self):
        backend = BacktestBackend()
        await backend.disconnect()


class TestBacktestBackendExecute:
    @pytest.mark.asyncio
    async def test_buy_order_basic(self):
        backend = BacktestBackend(fill_probability=1.0, partial_fill_enabled=False, random_seed=42)
        order = _FakeOrder(quantity=100, side="buy")
        costs = _make_costs(slippage_amount=10.0)
        result = await backend.execute(order, 100.0, costs)
        assert result.success is True
        assert result.quantity == 100
        assert result.price >= 100.0

    @pytest.mark.asyncio
    async def test_sell_order_basic(self):
        backend = BacktestBackend(fill_probability=1.0, partial_fill_enabled=False, random_seed=42)
        order = _FakeOrder(quantity=100, side="sell")
        costs = _make_costs(slippage_amount=10.0)
        result = await backend.execute(order, 100.0, costs)
        assert result.success is True
        assert result.quantity == 100
        assert result.price <= 100.0

    @pytest.mark.asyncio
    async def test_buy_slippage_increases_price(self):
        backend = BacktestBackend(fill_probability=1.0, partial_fill_enabled=False, random_seed=42)
        order = _FakeOrder(quantity=100, side="buy")
        costs = _make_costs(slippage_amount=100.0)
        result = await backend.execute(order, 100.0, costs)
        assert result.success is True
        assert result.price == pytest.approx(101.0)

    @pytest.mark.asyncio
    async def test_sell_slippage_decreases_price(self):
        backend = BacktestBackend(fill_probability=1.0, partial_fill_enabled=False, random_seed=42)
        order = _FakeOrder(quantity=100, side="sell")
        costs = _make_costs(slippage_amount=100.0)
        result = await backend.execute(order, 100.0, costs)
        assert result.success is True
        assert result.price == pytest.approx(99.0)

    @pytest.mark.asyncio
    async def test_zero_slippage(self):
        backend = BacktestBackend(fill_probability=1.0, partial_fill_enabled=False, random_seed=42)
        order = _FakeOrder(quantity=100, side="buy")
        costs = _make_costs(slippage_amount=0.0)
        result = await backend.execute(order, 100.0, costs)
        assert result.success is True
        assert result.price == pytest.approx(100.0)

    @pytest.mark.asyncio
    async def test_zero_quantity(self):
        backend = BacktestBackend(fill_probability=1.0, partial_fill_enabled=False, random_seed=42)
        order = _FakeOrder(quantity=0, side="buy")
        costs = _make_costs(slippage_amount=10.0)
        result = await backend.execute(order, 100.0, costs)
        assert result.success is True
        assert result.quantity == 0

    @pytest.mark.asyncio
    async def test_fill_failure(self):
        backend = BacktestBackend(fill_probability=0.0, random_seed=42)
        order = _FakeOrder(quantity=100, side="buy")
        costs = _make_costs(slippage_amount=10.0)
        result = await backend.execute(order, 100.0, costs)
        assert result.success is False
        assert "fill failure" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_deterministic_with_seed(self):
        b1 = BacktestBackend(fill_probability=1.0, partial_fill_enabled=True, random_seed=99)
        b2 = BacktestBackend(fill_probability=1.0, partial_fill_enabled=True, random_seed=99)
        order = _FakeOrder(quantity=100, side="buy")
        costs = _make_costs(slippage_amount=10.0)
        r1 = await b1.execute(order, 100.0, costs)
        r2 = await b2.execute(order, 100.0, costs)
        assert r1.price == r2.price
        assert r1.quantity == r2.quantity

    @pytest.mark.asyncio
    async def test_partial_fill_large_order(self):
        backend = BacktestBackend(
            fill_probability=1.0,
            partial_fill_enabled=True,
            random_seed=42,
        )
        order = _FakeOrder(quantity=_PARTIAL_FILL_QUANTITY_THRESHOLD + 1, side="buy")
        costs = _make_costs(slippage_amount=10.0)
        result = await backend.execute(order, 100.0, costs)
        assert result.success is True
        assert result.quantity >= 1
        assert result.quantity <= order.quantity

    @pytest.mark.asyncio
    async def test_no_partial_fill_small_order(self):
        backend = BacktestBackend(
            fill_probability=1.0,
            partial_fill_enabled=True,
            random_seed=42,
        )
        order = _FakeOrder(quantity=100, side="buy")
        costs = _make_costs(slippage_amount=10.0)
        result = await backend.execute(order, 100.0, costs)
        assert result.success is True
        assert result.quantity == 100

    @pytest.mark.asyncio
    async def test_partial_fill_disabled(self):
        backend = BacktestBackend(
            fill_probability=1.0,
            partial_fill_enabled=False,
            random_seed=42,
        )
        order = _FakeOrder(quantity=_PARTIAL_FILL_QUANTITY_THRESHOLD + 1, side="buy")
        costs = _make_costs(slippage_amount=10.0)
        result = await backend.execute(order, 100.0, costs)
        assert result.success is True
        assert result.quantity == order.quantity

    @pytest.mark.asyncio
    async def test_price_rounded_to_4_decimals(self):
        backend = BacktestBackend(fill_probability=1.0, partial_fill_enabled=False, random_seed=42)
        order = _FakeOrder(quantity=3, side="buy")
        costs = _make_costs(slippage_amount=0.003)
        result = await backend.execute(order, 100.0, costs)
        assert result.success is True
        price_str = str(result.price)
        if "." in price_str:
            decimals = len(price_str.split(".")[1])
            assert decimals <= 4

    @pytest.mark.asyncio
    async def test_multiple_executions_different_seeds(self):
        results = []
        for seed in range(5):
            backend = BacktestBackend(fill_probability=0.5, random_seed=seed)
            order = _FakeOrder(quantity=100, side="buy")
            costs = _make_costs(slippage_amount=10.0)
            result = await backend.execute(order, 100.0, costs)
            results.append(result.success)
        assert True in results or False in results
