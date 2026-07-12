"""Paper-trade runner — live ticks → strategy.evaluate(cost_model) → OrderManager → paper broker.

This is the live-paper-trade counterpart of
:class:`engine.core.backtest_runner.BacktestRunner`. Where the backtest
runner replays historical bars, the :class:`PaperTradeRunner` consumes a
streaming :class:`DataFeed` of live :class:`~engine.data.market_state.MarketState`
ticks and drives the same ``Signal → OrderManager → execution-backend``
pipeline — but against a paper (simulated) broker so no real money is at
risk.

Spec differentiator #1 (cost-model injection)
----------------------------------------------
The cost model is passed into *every* ``strategy.evaluate()`` call so
strategies can make cost-aware decisions against *current* live costs in
real time. The backtest runner injects the cost model too, but the paper
runner is what ties live data + live cost awareness together end-to-end.

Component wiring
----------------
- ``strategy``   : anything exposing ``async evaluate(portfolio, market, costs)``
                   (the :class:`~nexus_sdk.strategy.IStrategy` contract). Engine
                   ``BaseStrategy`` plugins that only implement ``on_bar`` are
                   supported via a graceful fallback.
- ``data_feed``  : an async iterable of ``MarketState`` ticks (see :class:`DataFeed`).
- ``cost_model`` : the :class:`~engine.core.cost_model.ICostModel` injected into
                   evaluate(). Should be the *same* instance held by the
                   ``OrderManager`` so costing is consistent.
- ``paper_broker``: an :class:`~engine.core.execution.base.ExecutionBackend`
                   (typically :class:`~engine.core.execution.paper.PaperExecutionBackend`).
                   Connected/disconnected by the runner and installed as the
                   OrderManager's execution backend.
- ``order_manager``: an :class:`~engine.core.order_manager.OrderManager` wired
                   with cost_model + risk_engine + portfolio.

Lifecycle
---------
``start()`` connects the broker and arms the runner; ``process_tick()`` /
``process_feed()`` drive the loop; ``stop()`` disconnects and reports
final stats. ``run()`` is a convenience that does start → drain feed →
stop and returns the run statistics.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import structlog

from engine.core.order_manager import Order, OrderManager, OrderStatus
from engine.core.signal import Side, Signal

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from engine.core.cost_model import ICostModel
    from engine.core.execution.base import ExecutionBackend
    from engine.core.portfolio import Portfolio
    from engine.data.market_state import MarketState

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Protocols (duck-typed contracts — never enforced via isinstance)
# ---------------------------------------------------------------------------


@runtime_checkable
class StrategyLike(Protocol):
    """Minimal strategy contract the runner needs.

    Implements the :class:`~nexus_sdk.strategy.IStrategy` shape:
    ``async evaluate(portfolio, market, costs) -> list[Signal]``. Engine
    ``BaseStrategy`` plugins (which expose ``on_bar`` instead) are also
    accepted — see :meth:`PaperTradeRunner._call_strategy`.
    """

    async def evaluate(self, portfolio: Any, market: Any, costs: Any) -> list[Signal]: ...


@runtime_checkable
class DataFeed(Protocol):
    """Streaming source of :class:`~engine.data.market_state.MarketState` ticks."""

    def ticks(self) -> AsyncIterator[MarketState]: ...


# ---------------------------------------------------------------------------
# Config & stats
# ---------------------------------------------------------------------------


@dataclass
class PaperTradeConfig:
    """Tunable settings for a paper-trading run."""

    #: Starting cash for the session portfolio. The runner itself never
    #: creates a portfolio — it always operates on ``order_manager.portfolio``
    #: — so this value is informational/reserved for runners that build an
    #: :class:`OrderManager` (with its :class:`Portfolio`) from config.
    initial_capital: float = 100_000.0
    #: Symbols the feed is expected to publish. Informational only — used
    #: for logging and for validating the watchlist at start.
    watchlist: list[str] = field(default_factory=list)
    #: Hard cap on processed ticks. ``None`` = unbounded (drain the feed).
    #: Useful for bounded test runs and as a runaway safety valve.
    max_ticks: int | None = None
    #: If ``True`` an exception inside evaluate()/process_signal() propagates
    #: and aborts the run. Default ``False`` isolates failures so one bad
    #: tick cannot kill a live paper-trading session.
    stop_on_error: bool = False


@dataclass
class PaperTradeStats:
    """Per-run counters surfaced after :meth:`PaperTradeRunner.stop`."""

    ticks_processed: int = 0
    evaluations: int = 0
    evaluation_errors: int = 0
    signals_emitted: int = 0
    signals_skipped_hold: int = 0
    signals_no_price: int = 0
    signals_routed: int = 0
    orders_filled: int = 0
    orders_rejected: int = 0
    orders_failed: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class PaperTradeRunner:
    """Orchestrates a live paper-trading session.

    Wires a streaming data feed to a strategy's ``evaluate()`` (injecting
    the cost model), then routes every emitted signal through the
    :class:`~engine.core.order_manager.OrderManager` to a paper broker.

    Example::

        runner = PaperTradeRunner(
            strategy=my_strategy,
            data_feed=live_feed,
            cost_model=DefaultCostModel(),
            paper_broker=PaperExecutionBackend(fill_probability=1.0),
            order_manager=OrderManager(
                cost_model=cost_model,
                risk_engine=RiskEngine(),
                portfolio=Portfolio(initial_cash=100_000.0),
            ),
        )
        stats = await runner.run()
    """

    def __init__(
        self,
        *,
        strategy: StrategyLike,
        data_feed: DataFeed,
        cost_model: ICostModel,
        paper_broker: ExecutionBackend,
        order_manager: OrderManager,
        config: PaperTradeConfig | None = None,
    ) -> None:
        if strategy is None:
            raise ValueError("strategy is required")
        if data_feed is None:
            raise ValueError("data_feed is required")
        if cost_model is None:
            raise ValueError("cost_model is required")
        if paper_broker is None:
            raise ValueError("paper_broker is required")
        if order_manager is None:
            raise ValueError("order_manager is required")

        self.strategy = strategy
        self.data_feed = data_feed
        self.cost_model = cost_model
        self.paper_broker = paper_broker
        self.order_manager = order_manager
        self.config = config or PaperTradeConfig()

        # The runner always shares the OrderManager's portfolio so that order
        # fills mutate exactly the same state the runner snapshots and marks
        # to market for the strategy. There is deliberately no separate
        # ``portfolio`` parameter: allowing a divergent portfolio would let
        # fills and strategy decisions drift out of sync.
        self.portfolio: Portfolio = order_manager.portfolio
        if self.portfolio is None:
            # Without a portfolio there is nothing to mutate, snapshot, or mark
            # to market — refuse rather than fail later with an opaque
            # AttributeError on the first tick.
            raise ValueError("portfolio is required")

        self.stats = PaperTradeStats()
        self._running = False
        self._started = False
        self._last_market_state: MarketState | None = None

    # ------------------------------------------------------------------ #
    # Public properties
    # ------------------------------------------------------------------ #

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def strategy_id(self) -> str:
        """Best-effort strategy identifier for log correlation."""
        return (
            getattr(self.strategy, "id", None)
            or getattr(self.strategy, "name", None)
            or "strategy"
        )

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """Connect the paper broker and arm the runner.

        Idempotent w.r.t. broker connect: calling ``start`` twice without an
        intervening ``stop`` raises :class:`RuntimeError` so callers cannot
        accidentally double-wire the OrderManager's execution backend.
        """
        if self._started:
            raise RuntimeError("PaperTradeRunner already started")

        await self.paper_broker.connect()
        try:
            # The runner owns the broker→OrderManager wiring because it is the
            # only component that holds references to both.
            self.order_manager.set_execution_backend(self.paper_broker)
            await self._invoke_hook("on_market_open")
            # Only flag as started once the broker is connected and wired so a
            # failed start leaves the runner cleanly disconnected and retryable
            # (rather than stuck behind the "already started" guard).
            self._started = True
        except Exception:
            # Roll back the broker connection so we never leak a connected
            # session when wiring or the market-open hook blows up.
            await self.paper_broker.disconnect()
            raise

        self._running = True
        logger.info(
            "paper_trade.started",
            strategy=self.strategy_id,
            watchlist=self.config.watchlist,
            max_ticks=self.config.max_ticks,
        )

    async def stop(self) -> PaperTradeStats:
        """Disconnect the paper broker and freeze the run.

        Safe to call when not running (no-op broker disconnect path).
        """
        if not self._started:
            # Nothing to stop; still return the (empty) stats.
            return self.stats

        self._running = False
        await self._invoke_hook("on_market_close")
        await self.paper_broker.disconnect()
        self._started = False

        logger.info("paper_trade.stopped", strategy=self.strategy_id, stats=self.stats.as_dict())
        return self.stats

    async def run(self) -> PaperTradeStats:
        """Convenience lifecycle: start → drain the feed → stop."""
        await self.start()
        try:
            await self.process_feed()
        finally:
            await self.stop()
        return self.stats

    # ------------------------------------------------------------------ #
    # Tick processing
    # ------------------------------------------------------------------ #

    async def process_feed(self) -> None:
        """Drain every tick from the data feed until exhausted or stopped.

        Honours :attr:`PaperTradeConfig.max_ticks` as a hard upper bound so a
        misbehaving feed cannot trap the runner in an infinite loop.
        """
        async for state in self.data_feed.ticks():
            if not self._running:
                break
            await self.process_tick(state)
            if (
                self.config.max_ticks is not None
                and self.stats.ticks_processed >= self.config.max_ticks
            ):
                logger.info(
                    "paper_trade.max_ticks_reached",
                    strategy=self.strategy_id,
                    max_ticks=self.config.max_ticks,
                )
                break

    async def process_tick(self, market_state: MarketState) -> list[Order]:
        """Process a single data tick end-to-end.

        1. Update portfolio mark prices for accurate P&L.
        2. Snapshot the portfolio for the strategy.
        3. Call ``strategy.evaluate(snapshot, market_state, cost_model)``
           — the cost-model injection (spec differentiator #1).
        4. Route every non-HOLD signal through the OrderManager.

        Returns the list of orders produced this tick (one per routed
        signal; ``None`` outcomes for HOLD / no-price are excluded).
        """
        self.stats.ticks_processed += 1
        self._last_market_state = market_state

        # Mark-to-market so the strategy sees current P&L.
        self.portfolio.update_prices(market_state.prices)
        # Stamp the portfolio so any tax lots opened this tick are dated
        # against the tick timestamp rather than wall-clock now().
        self.portfolio.transaction_date = market_state.timestamp

        snapshot = self.portfolio.snapshot()

        signals = await self._evaluate_strategy(snapshot, market_state)
        self.stats.evaluations += 1
        self.stats.signals_emitted += len(signals)

        orders: list[Order] = []
        for signal in signals:
            order = await self._route_signal(signal, market_state)
            if order is not None:
                orders.append(order)
        return orders

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    async def _evaluate_strategy(
        self,
        snapshot: Any,
        market_state: MarketState,
    ) -> list[Signal]:
        """Invoke the strategy, injecting the cost model.

        Supports both the SDK ``evaluate(portfolio, market, costs)`` contract
        and engine ``on_bar(state, portfolio)`` plugins. Any exception from
        the strategy is isolated unless ``stop_on_error`` is set.
        """
        try:
            return await self._call_strategy(snapshot, market_state)
        except Exception:
            self.stats.evaluation_errors += 1
            logger.exception(
                "paper_trade.evaluate_failed",
                strategy=self.strategy_id,
                tick=str(getattr(market_state, "timestamp", None)),
            )
            if self.config.stop_on_error:
                raise
            return []

    async def _call_strategy(
        self,
        snapshot: Any,
        market_state: MarketState,
    ) -> list[Signal]:
        """Dispatch to the strategy's evaluation entry point.

        Preference order:
        1. ``evaluate(portfolio, market, costs)``  ← cost model injected here
        2. ``on_bar(state, portfolio)``            ← engine BaseStrategy fallback
        """
        evaluate = getattr(self.strategy, "evaluate", None)
        if callable(evaluate):
            result = evaluate(snapshot, market_state, self.cost_model)
            if asyncio.iscoroutine(result):
                result = await result
            return list(result)

        on_bar = getattr(self.strategy, "on_bar", None)
        if callable(on_bar):
            result = on_bar(market_state, snapshot)
            if asyncio.iscoroutine(result):
                result = await result
            return list(result)

        raise TypeError(
            f"strategy {self.strategy_id!r} exposes neither evaluate() nor on_bar()"
        )

    async def _route_signal(
        self,
        signal: Signal,
        market_state: MarketState,
    ) -> Order | None:
        """Route a single signal through the OrderManager to the paper broker."""
        if signal.side == Side.HOLD:
            self.stats.signals_skipped_hold += 1
            return None

        price = market_state.prices.get(signal.symbol)
        if price is None or price <= 0:
            self.stats.signals_no_price += 1
            logger.warning(
                "paper_trade.no_price_for_signal",
                strategy=self.strategy_id,
                symbol=signal.symbol,
                side=signal.side.value,
            )
            return None

        try:
            order = await self.order_manager.process_signal(signal, price)
        except Exception:
            logger.exception(
                "paper_trade.route_failed",
                strategy=self.strategy_id,
                symbol=signal.symbol,
                side=signal.side.value,
            )
            self.stats.orders_failed += 1
            if self.config.stop_on_error:
                raise
            return None

        self.stats.signals_routed += 1
        self._record_order_outcome(order)

        # Surface the fill to the strategy if it implements the lifecycle hook.
        if order.status == OrderStatus.FILLED:
            await self._invoke_hook(
                "on_order_fill",
                {
                    "order_id": order.id,
                    "symbol": order.symbol,
                    "side": order.side.value,
                    "quantity": order.fill_quantity,
                    "fill_price": order.fill_price,
                    "cost_breakdown": order.cost_breakdown,
                },
            )

        return order

    def _record_order_outcome(self, order: Order) -> None:
        """Bucket the order's terminal status into run stats."""
        if order.status == OrderStatus.FILLED:
            self.stats.orders_filled += 1
        elif order.status == OrderStatus.PARTIALLY_FILLED:
            # Treat partial fills as a fill variant but don't double count.
            self.stats.orders_filled += 1
        elif order.status in (OrderStatus.REJECTED, OrderStatus.RISK_REJECTED):
            self.stats.orders_rejected += 1
        elif order.status == OrderStatus.FAILED:
            self.stats.orders_failed += 1
        # PENDING / VALIDATED / COSTED / RISK_APPROVED / SUBMITTED are
        # non-terminal for this synchronous pipeline; nothing to bucket.

    async def _invoke_hook(self, name: str, *args: Any) -> None:
        """Call an optional strategy lifecycle hook (sync or async).

        Strategies are not required to implement any of these — missing
        hooks are silently skipped. Hook exceptions are logged but never
        propagated: a buggy ``on_order_fill`` must not kill the trading loop.
        """
        hook = getattr(self.strategy, name, None)
        if not callable(hook):
            return
        try:
            result = hook(*args)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.exception(
                "paper_trade.hook_failed",
                strategy=self.strategy_id,
                hook=name,
            )
