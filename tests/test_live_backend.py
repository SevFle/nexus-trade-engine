"""Tests for engine.core.execution.live — LiveBackend scaffold."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from engine.core.execution.base import FillResult
from engine.core.execution.live import LiveBackend


def _make_order(symbol: str = "AAPL", side: str = "buy"):
    from engine.core.oms.order import Order
    from engine.core.oms.states import OrderSide

    return Order(
        symbol=symbol,
        side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
        quantity=Decimal("100"),
    )


class TestLiveBackendInit:
    def test_default_params(self):
        backend = LiveBackend()
        assert backend.broker_name == "alpaca"
        assert backend.api_key == ""
        assert backend.api_secret == ""
        assert backend.base_url == ""
        assert backend._client is None

    def test_custom_params(self):
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


class TestLiveBackendConnect:
    @pytest.mark.asyncio
    async def test_connect_does_not_crash(self):
        backend = LiveBackend()
        await backend.connect()

    @pytest.mark.asyncio
    async def test_connect_does_not_set_client(self):
        backend = LiveBackend()
        await backend.connect()
        assert backend._client is None


class TestLiveBackendDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_sets_client_none(self):
        backend = LiveBackend()
        backend._client = object()
        await backend.disconnect()
        assert backend._client is None

    @pytest.mark.asyncio
    async def test_disconnect_when_already_none(self):
        backend = LiveBackend()
        await backend.disconnect()
        assert backend._client is None


class TestLiveBackendExecute:
    @pytest.mark.asyncio
    async def test_execute_with_no_client_returns_failure(self):
        backend = LiveBackend()
        order = _make_order()
        result = await backend.execute(order, 150.0, None)
        assert isinstance(result, FillResult)
        assert result.success is False
        assert "not connected" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_execute_with_client_returns_not_implemented(self):
        backend = LiveBackend()
        backend._client = object()
        order = _make_order()
        result = await backend.execute(order, 150.0, None)
        assert isinstance(result, FillResult)
        assert result.success is False
        assert "not yet implemented" in result.reason.lower()


class TestLiveBackendInheritance:
    def test_subclass_of_execution_backend(self):
        from engine.core.execution.base import ExecutionBackend

        assert issubclass(LiveBackend, ExecutionBackend)
