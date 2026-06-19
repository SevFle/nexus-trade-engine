"""Tests for engine.core.execution.live and engine.core.execution.paper backends."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from engine.core.brokers.base import BrokerAuthError
from engine.core.execution.live import LiveBackend
from engine.core.execution.paper import PaperBackend

if TYPE_CHECKING:
    from engine.core.cost_model import CostBreakdown
    from engine.core.execution.base import FillResult
    from engine.core.order_manager import Order


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
    # ------------------------------------------------------------------ init

    def test_init_defaults(self):
        backend = LiveBackend()
        assert backend.broker_name == "alpaca"
        assert backend.api_key == ""
        assert backend.api_secret == ""
        assert backend.base_url == ""
        assert backend._client is None
        assert backend._connected is False
        assert backend._connected_at is None

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
        # Construction never implies connection.
        assert backend._connected is False

    # --------------------------------------------------------------- connect

    @pytest.mark.asyncio
    async def test_connect(self):
        backend = LiveBackend(api_key="key123", api_secret="secret456")
        await backend.connect()
        # The scaffold does not build a real broker client yet.
        assert backend._client is None
        # ... but the connection state flag must reflect a successful handshake.
        assert backend._connected is True
        assert backend._connected_at is not None

    @pytest.mark.asyncio
    async def test_connect_sets_connected_only_after_success(self):
        backend = LiveBackend(api_key="key123", api_secret="secret456")
        assert backend._connected is False
        await backend.connect()
        assert backend._connected is True

    @pytest.mark.asyncio
    async def test_connect_missing_credentials(self):
        backend = LiveBackend()
        with pytest.raises(BrokerAuthError, match="api_key and api_secret"):
            await backend.connect()
        # A failed connect must never leave the backend in a connected state.
        assert backend._connected is False
        assert backend._connected_at is None

    @pytest.mark.asyncio
    async def test_connect_missing_only_api_key(self):
        backend = LiveBackend(api_secret="secret456")
        with pytest.raises(BrokerAuthError):
            await backend.connect()
        assert backend._connected is False

    @pytest.mark.asyncio
    async def test_connect_missing_only_api_secret(self):
        backend = LiveBackend(api_key="key123")
        with pytest.raises(BrokerAuthError):
            await backend.connect()
        assert backend._connected is False

    @pytest.mark.asyncio
    async def test_connect_rejects_empty_string_credentials(self):
        # Boundary: empty-string credentials are falsy and must be rejected.
        for key, secret in [("", "secret"), ("key", ""), ("", "")]:
            backend = LiveBackend(api_key=key, api_secret=secret)
            with pytest.raises(BrokerAuthError):
                await backend.connect()

    @pytest.mark.asyncio
    async def test_connect_error_message_includes_broker_name(self):
        backend = LiveBackend(broker_name="ibkr")
        with pytest.raises(BrokerAuthError, match="ibkr"):
            await backend.connect()

    @pytest.mark.asyncio
    async def test_connect_records_timestamp(self):
        backend = LiveBackend(api_key="key123", api_secret="secret456")
        before = __import__("time").monotonic()
        await backend.connect()
        after = __import__("time").monotonic()
        assert before <= backend._connected_at <= after

    # ------------------------------------------------------------ disconnect

    @pytest.mark.asyncio
    async def test_disconnect(self):
        backend = LiveBackend()
        backend._client = MagicMock()
        await backend.disconnect()
        assert backend._client is None
        assert backend._connected is False
        assert backend._connected_at is None

    @pytest.mark.asyncio
    async def test_disconnect_after_connect_clears_state(self):
        backend = LiveBackend(api_key="key123", api_secret="secret456")
        await backend.connect()
        assert backend._connected is True
        await backend.disconnect()
        assert backend._connected is False
        assert backend._connected_at is None
        assert backend._client is None

    @pytest.mark.asyncio
    async def test_disconnect_when_never_connected_is_safe(self):
        # Idempotent: disconnecting without ever connecting must not raise.
        backend = LiveBackend()
        await backend.disconnect()
        assert backend._connected is False
        assert backend._client is None

    @pytest.mark.asyncio
    async def test_disconnect_is_idempotent(self):
        backend = LiveBackend(api_key="key123", api_secret="secret456")
        await backend.connect()
        await backend.disconnect()
        await backend.disconnect()  # second call is a no-op
        assert backend._connected is False
        assert backend._client is None

    @pytest.mark.asyncio
    async def test_reconnect_after_disconnect(self):
        backend = LiveBackend(api_key="key123", api_secret="secret456")
        await backend.connect()
        await backend.disconnect()
        assert backend._connected is False
        await backend.connect()
        assert backend._connected is True
        assert backend._connected_at is not None

    # --------------------------------------------------------------- execute

    @pytest.mark.asyncio
    async def test_execute_not_connected(self):
        backend = LiveBackend()
        result = await backend.execute(_FakeOrder(), 150.0, _make_cost())
        assert result.success is False
        assert "not connected" in result.reason.lower()
        # Boundary: a non-fill carries no price or quantity.
        assert result.price == 0.0
        assert result.quantity == 0

    @pytest.mark.asyncio
    async def test_execute_not_implemented(self):
        backend = LiveBackend()
        backend._client = AsyncMock()
        result = await backend.execute(_FakeOrder(), 150.0, _make_cost())
        assert result.success is False
        assert "not yet implemented" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_execute_not_implemented_has_zero_fill(self):
        backend = LiveBackend()
        backend._client = AsyncMock()
        result = await backend.execute(_FakeOrder(), 150.0, _make_cost())
        assert result.price == 0.0
        assert result.quantity == 0

    @pytest.mark.asyncio
    async def test_submit_order_scaffold_raises_not_implemented(self):
        # The default broker-submission hook must signal "not implemented".
        backend = LiveBackend()
        with pytest.raises(NotImplementedError, match="not yet implemented"):
            await backend._submit_order(_FakeOrder(), 150.0, _make_cost())

    @pytest.mark.asyncio
    async def test_execute_wraps_broker_exception(self):
        # A subclass overrides the submission hook to raise; execute() must
        # catch it and return a structured failure rather than propagating.
        class _BrokenBroker(LiveBackend):
            async def _submit_order(
                self, order: Order, market_price: float, costs: CostBreakdown
            ) -> FillResult:
                raise RuntimeError("broker down")

        backend = _BrokenBroker()
        backend._client = object()  # truthy so the connect-guard passes
        result = await backend.execute(_FakeOrder(), 150.0, _make_cost())
        assert result.success is False
        assert "broker error" in result.reason.lower()
        assert "broker down" in result.reason.lower()
        assert result.price == 0.0
        assert result.quantity == 0

    @pytest.mark.asyncio
    async def test_execute_broker_exception_preserves_error_text(self):
        class _RejectingBroker(LiveBackend):
            async def _submit_order(
                self, order: Order, market_price: float, costs: CostBreakdown
            ) -> FillResult:
                raise ValueError("insufficient buying power")

        backend = _RejectingBroker()
        backend._client = object()
        result = await backend.execute(_FakeOrder(), 150.0, _make_cost())
        assert result.success is False
        assert "insufficient buying power" in result.reason

    @pytest.mark.asyncio
    async def test_execute_uses_client_guard_not_connected_flag(self):
        # Even when _connected is True, execute() requires a real client;
        # the scaffold therefore surfaces "not connected" until a client exists.
        backend = LiveBackend(api_key="key123", api_secret="secret456")
        await backend.connect()
        assert backend._connected is True
        result = await backend.execute(_FakeOrder(), 150.0, _make_cost())
        assert result.success is False
        assert "not connected" in result.reason.lower()

    # --------------------------------------------------------------- lifecycle

    @pytest.mark.asyncio
    async def test_full_lifecycle(self):
        backend = LiveBackend(api_key="key123", api_secret="secret456")
        assert backend._connected is False

        await backend.connect()
        assert backend._connected is True

        # Without a concrete client, execution is gated.
        result = await backend.execute(_FakeOrder(), 100.0, _make_cost())
        assert result.success is False

        await backend.disconnect()
        assert backend._connected is False


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

    @pytest.mark.asyncio
    async def test_execute_sell_slippage_decreases_price(self):
        # Boundary: slippage moves sells below the effective price.
        backend = PaperBackend()
        await backend.connect()
        order = _FakeOrder(side=_FakeSide.SELL, quantity=100)
        result = await backend.execute(order, 200.0, _make_cost(50.0))
        assert result.success is True
        assert result.price <= 200.0

    @pytest.mark.asyncio
    async def test_execute_rejects_non_positive_market_price(self):
        # Edge case: no valid price available -> structured failure.
        backend = PaperBackend()
        await backend.connect()
        order = _FakeOrder(quantity=100)
        result = await backend.execute(order, 0.0, _make_cost(10.0))
        assert result.success is False
        assert "price" in result.reason.lower()
