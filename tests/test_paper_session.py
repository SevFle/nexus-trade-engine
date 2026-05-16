"""Tests for paper trading session management, runner, and store."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest

from engine.core.execution.paper import PaperBackend
from engine.core.execution.paper_runner import (
    _ACTIVE_SESSIONS,
    _ACTIVE_TASKS,
    PaperTradeRunner,
    cancel_active_task,
    create_and_start_session,
    get_active_session,
    get_active_sessions,
)
from engine.core.execution.session import (
    PaperSessionConfig,
    PaperSessionState,
    PaperTradeSession,
    SessionStatus,
    create_session_id,
)
from engine.core.execution.session_store import PaperSessionStore
from engine.core.execution.slippage import SlippageModelType
from engine.events.bus import EventType


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


def _make_config(**overrides: Any) -> PaperSessionConfig:
    defaults = {
        "strategy_name": "test-strategy",
        "symbols": ["AAPL"],
        "initial_capital": 100_000.0,
        "interval_seconds": 1,
        "random_seed": 42,
    }
    defaults.update(overrides)
    return PaperSessionConfig(**defaults)


def _make_state(**overrides: Any) -> PaperSessionState:
    config = overrides.pop("config", _make_config())
    defaults = {
        "session_id": create_session_id(),
        "user_id": "user-1",
        "config": config,
    }
    defaults.update(overrides)
    return PaperSessionState(**defaults)


class _FakeDataProvider:
    """In-memory data provider that returns deterministic OHLCV data."""

    def __init__(self, price: float = 150.0, bars: int = 100):
        self._price = price
        self._bars = bars

    async def get_latest_price(self, symbol: str) -> float | None:
        return self._price

    async def get_ohlcv(
        self,
        symbol: str,
        period: str = "1y",
        interval: str = "1d",
    ) -> pd.DataFrame:
        dates = pd.date_range("2024-01-01", periods=self._bars, freq="1D")
        return pd.DataFrame(
            {
                "open": [self._price - 0.5] * self._bars,
                "high": [self._price + 1.0] * self._bars,
                "low": [self._price - 1.0] * self._bars,
                "close": [self._price] * self._bars,
                "volume": [1_000_000] * self._bars,
            },
            index=dates,
        )

    async def get_multiple_prices(self, symbols: list[str]) -> dict[str, float]:
        return dict.fromkeys(symbols, self._price)


class _FakeStrategy:
    name = "test-strategy"
    version = "1.0.0"

    def on_bar(self, state: Any, portfolio: Any) -> list[dict]:
        return [{"symbol": "AAPL", "side": "buy", "weight": 0.1}]


class TestPaperSessionConfig:
    def test_defaults(self):
        config = _make_config()
        assert config.strategy_name == "test-strategy"
        assert config.symbols == ["AAPL"]
        assert config.initial_capital == 100_000.0
        assert config.fill_probability == 0.95
        assert config.slippage_model_type == SlippageModelType.FIXED_BPS
        assert config.refresh_price_from_provider is True

    def test_custom_slippage_type(self):
        config = _make_config(
            slippage_model_type=SlippageModelType.PERCENTAGE,
            slippage_model_kwargs={"pct": 0.001},
        )
        assert config.slippage_model_type == SlippageModelType.PERCENTAGE
        assert config.slippage_model_kwargs == {"pct": 0.001}


class TestPaperSessionState:
    def test_creates_with_timestamp(self):
        state = _make_state()
        assert state.session_id
        assert state.user_id == "user-1"
        assert state.status == SessionStatus.PENDING
        assert state.created_at

    def test_to_dict(self):
        state = _make_state()
        d = state.to_dict()
        assert d["session_id"] == state.session_id
        assert d["user_id"] == "user-1"
        assert d["status"] == "pending"
        assert d["strategy_name"] == "test-strategy"
        assert d["symbols"] == ["AAPL"]
        assert d["initial_capital"] == 100_000.0


class TestPaperTradeSession:
    def test_create_backend(self):
        state = _make_state()
        session = PaperTradeSession(state=state)
        backend = session.create_backend()
        assert isinstance(backend, PaperBackend)
        assert session.backend is backend

    def test_record_trade_filled(self):
        state = _make_state()
        session = PaperTradeSession(state=state)
        session.record_trade({
            "status": "filled",
            "quantity": 10,
            "fill_price": 150.0,
        })
        assert session.state.total_trades == 1
        assert session.state.total_fills == 1
        assert session.state.total_notional == 1500.0

    def test_record_trade_rejected(self):
        state = _make_state()
        session = PaperTradeSession(state=state)
        session.record_trade({"status": "rejected"})
        assert session.state.total_trades == 1
        assert session.state.total_rejections == 1

    def test_get_trades_pagination(self):
        state = _make_state()
        session = PaperTradeSession(state=state)
        for i in range(5):
            session.record_trade({"status": "filled", "idx": i})
        trades = session.get_trades(limit=2, offset=1)
        assert len(trades) == 2
        assert trades[0]["idx"] == 1

    def test_record_equity(self):
        state = _make_state()
        session = PaperTradeSession(state=state)
        session.record_equity({"total_value": 100_000, "cash": 50_000})
        session.record_equity({"total_value": 101_000, "cash": 51_000})
        curve = session.get_equity_curve()
        assert len(curve) == 2
        assert curve[1]["total_value"] == 101_000

    def test_get_fill_stats_no_backend(self):
        state = _make_state()
        session = PaperTradeSession(state=state)
        assert session.get_fill_stats() == {}

    def test_get_fill_stats_with_backend(self):
        state = _make_state()
        session = PaperTradeSession(state=state)
        session.create_backend()
        stats = session.get_fill_stats()
        assert "global" in stats
        assert "per_symbol" in stats

    def test_mark_started(self):
        state = _make_state()
        session = PaperTradeSession(state=state)
        session.mark_started()
        assert session.state.status == SessionStatus.RUNNING
        assert session.state.started_at is not None

    def test_mark_stopped(self):
        state = _make_state()
        session = PaperTradeSession(state=state)
        session.mark_stopped()
        assert session.state.status == SessionStatus.STOPPED
        assert session.state.stopped_at is not None

    def test_mark_stopped_with_error(self):
        state = _make_state()
        session = PaperTradeSession(state=state)
        session.mark_stopped(error="something broke")
        assert session.state.status == SessionStatus.FAILED
        assert session.state.error == "something broke"


class TestCreateSessionId:
    def test_unique(self):
        ids = {create_session_id() for _ in range(100)}
        assert len(ids) == 100

    def test_format(self):
        sid = create_session_id()
        assert len(sid) == 36
        assert sid.count("-") == 4


class TestPaperSessionStore:
    @pytest.mark.asyncio
    async def test_save_and_get_local_fallback(self):
        store = PaperSessionStore()
        data = {"session_id": "abc", "user_id": "u1", "status": "running"}
        await store.save("abc", data)
        result = await store.get("abc")
        assert result is not None
        assert result["session_id"] == "abc"

    @pytest.mark.asyncio
    async def test_get_missing(self):
        store = PaperSessionStore()
        result = await store.get("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_delete(self):
        store = PaperSessionStore()
        await store.save("abc", {"session_id": "abc"})
        await store.delete("abc")
        result = await store.get("abc")
        assert result is None

    @pytest.mark.asyncio
    async def test_list_by_user(self):
        store = PaperSessionStore()
        await store.save("s1", {"session_id": "s1", "user_id": "u1"})
        await store.save("s2", {"session_id": "s2", "user_id": "u2"})
        await store.save("s3", {"session_id": "s3", "user_id": "u1"})
        results = await store.list_by_user("u1")
        assert len(results) == 2
        assert {r["session_id"] for r in results} == {"s1", "s3"}

    @pytest.mark.asyncio
    async def test_list_by_user_empty(self):
        store = PaperSessionStore()
        results = await store.list_by_user("u99")
        assert results == []

    @pytest.mark.asyncio
    async def test_evict_expired(self):
        store = PaperSessionStore()
        await store.save("old", {"session_id": "old"})
        store._local_fallback["old"]["_updated_at"] = 0
        await store.evict_expired()
        result = await store.get("old")
        assert result is None


class TestPaperTradeRunner:
    def setup_method(self):
        _ACTIVE_SESSIONS.clear()
        _ACTIVE_TASKS.clear()

    @pytest.mark.asyncio
    async def test_start_and_stop(self):
        config = _make_config(interval_seconds=300)
        state = _make_state(config=config)
        session = PaperTradeSession(state=state, data_provider=_FakeDataProvider())
        strategy = _FakeStrategy()
        provider = _FakeDataProvider()

        runner = PaperTradeRunner(session=session, strategy=strategy, provider=provider)
        task = await runner.start()

        assert session.state.status == SessionStatus.RUNNING
        assert get_active_session(session.state.session_id) is session
        assert not task.done()

        await runner.stop()
        assert session.state.status == SessionStatus.STOPPED
        assert get_active_session(session.state.session_id) is None
        assert task.done()

    @pytest.mark.asyncio
    async def test_creates_backend_and_portfolio(self):
        config = _make_config(interval_seconds=300)
        state = _make_state(config=config)
        session = PaperTradeSession(state=state)
        strategy = _FakeStrategy()
        provider = _FakeDataProvider()

        runner = PaperTradeRunner(session=session, strategy=strategy, provider=provider)
        await runner.start()

        assert session.backend is not None
        assert isinstance(session.backend, PaperBackend)
        assert session.portfolio is not None
        assert session.portfolio.initial_cash == 100_000.0
        assert session.order_manager is not None

        await runner.stop()

    @pytest.mark.asyncio
    async def test_records_equity_on_tick(self):
        config = _make_config(interval_seconds=300)
        state = _make_state(config=config)
        session = PaperTradeSession(state=state)
        strategy = _FakeStrategy()
        provider = _FakeDataProvider(price=150.0)

        runner = PaperTradeRunner(session=session, strategy=strategy, provider=provider)
        await runner.start()

        await asyncio.sleep(0.5)
        await runner.stop()

        curve = session.get_equity_curve()
        assert len(curve) >= 1
        assert curve[0]["total_value"] > 0

    @pytest.mark.asyncio
    async def test_persists_state_to_store(self):
        config = _make_config(interval_seconds=300)
        state = _make_state(config=config)
        session = PaperTradeSession(state=state)
        strategy = _FakeStrategy()
        provider = _FakeDataProvider()

        store = PaperSessionStore()
        runner = PaperTradeRunner(
            session=session, strategy=strategy, provider=provider, store=store
        )
        await runner.start()
        await runner.stop()

        saved = await store.get(session.state.session_id)
        assert saved is not None
        assert saved["status"] == "stopped"

    @pytest.mark.asyncio
    async def test_strategy_params_applied(self):
        strategy = _FakeStrategy()
        strategy.my_param = "default"

        config = _make_config(
            interval_seconds=300,
            strategy_params={"my_param": "custom"},
        )
        state = _make_state(config=config)
        session = PaperTradeSession(state=state)

        runner = PaperTradeRunner(session=session, strategy=strategy, provider=_FakeDataProvider())
        await runner.start()

        assert strategy.my_param == "custom"

        await runner.stop()


class TestActiveSessionRegistry:
    def setup_method(self):
        _ACTIVE_SESSIONS.clear()
        _ACTIVE_TASKS.clear()

    @pytest.mark.asyncio
    async def test_get_active_sessions(self):
        config = _make_config(interval_seconds=300)
        state = _make_state(config=config)
        session = PaperTradeSession(state=state)

        runner = PaperTradeRunner(
            session=session,
            strategy=_FakeStrategy(),
            provider=_FakeDataProvider(),
        )
        await runner.start()

        active = get_active_sessions()
        assert session.state.session_id in active

        await runner.stop()

    @pytest.mark.asyncio
    async def test_cancel_active_task(self):
        config = _make_config(interval_seconds=300)
        state = _make_state(config=config)
        session = PaperTradeSession(state=state)

        runner = PaperTradeRunner(
            session=session,
            strategy=_FakeStrategy(),
            provider=_FakeDataProvider(),
        )
        await runner.start()

        assert cancel_active_task(session.state.session_id) is True

        await asyncio.sleep(0.1)

    def test_cancel_nonexistent_task(self):
        assert cancel_active_task("nonexistent") is False


class TestCreateAndStartSession:
    def setup_method(self):
        _ACTIVE_SESSIONS.clear()
        _ACTIVE_TASKS.clear()

    @pytest.mark.asyncio
    async def test_creates_and_starts(self):
        config = _make_config(interval_seconds=300)
        strategy = _FakeStrategy()
        provider = _FakeDataProvider()

        runner = await create_and_start_session(
            user_id="user-1",
            config=config,
            strategy=strategy,
            provider=provider,
        )

        assert runner.session.state.status == SessionStatus.RUNNING
        assert runner.session.state.user_id == "user-1"

        await runner.stop()


class TestPaperEventTypes:
    def test_paper_session_events_exist(self):
        assert EventType.PAPER_SESSION_STARTED == "paper.session.started"
        assert EventType.PAPER_SESSION_STOPPED == "paper.session.stopped"
        assert EventType.PAPER_SESSION_TICK == "paper.session.tick"
        assert EventType.PAPER_SESSION_FAILED == "paper.session.failed"


class TestPaperBackendIntegration:
    @pytest.mark.asyncio
    async def test_backend_connects_and_executes_in_session(self):
        state = _make_state()
        session = PaperTradeSession(state=state)
        backend = session.create_backend()

        await backend.connect()
        assert backend.connected

        order = _FakeOrder()
        result = await backend.execute(order, 150.0, _make_cost(10.0))
        assert result.success is True
        assert result.quantity > 0
        assert result.price > 0

        await backend.disconnect()
        assert not backend.connected

    @pytest.mark.asyncio
    async def test_fill_stats_after_execution(self):
        state = _make_state()
        session = PaperTradeSession(state=state)
        backend = session.create_backend()
        await backend.connect()

        for _ in range(5):
            await backend.execute(_FakeOrder(), 100.0, _make_cost(5.0))

        stats = session.get_fill_stats()
        assert stats["global"]["total_orders"] == 5
        assert stats["global"]["fill_rate"] > 0

        await backend.disconnect()

    @pytest.mark.asyncio
    async def test_per_symbol_stats(self):
        state = _make_state(config=_make_config(symbols=["AAPL", "MSFT"]))
        session = PaperTradeSession(state=state)
        backend = session.create_backend()
        await backend.connect()

        await backend.execute(
            _FakeOrder(symbol="AAPL"), 100.0, _make_cost(5.0)
        )
        await backend.execute(
            _FakeOrder(symbol="MSFT"), 200.0, _make_cost(5.0)
        )

        stats = session.get_fill_stats()
        assert "AAPL" in stats["per_symbol"]
        assert "MSFT" in stats["per_symbol"]

        await backend.disconnect()


class TestSessionStatusTransitions:
    def test_pending_to_running_to_stopped(self):
        state = _make_state()
        session = PaperTradeSession(state=state)
        assert session.state.status == SessionStatus.PENDING

        session.mark_started()
        assert session.state.status == SessionStatus.RUNNING

        session.mark_stopped()
        assert session.state.status == SessionStatus.STOPPED

    def test_pending_to_running_to_failed(self):
        state = _make_state()
        session = PaperTradeSession(state=state)
        session.mark_started()
        session.mark_stopped(error="market data unavailable")
        assert session.state.status == SessionStatus.FAILED
        assert session.state.error == "market data unavailable"
