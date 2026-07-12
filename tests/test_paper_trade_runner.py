"""Unit tests for PaperTradeRunner — live ticks → strategy.evaluate(cost_model)
→ OrderManager → paper broker.

Covers (per the implementation plan):
  * happy-path tick processing
  * cost-model injection verification (spec differentiator #1)
  * multi-signal routing
  * error handling & lifecycle edge cases
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from engine.core.cost_model import DefaultCostModel
from engine.core.execution.base import ExecutionBackend, FillResult
from engine.core.order_manager import OrderManager, OrderStatus
from engine.core.portfolio import Portfolio
from engine.core.risk_engine import RiskEngine
from engine.core.signal import Side, Signal
from engine.data.market_state import MarketState
from engine.paper_trade.runner import (
    PaperTradeConfig,
    PaperTradeRunner,
    PaperTradeStats,
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


def _market_state(prices: dict[str, float], *, timestamp: datetime | None = None) -> MarketState:
    return MarketState(
        timestamp=timestamp or datetime.now(tz=UTC),
        prices=dict(prices),
    )


class _ListDataFeed:
    """Yields a fixed list of MarketState ticks then stops."""

    def __init__(self, states: list[MarketState]) -> None:
        self._states = list(states)
        self.ticks_called = 0

    def ticks(self) -> AsyncIterator[MarketState]:
        self.ticks_called += 1

        async def _gen() -> AsyncIterator[MarketState]:
            for state in self._states:
                yield state

        return _gen()


class _RecordingStrategy:
    """SDK-style strategy: records evaluate() args and returns canned signals."""

    def __init__(self, name: str = "recording") -> None:
        self.id = name
        self.name = name
        self.version = "1.0.0"
        self.evaluate_calls: list[tuple[Any, Any, Any]] = []
        self._responses: list[list[Signal]] = []
        self.fill_events: list[dict] = []
        self.market_open_called = False
        self.market_close_called = False

    def set_responses(self, responses: list[list[Signal]]) -> None:
        self._responses = list(responses)

    async def evaluate(self, portfolio: Any, market: Any, costs: Any) -> list[Signal]:
        self.evaluate_calls.append((portfolio, market, costs))
        if self._responses:
            return self._responses.pop(0)
        return []

    async def on_order_fill(self, fill: dict) -> None:
        self.fill_events.append(fill)

    async def on_market_open(self) -> None:
        self.market_open_called = True

    async def on_market_close(self) -> None:
        self.market_close_called = True


class _OnBarStrategy:
    """Engine BaseStrategy-style plugin exposing only on_bar()."""

    name = "onbar"
    version = "0.1.0"

    def __init__(self) -> None:
        self.calls = 0

    def on_bar(self, state: MarketState, portfolio: Any) -> list[Signal]:
        self.calls += 1
        # Emit a buy each tick for the first symbol with a price.
        for symbol, price in state.prices.items():
            if price > 0:
                return [Signal.buy(symbol=symbol, strategy_id=self.name, quantity=1)]
        return []


class _FakeBackend(ExecutionBackend):
    """Deterministic paper-broker stand-in for controllable tests."""

    def __init__(
        self,
        *,
        success: bool = True,
        price: float = 100.0,
        quantity: int = 10,
    ) -> None:
        self.success = success
        self.price = price
        self.quantity = quantity
        self.connected = False
        self.connect_count = 0
        self.disconnect_count = 0
        self.executed: list[Any] = []

    async def connect(self) -> None:
        self.connected = True
        self.connect_count += 1

    async def disconnect(self) -> None:
        self.connected = False
        self.disconnect_count += 1

    async def execute(self, order: Any, market_price: float, costs: Any) -> FillResult:
        self.executed.append(order)
        if not self.success:
            return FillResult(success=False, reason="forced failure")
        return FillResult(success=True, price=self.price, quantity=self.quantity)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cost_model() -> DefaultCostModel:
    return DefaultCostModel()


@pytest.fixture
def risk_engine() -> RiskEngine:
    # Allow the small test positions through risk checks.
    return RiskEngine(max_position_pct=1.0, max_portfolio_risk_pct=1.0, max_daily_trades=1000)


@pytest.fixture
def portfolio() -> Portfolio:
    return Portfolio(initial_cash=100_000.0)


@pytest.fixture
def paper_broker() -> _FakeBackend:
    return _FakeBackend(success=True, price=150.0, quantity=10)


@pytest.fixture
def order_manager(
    cost_model: DefaultCostModel,
    risk_engine: RiskEngine,
    portfolio: Portfolio,
) -> OrderManager:
    return OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)


# ---------------------------------------------------------------------------
# Construction & validation
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_requires_all_components(self, cost_model, paper_broker, order_manager):
        feed = _ListDataFeed([])
        with pytest.raises(ValueError, match="strategy is required"):
            PaperTradeRunner(
                strategy=None,
                data_feed=feed,
                cost_model=cost_model,
                paper_broker=paper_broker,
                order_manager=order_manager,
            )

    def test_requires_data_feed(self, cost_model, paper_broker, order_manager):
        strategy = _RecordingStrategy()
        with pytest.raises(ValueError, match="data_feed is required"):
            PaperTradeRunner(
                strategy=strategy,
                data_feed=None,
                cost_model=cost_model,
                paper_broker=paper_broker,
                order_manager=order_manager,
            )

    def test_reuses_order_manager_portfolio_by_default(
        self, cost_model, paper_broker, order_manager
    ):
        runner = PaperTradeRunner(
            strategy=_RecordingStrategy(),
            data_feed=_ListDataFeed([]),
            cost_model=cost_model,
            paper_broker=paper_broker,
            order_manager=order_manager,
        )
        assert runner.portfolio is order_manager.portfolio

    def test_explicit_portfolio_kwarg_rejected_uses_order_manager_portfolio(
        self, cost_model, paper_broker, order_manager, portfolio
    ):
        """The runner deliberately does NOT accept a ``portfolio=`` parameter.

        Allowing a divergent portfolio would let order fills (which mutate
        ``order_manager.portfolio``) and strategy decisions drift out of sync.
        The runner must therefore share the OrderManager's portfolio by object
        identity, and construction with a separate ``portfolio=`` kwarg must
        fail loudly with a ``TypeError``.
        """
        other = Portfolio(initial_cash=50.0)

        # 1) A separate portfolio kwarg is no longer part of the contract.
        with pytest.raises(TypeError, match="portfolio"):
            PaperTradeRunner(
                strategy=_RecordingStrategy(),
                data_feed=_ListDataFeed([]),
                cost_model=cost_model,
                paper_broker=paper_broker,
                order_manager=order_manager,
                portfolio=other,
            )

        # 2) Without the kwarg, the runner binds to order_manager.portfolio.
        runner = PaperTradeRunner(
            strategy=_RecordingStrategy(),
            data_feed=_ListDataFeed([]),
            cost_model=cost_model,
            paper_broker=paper_broker,
            order_manager=order_manager,
        )
        # Same object identity — fills and snapshots share one source of truth.
        assert runner.portfolio is order_manager.portfolio
        assert runner.portfolio is not other

        # 3) Mutating the shared Portfolio object is observable through the
        #    runner too — proving they reference the *same* object, not a
        #    copy. This is exactly the invariant the runner relies on so
        #    filled orders mutate the state it marks to market and snapshots
        #    for the strategy.
        original_cash = order_manager.portfolio._cash
        order_manager.portfolio._cash = original_cash + 1.0
        assert runner.portfolio._cash == original_cash + 1.0
        # Sanity: a freshly-built separate portfolio is unaffected.
        assert other._cash != original_cash + 1.0

    def test_raises_when_portfolio_resolves_to_none(
        self, cost_model, risk_engine, paper_broker
    ):
        """If neither an explicit portfolio nor the OrderManager's portfolio is
        wired, construction must fail fast rather than AttributeError-ing on
        the first tick."""
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=None)
        with pytest.raises(ValueError, match="portfolio is required"):
            PaperTradeRunner(
                strategy=_RecordingStrategy(),
                data_feed=_ListDataFeed([]),
                cost_model=cost_model,
                paper_broker=paper_broker,
                order_manager=om,
            )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def test_start_connects_broker_and_wires_backend(
        self,
        cost_model,
        paper_broker,
        order_manager,
    ):
        runner = PaperTradeRunner(
            strategy=_RecordingStrategy(),
            data_feed=_ListDataFeed([]),
            cost_model=cost_model,
            paper_broker=paper_broker,
            order_manager=order_manager,
        )
        assert not runner.is_running

        await runner.start()

        assert runner.is_running
        assert paper_broker.connected
        assert paper_broker.connect_count == 1
        assert order_manager.execution_backend is paper_broker

    async def test_start_calls_market_open_hook(
        self, cost_model, paper_broker, order_manager
    ):
        strategy = _RecordingStrategy()
        runner = PaperTradeRunner(
            strategy=strategy,
            data_feed=_ListDataFeed([]),
            cost_model=cost_model,
            paper_broker=paper_broker,
            order_manager=order_manager,
        )
        await runner.start()
        assert strategy.market_open_called
        await runner.stop()

    async def test_double_start_raises(self, cost_model, paper_broker, order_manager):
        runner = PaperTradeRunner(
            strategy=_RecordingStrategy(),
            data_feed=_ListDataFeed([]),
            cost_model=cost_model,
            paper_broker=paper_broker,
            order_manager=order_manager,
        )
        await runner.start()
        with pytest.raises(RuntimeError, match="already started"):
            await runner.start()
        await runner.stop()

    async def test_start_failure_disconnects_broker(
        self, cost_model, paper_broker, order_manager
    ):
        """A failure after connect() rolls back the broker connection and must
        not leave the runner flagged as started (so a retry is allowed)."""
        runner = PaperTradeRunner(
            strategy=_RecordingStrategy(),
            data_feed=_ListDataFeed([]),
            cost_model=cost_model,
            paper_broker=paper_broker,
            order_manager=order_manager,
        )

        def _boom(_backend) -> None:
            raise RuntimeError("wiring exploded")

        order_manager.set_execution_backend = _boom

        with pytest.raises(RuntimeError, match="wiring exploded"):
            await runner.start()

        # connect() ran, but the post-connect failure rolled it back.
        assert paper_broker.connect_count == 1
        assert paper_broker.disconnect_count == 1
        assert not paper_broker.connected
        assert not runner.is_running

        # The failed start must not trip the "already started" guard, so the
        # caller can retry cleanly once wiring is fixed.
        order_manager.set_execution_backend = lambda backend: setattr(
            order_manager, "execution_backend", backend
        )
        await runner.start()
        assert runner.is_running
        await runner.stop()

    async def test_stop_disconnects_broker(
        self,
        cost_model,
        paper_broker,
        order_manager,
    ):
        strategy = _RecordingStrategy()
        runner = PaperTradeRunner(
            strategy=strategy,
            data_feed=_ListDataFeed([]),
            cost_model=cost_model,
            paper_broker=paper_broker,
            order_manager=order_manager,
        )
        await runner.start()
        await runner.stop()

        assert not runner.is_running
        assert paper_broker.disconnect_count == 1
        assert strategy.market_close_called

    async def test_stop_when_not_started_is_safe(
        self, cost_model, paper_broker, order_manager
    ):
        runner = PaperTradeRunner(
            strategy=_RecordingStrategy(),
            data_feed=_ListDataFeed([]),
            cost_model=cost_model,
            paper_broker=paper_broker,
            order_manager=order_manager,
        )
        stats = await runner.stop()  # should not raise
        assert isinstance(stats, PaperTradeStats)

    async def test_run_drains_feed_and_stops(
        self,
        cost_model,
        paper_broker,
        order_manager,
    ):
        strategy = _RecordingStrategy()
        feed = _ListDataFeed([_market_state({"AAPL": 150.0})])
        runner = PaperTradeRunner(
            strategy=strategy,
            data_feed=feed,
            cost_model=cost_model,
            paper_broker=paper_broker,
            order_manager=order_manager,
        )
        stats = await runner.run()
        assert stats.ticks_processed == 1
        assert paper_broker.disconnect_count == 1
        assert not runner.is_running


# ---------------------------------------------------------------------------
# Happy-path tick processing
# ---------------------------------------------------------------------------


class TestHappyPathTickProcessing:
    async def test_single_buy_signal_fills_and_updates_portfolio(
        self,
        cost_model,
        paper_broker,
        order_manager,
    ):
        strategy = _RecordingStrategy()
        strategy.set_responses(
            [[Signal.buy(symbol="AAPL", strategy_id="recording", quantity=10)]]
        )
        feed = _ListDataFeed([_market_state({"AAPL": 150.0})])
        runner = PaperTradeRunner(
            strategy=strategy,
            data_feed=feed,
            cost_model=cost_model,
            paper_broker=paper_broker,
            order_manager=order_manager,
        )

        await runner.start()
        orders = await runner.process_tick(_market_state({"AAPL": 150.0}))
        await runner.stop()

        assert len(orders) == 1
        order = orders[0]
        assert order.status == OrderStatus.FILLED
        assert order.side == Side.BUY
        assert order.fill_quantity == 10
        assert order.fill_price == 150.0

        # Portfolio mutated by the OrderManager via the paper broker.
        assert "AAPL" in runner.portfolio.positions
        assert runner.portfolio.positions["AAPL"].quantity == 10

        assert runner.stats.ticks_processed == 1
        assert runner.stats.evaluations == 1
        assert runner.stats.signals_emitted == 1
        assert runner.stats.signals_routed == 1
        assert runner.stats.orders_filled == 1

    async def test_buy_then_sell_full_cycle(
        self,
        cost_model,
        risk_engine,
        portfolio,
    ):
        # Use a backend that fills the requested quantity exactly.
        broker = _FakeBackend(success=True, price=150.0, quantity=10)
        om = OrderManager(
            cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio
        )
        strategy = _RecordingStrategy()
        strategy.set_responses(
            [
                [Signal.buy(symbol="AAPL", strategy_id="recording", quantity=10)],
                [Signal.sell(symbol="AAPL", strategy_id="recording", quantity=10)],
            ]
        )
        feed = _ListDataFeed([_market_state({"AAPL": 150.0}), _market_state({"AAPL": 160.0})])
        runner = PaperTradeRunner(
            strategy=strategy,
            data_feed=feed,
            cost_model=cost_model,
            paper_broker=broker,
            order_manager=om,
        )
        await runner.run()

        assert runner.stats.orders_filled == 2
        assert runner.stats.ticks_processed == 2
        # Position fully closed after the sell.
        assert "AAPL" not in runner.portfolio.positions
        assert runner.portfolio.realized_pnl != 0

    async def test_hold_signal_is_skipped_not_routed(
        self,
        cost_model,
        paper_broker,
        order_manager,
    ):
        strategy = _RecordingStrategy()
        strategy.set_responses(
            [[Signal.hold(symbol="AAPL", strategy_id="recording")]]
        )
        feed = _ListDataFeed([_market_state({"AAPL": 150.0})])
        runner = PaperTradeRunner(
            strategy=strategy,
            data_feed=feed,
            cost_model=cost_model,
            paper_broker=paper_broker,
            order_manager=order_manager,
        )
        await runner.run()

        assert runner.stats.signals_skipped_hold == 1
        assert runner.stats.signals_routed == 0
        assert paper_broker.executed == []

    async def test_on_order_fill_hook_fires(
        self,
        cost_model,
        paper_broker,
        order_manager,
    ):
        strategy = _RecordingStrategy()
        strategy.set_responses(
            [[Signal.buy(symbol="AAPL", strategy_id="recording", quantity=10)]]
        )
        feed = _ListDataFeed([_market_state({"AAPL": 150.0})])
        runner = PaperTradeRunner(
            strategy=strategy,
            data_feed=feed,
            cost_model=cost_model,
            paper_broker=paper_broker,
            order_manager=order_manager,
        )
        await runner.run()

        assert len(strategy.fill_events) == 1
        fill = strategy.fill_events[0]
        assert fill["symbol"] == "AAPL"
        assert fill["side"] == "buy"
        assert fill["quantity"] == 10
        assert fill["fill_price"] == 150.0


# ---------------------------------------------------------------------------
# Cost-model injection verification (spec differentiator #1)
# ---------------------------------------------------------------------------


class TestCostModelInjection:
    async def test_evaluate_receives_the_exact_cost_model(
        self,
        cost_model,
        paper_broker,
        order_manager,
    ):
        strategy = _RecordingStrategy()
        feed = _ListDataFeed([_market_state({"AAPL": 150.0})])
        runner = PaperTradeRunner(
            strategy=strategy,
            data_feed=feed,
            cost_model=cost_model,
            paper_broker=paper_broker,
            order_manager=order_manager,
        )
        await runner.run()

        assert len(strategy.evaluate_calls) == 1
        _, _, injected_costs = strategy.evaluate_calls[0]
        # Differentiator #1: the SAME cost_model instance is injected.
        assert injected_costs is cost_model

    async def test_evaluate_receives_portfolio_snapshot_and_market_state(
        self,
        cost_model,
        paper_broker,
        order_manager,
    ):
        strategy = _RecordingStrategy()
        state = _market_state({"AAPL": 150.0}, timestamp=datetime(2024, 6, 1, tzinfo=UTC))
        feed = _ListDataFeed([state])
        runner = PaperTradeRunner(
            strategy=strategy,
            data_feed=feed,
            cost_model=cost_model,
            paper_broker=paper_broker,
            order_manager=order_manager,
        )
        await runner.run()

        snapshot, market, costs = strategy.evaluate_calls[0]
        # Portfolio snapshot contract.
        assert snapshot.cash == 100_000.0
        assert snapshot.total_value == 100_000.0
        # Market state is forwarded unchanged.
        assert market is state
        assert market.prices["AAPL"] == 150.0
        assert costs is cost_model

    async def test_cost_model_injected_every_tick(
        self,
        cost_model,
        paper_broker,
        order_manager,
    ):
        strategy = _RecordingStrategy()
        feed = _ListDataFeed(
            [
                _market_state({"AAPL": 150.0}),
                _market_state({"AAPL": 151.0}),
                _market_state({"AAPL": 149.0}),
            ]
        )
        runner = PaperTradeRunner(
            strategy=strategy,
            data_feed=feed,
            cost_model=cost_model,
            paper_broker=paper_broker,
            order_manager=order_manager,
        )
        await runner.run()

        assert len(strategy.evaluate_calls) == 3
        # Every single tick gets the cost model injected.
        for _, _, injected in strategy.evaluate_calls:
            assert injected is cost_model

    async def test_on_bar_strategy_does_not_crash_runner(
        self,
        cost_model,
        paper_broker,
        order_manager,
    ):
        """Engine BaseStrategy plugins (on_bar only) are supported via fallback."""
        strategy = _OnBarStrategy()
        feed = _ListDataFeed([_market_state({"AAPL": 150.0})])
        runner = PaperTradeRunner(
            strategy=strategy,
            data_feed=feed,
            cost_model=cost_model,
            paper_broker=paper_broker,
            order_manager=order_manager,
        )
        await runner.run()

        assert strategy.calls == 1
        assert runner.stats.orders_filled == 1


# ---------------------------------------------------------------------------
# Multi-signal routing
# ---------------------------------------------------------------------------


class TestMultiSignalRouting:
    async def test_multiple_signals_in_one_tick_all_routed(
        self,
        cost_model,
        risk_engine,
    ):
        portfolio = Portfolio(initial_cash=500_000.0)
        broker = _FakeBackend(success=True, price=100.0, quantity=5)
        om = OrderManager(
            cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio
        )
        strategy = _RecordingStrategy()
        strategy.set_responses(
            [
                [
                    Signal.buy(symbol="AAPL", strategy_id="recording", quantity=5),
                    Signal.buy(symbol="MSFT", strategy_id="recording", quantity=5),
                    Signal.hold(symbol="GOOG", strategy_id="recording"),
                ]
            ]
        )
        feed = _ListDataFeed([_market_state({"AAPL": 100.0, "MSFT": 100.0, "GOOG": 100.0})])
        runner = PaperTradeRunner(
            strategy=strategy,
            data_feed=feed,
            cost_model=cost_model,
            paper_broker=broker,
            order_manager=om,
        )
        await runner.run()

        # Two buys routed + executed, one HOLD skipped.
        assert runner.stats.signals_emitted == 3
        assert runner.stats.signals_skipped_hold == 1
        assert runner.stats.signals_routed == 2
        assert runner.stats.orders_filled == 2
        assert len(broker.executed) == 2
        assert {o.symbol for o in broker.executed} == {"AAPL", "MSFT"}

    async def test_multi_signal_partial_failure_does_not_block_others(
        self,
        cost_model,
        risk_engine,
    ):
        """A sell with no position should be rejected by validation, but the
        accompanying valid buy still fills."""
        portfolio = Portfolio(initial_cash=100_000.0)
        broker = _FakeBackend(success=True, price=100.0, quantity=5)
        om = OrderManager(
            cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio
        )
        strategy = _RecordingStrategy()
        strategy.set_responses(
            [
                [
                    Signal.sell(symbol="MSFT", strategy_id="recording", quantity=5),
                    Signal.buy(symbol="AAPL", strategy_id="recording", quantity=5),
                ]
            ]
        )
        feed = _ListDataFeed([_market_state({"AAPL": 100.0, "MSFT": 100.0})])
        runner = PaperTradeRunner(
            strategy=strategy,
            data_feed=feed,
            cost_model=cost_model,
            paper_broker=broker,
            order_manager=om,
        )
        await runner.run()

        assert runner.stats.signals_routed == 2
        assert runner.stats.orders_filled == 1
        assert runner.stats.orders_rejected == 1
        assert "AAPL" in runner.portfolio.positions

    async def test_signal_for_missing_price_is_skipped(
        self,
        cost_model,
        paper_broker,
        order_manager,
    ):
        strategy = _RecordingStrategy()
        strategy.set_responses(
            [[Signal.buy(symbol="NOPE", strategy_id="recording", quantity=5)]]
        )
        # Feed has AAPL but the signal targets NOPE — no price available.
        feed = _ListDataFeed([_market_state({"AAPL": 100.0})])
        runner = PaperTradeRunner(
            strategy=strategy,
            data_feed=feed,
            cost_model=cost_model,
            paper_broker=paper_broker,
            order_manager=order_manager,
        )
        await runner.run()

        assert runner.stats.signals_no_price == 1
        assert runner.stats.signals_routed == 0
        assert paper_broker.executed == []


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    async def test_strategy_evaluate_exception_isolated(
        self,
        cost_model,
        paper_broker,
        order_manager,
    ):
        class _BoomStrategy(_RecordingStrategy):
            async def evaluate(self, portfolio, market, costs):
                raise RuntimeError("strategy blew up")

        strategy = _BoomStrategy()
        feed = _ListDataFeed(
            [_market_state({"AAPL": 150.0}), _market_state({"AAPL": 151.0})]
        )
        runner = PaperTradeRunner(
            strategy=strategy,
            data_feed=feed,
            cost_model=cost_model,
            paper_broker=paper_broker,
            order_manager=order_manager,
        )
        stats = await runner.run()

        # Both ticks processed; both evaluations failed but neither killed the loop.
        assert stats.ticks_processed == 2
        assert stats.evaluation_errors == 2
        assert stats.signals_routed == 0

    async def test_stop_on_error_propagates_evaluate_failure(
        self,
        cost_model,
        paper_broker,
        order_manager,
    ):
        class _BoomStrategy(_RecordingStrategy):
            async def evaluate(self, portfolio, market, costs):
                raise RuntimeError("strategy blew up")

        strategy = _BoomStrategy()
        feed = _ListDataFeed([_market_state({"AAPL": 150.0})])
        runner = PaperTradeRunner(
            strategy=strategy,
            data_feed=feed,
            cost_model=cost_model,
            paper_broker=paper_broker,
            order_manager=order_manager,
            config=PaperTradeConfig(stop_on_error=True),
        )
        with pytest.raises(RuntimeError, match="strategy blew up"):
            await runner.run()
        # Broker still torn down by run()'s finally clause.
        assert paper_broker.disconnect_count == 1

    async def test_strategy_with_no_entry_point_raises(
        self,
        cost_model,
        paper_broker,
        order_manager,
    ):
        @dataclass
        class _Dumb:
            id: str = "dumb"
            name: str = "dumb"

        feed = _ListDataFeed([_market_state({"AAPL": 150.0})])
        runner = PaperTradeRunner(
            strategy=_Dumb(),  # type: ignore[arg-type]
            data_feed=feed,
            cost_model=cost_model,
            paper_broker=paper_broker,
            order_manager=order_manager,
        )
        await runner.run()

        # Missing evaluate/on_bar surfaces as an isolated evaluation error.
        assert runner.stats.evaluation_errors == 1

    async def test_on_order_fill_hook_failure_is_swallowed(
        self,
        cost_model,
        paper_broker,
        order_manager,
    ):
        class _BadHookStrategy(_RecordingStrategy):
            async def on_order_fill(self, fill: dict) -> None:
                raise RuntimeError("hook blew up")

        strategy = _BadHookStrategy()
        strategy.set_responses(
            [[Signal.buy(symbol="AAPL", strategy_id="recording", quantity=10)]]
        )
        feed = _ListDataFeed([_market_state({"AAPL": 150.0})])
        runner = PaperTradeRunner(
            strategy=strategy,
            data_feed=feed,
            cost_model=cost_model,
            paper_broker=paper_broker,
            order_manager=order_manager,
        )
        # Should not raise despite the broken hook.
        stats = await runner.run()
        assert stats.orders_filled == 1

    async def test_max_ticks_caps_processed_count(
        self,
        cost_model,
        paper_broker,
        order_manager,
    ):
        strategy = _RecordingStrategy()
        feed = _ListDataFeed([_market_state({"AAPL": float(p)}) for p in range(100, 110)])
        runner = PaperTradeRunner(
            strategy=strategy,
            data_feed=feed,
            cost_model=cost_model,
            paper_broker=paper_broker,
            order_manager=order_manager,
            config=PaperTradeConfig(max_ticks=3),
        )
        stats = await runner.run()
        assert stats.ticks_processed == 3


# ---------------------------------------------------------------------------
# Stats contract
# ---------------------------------------------------------------------------


class TestStats:
    def test_as_dict_round_trip(self):
        stats = PaperTradeStats(
            ticks_processed=5,
            evaluations=5,
            signals_emitted=10,
            signals_routed=8,
            orders_filled=7,
        )
        d = stats.as_dict()
        assert d["ticks_processed"] == 5
        assert d["orders_filled"] == 7
        assert set(d.keys()) == {
            "ticks_processed",
            "evaluations",
            "evaluation_errors",
            "signals_emitted",
            "signals_skipped_hold",
            "signals_no_price",
            "signals_routed",
            "orders_filled",
            "orders_rejected",
            "orders_failed",
        }
