"""Tests for the PaperTradeBroker — full execution engine tests."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from unittest.mock import MagicMock

import pytest

from engine.core.execution.commission import ZeroCommission
from engine.core.execution.paper_broker_interface import (
    IPaperTradeBroker,
    OrderRejectReason,
    PaperTradeBrokerConfig,
    PaperTradeRiskConfig,
)
from engine.core.execution.paper_trade_broker import PaperTradeBroker


class _FakeSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


@dataclass
class _FakeCostBreakdown:
    slippage: Any = None


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


def _make_config(**overrides: Any) -> PaperTradeBrokerConfig:
    defaults = {
        "fill_probability": 1.0,
        "partial_fill_enabled": False,
        "latency_ms": 0.0,
        "latency_jitter_ms": 0.0,
        "random_seed": 42,
    }
    defaults.update(overrides)
    return PaperTradeBrokerConfig(**defaults)


class TestPaperTradeBrokerConstruction:
    def test_satisfies_ipapertradebroker_protocol(self):
        broker = PaperTradeBroker(config=_make_config())
        assert isinstance(broker, IPaperTradeBroker)

    def test_default_config(self):
        broker = PaperTradeBroker()
        assert not broker.connected
        assert broker.config is not None

    def test_custom_config(self):
        config = _make_config(fill_probability=0.8, latency_ms=100.0)
        broker = PaperTradeBroker(config=config)
        assert broker.config.fill_probability == 0.8
        assert broker.config.latency_ms == 100.0

    def test_with_zero_commission(self):
        broker = PaperTradeBroker(
            config=_make_config(),
            commission_calculator=ZeroCommission(),
        )
        assert isinstance(broker._commission, ZeroCommission)


class TestConnectDisconnect:
    @pytest.mark.asyncio
    async def test_connect(self):
        broker = PaperTradeBroker(config=_make_config())
        await broker.connect()
        assert broker.connected

    @pytest.mark.asyncio
    async def test_disconnect(self):
        broker = PaperTradeBroker(config=_make_config())
        await broker.connect()
        await broker.disconnect()
        assert not broker.connected

    @pytest.mark.asyncio
    async def test_execute_not_connected(self):
        broker = PaperTradeBroker(config=_make_config())
        result = await broker.execute(_FakeOrder(), 150.0, _make_cost())
        assert not result.success
        assert "not connected" in result.reason.lower()


class TestExecutionBackend:
    @pytest.mark.asyncio
    async def test_buy_order(self):
        broker = PaperTradeBroker(config=_make_config(), initial_cash=100_000.0)
        await broker.connect()
        order = _FakeOrder(side=_FakeSide.BUY, quantity=100)
        result = await broker.execute(order, 150.0, _make_cost(5.0))
        assert result.success
        assert result.quantity == 100
        assert result.price > 0

    @pytest.mark.asyncio
    async def test_sell_order(self):
        broker = PaperTradeBroker(config=_make_config(), initial_cash=100_000.0)
        await broker.connect()
        buy_order = _FakeOrder(side=_FakeSide.BUY, quantity=100)
        await broker.execute(buy_order, 150.0, _make_cost())
        sell_order = _FakeOrder(side=_FakeSide.SELL, quantity=100)
        result = await broker.execute(sell_order, 160.0, _make_cost())
        assert result.success
        assert result.quantity > 0

    @pytest.mark.asyncio
    async def test_slippage_applied_to_buy(self):
        broker = PaperTradeBroker(config=_make_config(), initial_cash=100_000.0)
        await broker.connect()
        order = _FakeOrder(side=_FakeSide.BUY, quantity=100)
        result = await broker.execute(order, 100.0, _make_cost(100.0))
        assert result.price >= 100.0

    @pytest.mark.asyncio
    async def test_slippage_applied_to_sell(self):
        broker = PaperTradeBroker(config=_make_config(), initial_cash=100_000.0)
        await broker.connect()
        buy = _FakeOrder(side=_FakeSide.BUY, quantity=100)
        await broker.execute(buy, 100.0, _make_cost())
        sell = _FakeOrder(side=_FakeSide.SELL, quantity=100)
        result = await broker.execute(sell, 100.0, _make_cost(100.0))
        assert result.price <= 100.0

    @pytest.mark.asyncio
    async def test_deterministic_with_seed(self):

        config = _make_config(partial_fill_enabled=True, random_seed=42)
        broker = PaperTradeBroker(config=config, initial_cash=200_000.0)
        await broker.connect()

        order1 = _FakeOrder(side=_FakeSide.BUY, quantity=2000)
        r1 = await broker.execute(order1, 100.0, _make_cost())

        config2 = _make_config(partial_fill_enabled=True, random_seed=42)
        broker2 = PaperTradeBroker(config=config2, initial_cash=200_000.0)
        await broker2.connect()
        order2 = _FakeOrder(side=_FakeSide.BUY, quantity=2000)
        r2 = await broker2.execute(order2, 100.0, _make_cost())
        assert r1.quantity == r2.quantity

    @pytest.mark.asyncio
    async def test_fill_rejection_with_probability(self):
        config = _make_config(fill_probability=0.0)
        broker = PaperTradeBroker(config=config, initial_cash=100_000.0)
        await broker.connect()
        order = _FakeOrder()
        result = await broker.execute(order, 150.0, _make_cost())
        assert not result.success


class TestSubmitOrder:
    @pytest.mark.asyncio
    async def test_market_buy(self):
        broker = PaperTradeBroker(config=_make_config(), initial_cash=100_000.0)
        await broker.connect()
        broker.update_market_price("AAPL", 150.0)
        result = await broker.submit_order("AAPL", "buy", 100)
        assert result.success
        assert result.quantity > 0

    @pytest.mark.asyncio
    async def test_market_sell(self):
        broker = PaperTradeBroker(config=_make_config(), initial_cash=100_000.0)
        await broker.connect()
        broker.update_market_price("AAPL", 150.0)
        await broker.submit_order("AAPL", "buy", 100)
        result = await broker.submit_order("AAPL", "sell", 100)
        assert result.success

    @pytest.mark.asyncio
    async def test_limit_buy_fills_when_price_at_limit(self):
        broker = PaperTradeBroker(config=_make_config(), initial_cash=100_000.0)
        await broker.connect()
        broker.update_market_price("AAPL", 145.0)
        result = await broker.submit_order("AAPL", "buy", 100, order_type="limit", limit_price=150.0)
        assert result.success
        assert result.price == 150.0

    @pytest.mark.asyncio
    async def test_limit_buy_rests_when_price_above_limit(self):
        broker = PaperTradeBroker(config=_make_config(), initial_cash=100_000.0)
        await broker.connect()
        broker.update_market_price("AAPL", 155.0)
        result = await broker.submit_order("AAPL", "buy", 100, order_type="limit", limit_price=150.0)
        assert not result.success
        assert "resting" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_limit_sell_fills_when_price_at_limit(self):
        broker = PaperTradeBroker(config=_make_config(), initial_cash=100_000.0)
        await broker.connect()
        broker.update_market_price("AAPL", 155.0)
        await broker.submit_order("AAPL", "buy", 100)
        result = await broker.submit_order("AAPL", "sell", 100, order_type="limit", limit_price=150.0)
        assert result.success

    @pytest.mark.asyncio
    async def test_stop_buy_triggers_when_price_above_stop(self):
        broker = PaperTradeBroker(config=_make_config(), initial_cash=100_000.0)
        await broker.connect()
        broker.update_market_price("AAPL", 155.0)
        result = await broker.submit_order("AAPL", "buy", 100, order_type="stop", stop_price=150.0)
        assert result.success

    @pytest.mark.asyncio
    async def test_stop_buy_rests_when_price_below_stop(self):
        broker = PaperTradeBroker(config=_make_config(), initial_cash=100_000.0)
        await broker.connect()
        broker.update_market_price("AAPL", 145.0)
        result = await broker.submit_order("AAPL", "buy", 100, order_type="stop", stop_price=150.0)
        assert not result.success
        assert "resting" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_invalid_quantity_rejected(self):
        broker = PaperTradeBroker(config=_make_config(), initial_cash=100_000.0)
        await broker.connect()
        result = await broker.submit_order("AAPL", "buy", 0)
        assert not result.success
        assert OrderRejectReason.INVALID_ORDER in result.reason

    @pytest.mark.asyncio
    async def test_no_price_rejected(self):
        broker = PaperTradeBroker(config=_make_config(), initial_cash=100_000.0)
        await broker.connect()
        result = await broker.submit_order("UNKNOWN", "buy", 100)
        assert not result.success


class TestCancelOrder:
    @pytest.mark.asyncio
    async def test_cancel_open_order(self):
        broker = PaperTradeBroker(config=_make_config(), initial_cash=100_000.0)
        await broker.connect()
        broker.update_market_price("AAPL", 155.0)
        await broker.submit_order("AAPL", "buy", 100, order_type="limit", limit_price=150.0)
        open_orders = await broker.get_open_orders()
        assert len(open_orders) == 1
        cancelled = await broker.cancel_order(open_orders[0]["order_id"])
        assert cancelled
        open_orders = await broker.get_open_orders()
        assert len(open_orders) == 0

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_order(self):
        broker = PaperTradeBroker(config=_make_config(), initial_cash=100_000.0)
        await broker.connect()
        cancelled = await broker.cancel_order("nonexistent")
        assert not cancelled


class TestPositionTracking:
    @pytest.mark.asyncio
    async def test_positions_after_buy(self):
        broker = PaperTradeBroker(config=_make_config(), initial_cash=100_000.0)
        await broker.connect()
        broker.update_market_price("AAPL", 150.0)
        await broker.submit_order("AAPL", "buy", 100)
        positions = await broker.get_positions()
        assert "AAPL" in positions
        assert positions["AAPL"].quantity == 100

    @pytest.mark.asyncio
    async def test_positions_cleared_after_sell(self):
        broker = PaperTradeBroker(config=_make_config(), initial_cash=100_000.0)
        await broker.connect()
        broker.update_market_price("AAPL", 150.0)
        await broker.submit_order("AAPL", "buy", 100)
        broker.update_market_price("AAPL", 160.0)
        await broker.submit_order("AAPL", "sell", 100)
        positions = await broker.get_positions()
        assert "AAPL" not in positions

    @pytest.mark.asyncio
    async def test_portfolio_snapshot(self):
        broker = PaperTradeBroker(config=_make_config(), initial_cash=100_000.0)
        await broker.connect()
        broker.update_market_price("AAPL", 150.0)
        await broker.submit_order("AAPL", "buy", 100)
        portfolio = await broker.get_portfolio()
        assert portfolio.cash < 100_000.0
        assert portfolio.total_equity > 0
        assert isinstance(portfolio.positions, dict)


class TestRiskChecks:
    @pytest.mark.asyncio
    async def test_max_position_size(self):
        risk = PaperTradeRiskConfig(max_position_size=50)
        config = _make_config(risk_config=risk)
        broker = PaperTradeBroker(config=config, initial_cash=1_000_000.0)
        await broker.connect()
        broker.update_market_price("AAPL", 150.0)
        result = await broker.submit_order("AAPL", "buy", 100)
        assert not result.success
        assert "position" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_banned_symbol(self):
        risk = PaperTradeRiskConfig(banned_symbols={"AAPL"})
        config = _make_config(risk_config=risk)
        broker = PaperTradeBroker(config=config, initial_cash=100_000.0)
        await broker.connect()
        broker.update_market_price("AAPL", 150.0)
        result = await broker.submit_order("AAPL", "buy", 100)
        assert not result.success
        assert "banned" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_allowed_symbols_filter(self):
        risk = PaperTradeRiskConfig(allowed_symbols={"MSFT"})
        config = _make_config(risk_config=risk)
        broker = PaperTradeBroker(config=config, initial_cash=100_000.0)
        await broker.connect()
        broker.update_market_price("AAPL", 150.0)
        result = await broker.submit_order("AAPL", "buy", 100)
        assert not result.success
        assert "allowed" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_max_single_order_value(self):
        risk = PaperTradeRiskConfig(max_single_order_value=1000.0)
        config = _make_config(risk_config=risk)
        broker = PaperTradeBroker(config=config, initial_cash=1_000_000.0)
        await broker.connect()
        broker.update_market_price("AAPL", 150.0)
        result = await broker.submit_order("AAPL", "buy", 100)
        assert not result.success
        assert "value" in result.reason.lower()


class TestFillStats:
    @pytest.mark.asyncio
    async def test_stats_after_fills(self):
        broker = PaperTradeBroker(config=_make_config(), initial_cash=200_000.0)
        await broker.connect()
        broker.update_market_price("AAPL", 150.0)
        await broker.submit_order("AAPL", "buy", 100)
        await broker.submit_order("AAPL", "sell", 100)
        stats = await broker.get_fill_stats()
        assert stats["global"]["total_orders"] == 2
        assert stats["global"]["filled_orders"] == 2
        assert stats["global"]["fill_rate"] == 1.0
        assert "portfolio" in stats
        assert stats["portfolio"]["total_pnl"] != 0.0


class TestOrderHistory:
    @pytest.mark.asyncio
    async def test_order_history_after_cancel(self):
        broker = PaperTradeBroker(config=_make_config(), initial_cash=100_000.0)
        await broker.connect()
        broker.update_market_price("AAPL", 155.0)
        await broker.submit_order("AAPL", "buy", 100, order_type="limit", limit_price=150.0)
        open_orders = await broker.get_open_orders()
        assert len(open_orders) == 1
        await broker.cancel_order(open_orders[0]["order_id"])
        history = await broker.get_order_history()
        assert len(history) == 1
        assert history[0]["status"] == "cancelled"


class TestUpdateMarketPrice:
    @pytest.mark.asyncio
    async def test_price_updates_unrealized_pnl(self):
        broker = PaperTradeBroker(config=_make_config(), initial_cash=100_000.0)
        await broker.connect()
        broker.update_market_price("AAPL", 150.0)
        await broker.submit_order("AAPL", "buy", 100)
        broker.update_market_price("AAPL", 160.0)
        portfolio = await broker.get_portfolio()
        assert portfolio.unrealized_pnl > 0


class TestPartialFills:
    @pytest.mark.asyncio
    async def test_partial_fill_enabled(self):
        risk = PaperTradeRiskConfig(max_single_order_value=1_000_000.0)
        config = _make_config(
            partial_fill_enabled=True,
            partial_fill_min_ratio=0.5,
            random_seed=42,
            risk_config=risk,
        )
        broker = PaperTradeBroker(config=config, initial_cash=10_000_000.0)
        await broker.connect()
        broker.update_market_price("AAPL", 150.0)
        result = await broker.submit_order("AAPL", "buy", 2000)
        assert result.success
        assert result.quantity <= 2000
        assert result.quantity >= 1000


class TestDataProviderIntegration:
    @pytest.mark.asyncio
    async def test_price_refresh_from_provider(self):
        class _Provider:
            async def get_latest_price(self, symbol: str) -> float | None:
                return 155.0

        config = _make_config(refresh_price_from_provider=True)
        broker = PaperTradeBroker(
            config=config,
            initial_cash=100_000.0,
            data_provider=_Provider(),
        )
        await broker.connect()
        order = _FakeOrder()
        result = await broker.execute(order, 150.0, _make_cost())
        assert result.success
