"""
Paper trading runner — real-time orchestration engine.

Drives the paper trading loop: fetches live market data at a configurable
interval, evaluates the strategy through the sandbox, processes signals
through the OrderManager with the PaperBackend, and tracks results.

Mirrors BacktestRunner architecture but replaces the historical timeline
with a real-time polling loop.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from engine.core.cost_model import DefaultCostModel, TaxMethod
from engine.core.execution.session import (
    PaperSessionConfig,
    PaperSessionState,
    PaperTradeSession,
    SessionStatus,
    create_session_id,
)
from engine.core.execution.session_store import PaperSessionStore, get_paper_session_store
from engine.core.order_manager import OrderManager
from engine.core.portfolio import Portfolio
from engine.core.risk_engine import RiskEngine
from engine.core.signal import Side
from engine.data.market_state import MarketStateBuilder, ValidationError
from engine.plugins.manifest import StrategyManifest
from engine.plugins.sandbox import StrategySandbox

if TYPE_CHECKING:
    from engine.data.feeds import MarketDataProvider
    from engine.plugins.sdk import BaseStrategy

logger = structlog.get_logger()

_ACTIVE_SESSIONS: dict[str, PaperTradeSession] = {}
_ACTIVE_TASKS: dict[str, asyncio.Task[None]] = {}


class PaperTradeRunner:
    """Orchestrates a real-time paper trading session.

    Usage::

        runner = PaperTradeRunner(session, strategy, provider)
        task = await runner.start()          # non-blocking
        ...
        await runner.stop()                  # graceful shutdown
    """

    def __init__(
        self,
        session: PaperTradeSession,
        strategy: BaseStrategy,
        provider: MarketDataProvider,
        *,
        store: PaperSessionStore | None = None,
        event_bus: Any | None = None,
    ) -> None:
        self._session = session
        self._strategy = strategy
        self._provider = provider
        self._store = store
        self._event_bus = event_bus
        self._builder = MarketStateBuilder(
            min_bars=session.state.config.max_bars_history,
        )
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    @property
    def session(self) -> PaperTradeSession:
        return self._session

    async def start(self) -> asyncio.Task[None]:
        """Wire components and start the evaluation loop as a background task."""
        config = self._session.state.config

        self._session.strategy = self._strategy
        self._apply_strategy_params()

        portfolio = Portfolio(
            initial_cash=config.initial_capital,
            tax_method=TaxMethod.FIFO,
        )
        self._session.portfolio = portfolio

        cost_model = DefaultCostModel(**config.cost_config)
        risk_engine = RiskEngine()

        backend = self._session.create_backend()
        await backend.connect()

        order_manager = OrderManager(
            cost_model=cost_model,
            risk_engine=risk_engine,
            portfolio=portfolio,
        )
        order_manager.set_execution_backend(backend)
        self._session.order_manager = order_manager

        self._session.mark_started()
        await self._persist_state()

        _ACTIVE_SESSIONS[self._session.state.session_id] = self._session

        self._task = asyncio.create_task(
            self._loop(),
            name=f"paper-{self._session.state.session_id[:8]}",
        )
        _ACTIVE_TASKS[self._session.state.session_id] = self._task

        logger.info(
            "paper_runner.started",
            session_id=self._session.state.session_id,
            strategy=config.strategy_name,
            symbols=config.symbols,
            interval=config.interval_seconds,
        )

        return self._task

    async def stop(self) -> None:
        """Gracefully stop the evaluation loop and disconnect backend."""
        self._stop_event.set()

        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=10.0)
            except TimeoutError:
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task

        if self._session.backend:
            await self._session.backend.disconnect()

        self._session.mark_stopped()
        await self._persist_state()

        _ACTIVE_SESSIONS.pop(self._session.state.session_id, None)
        _ACTIVE_TASKS.pop(self._session.state.session_id, None)

        logger.info(
            "paper_runner.stopped",
            session_id=self._session.state.session_id,
            total_trades=self._session.state.total_trades,
            total_fills=self._session.state.total_fills,
        )

    async def _loop(self) -> None:
        config = self._session.state.config
        interval = config.interval_seconds

        manifest = StrategyManifest(
            id=config.strategy_name,
            name=config.strategy_name,
            version="0.1.0",
        )
        sandbox = StrategySandbox(self._strategy, manifest)

        while not self._stop_event.is_set():
            try:
                await self._tick(sandbox, config.symbols)
            except Exception as exc:
                logger.exception(
                    "paper_runner.tick_error",
                    session_id=self._session.state.session_id,
                    error=str(exc),
                )
                self._session.state.status = SessionStatus.FAILED
                self._session.state.error = str(exc)
                await self._persist_state()
                return

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=interval,
                )
            except TimeoutError:
                continue
            else:
                return

    async def _tick(self, sandbox: StrategySandbox, symbols: list[str]) -> None:
        portfolio = self._session.portfolio
        order_manager = self._session.order_manager

        try:
            market_state = await self._builder.build_for_live(
                self._provider, symbols
            )
        except ValidationError:
            logger.debug(
                "paper_runner.warmup_skip",
                session_id=self._session.state.session_id,
            )
            return

        prices = dict(market_state.prices)
        portfolio.update_prices(prices)

        snapshot = portfolio.snapshot()

        cost_model = order_manager.cost_model
        signals = await sandbox.safe_evaluate(snapshot, market_state, cost_model)

        for signal in signals:
            if signal.side == Side.HOLD:
                continue
            if signal.symbol not in symbols:
                continue

            price = prices.get(signal.symbol, 0)
            volume = market_state.volumes.get(signal.symbol, 0)

            order = await order_manager.process_signal(signal, price, volume)

            trade_record: dict[str, Any] = {
                "timestamp": datetime.now(UTC).isoformat(),
                "symbol": order.symbol,
                "side": order.side.value,
                "quantity": order.quantity,
                "fill_price": order.fill_price,
                "fill_quantity": order.fill_quantity,
                "status": order.status.value,
                "order_id": order.id,
            }

            if order.status.value == "filled" and order.side == Side.SELL:
                pos = portfolio.positions.get(order.symbol)
                avg_cost = pos.avg_cost if pos else 0.0
                realized_pnl = (order.fill_price - avg_cost) * order.fill_quantity
                costs = order.cost_breakdown or {}
                total_costs = costs.get("total", 0.0)
                realized_pnl -= total_costs
                trade_record["realized_pnl"] = realized_pnl
            else:
                trade_record["realized_pnl"] = 0.0

            self._session.record_trade(trade_record)

        equity_point: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "total_value": portfolio.total_value,
            "cash": portfolio.cash,
        }
        self._session.record_equity(equity_point)

        await self._persist_state()

    async def _persist_state(self) -> None:
        if self._store is None:
            return
        try:
            await self._store.save(
                self._session.state.session_id,
                self._session.state.to_dict(),
            )
        except Exception:
            logger.exception(
                "paper_runner.persist_failed",
                session_id=self._session.state.session_id,
            )

    def _apply_strategy_params(self) -> None:
        params = self._session.state.config.strategy_params
        if not params or self._strategy is None:
            return
        _sentinel = object()
        for key, value in params.items():
            existing = getattr(self._strategy, key, _sentinel)
            if existing is _sentinel or callable(existing):
                continue
            setattr(self._strategy, key, value)


def get_active_session(session_id: str) -> PaperTradeSession | None:
    return _ACTIVE_SESSIONS.get(session_id)


def get_active_sessions() -> dict[str, PaperTradeSession]:
    return dict(_ACTIVE_SESSIONS)


def cancel_active_task(session_id: str) -> bool:
    task = _ACTIVE_TASKS.get(session_id)
    if task and not task.done():
        task.cancel()
        return True
    return False


async def create_and_start_session(
    user_id: str,
    config: PaperSessionConfig,
    strategy: BaseStrategy,
    provider: MarketDataProvider,
    *,
    store: PaperSessionStore | None = None,
    event_bus: Any | None = None,
) -> PaperTradeRunner:
    """Convenience factory: create session + runner and start the loop."""
    session_id = create_session_id()
    state = PaperSessionState(
        session_id=session_id,
        user_id=user_id,
        config=config,
    )
    session = PaperTradeSession(state=state, data_provider=provider)
    store = store or await get_paper_session_store()

    runner = PaperTradeRunner(
        session=session,
        strategy=strategy,
        provider=provider,
        store=store,
        event_bus=event_bus,
    )
    await runner.start()
    return runner
