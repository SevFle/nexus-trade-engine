from __future__ import annotations

import traceback

import structlog

# The broker (and its scheduler) are constructed in :mod:`engine.tasks.broker`
# (the canonical single source of truth) so the FastAPI app factory can wire
# their ``startup()`` / ``shutdown()`` lifecycle into the app lifespan while
# the worker process registers tasks on the very same broker object. Both
# are re-exported here for backwards compatibility with callers that import
# from ``engine.tasks.worker`` — notably the deprecated ``engine.tasks``
# package facade, which re-exports ``broker`` and ``scheduler`` from here.
#
# The broker-construction primitives (``ListQueueBroker``,
# ``RedisAsyncResultBackend``, ``CorrelationMiddleware`` and
# ``TaskiqScheduler``) are likewise re-exported: tests that want to drive
# ``run_backtest_task`` without opening a real Redis connection patch these
# names *on this module* (``engine.tasks.worker.ListQueueBroker``), so they
# must resolve as attributes here. Keeping them in scope also means the
# deprecated facade keeps the historical ``engine.tasks.worker`` surface
# intact even though the real construction now lives in ``broker.py``.
from engine.tasks.broker import (
    CorrelationMiddleware,
    ListQueueBroker,
    RedisAsyncResultBackend,
    TaskiqScheduler,
    broker,
    broker_url,
    build_broker,
    scheduler,
)

logger = structlog.get_logger()

__all__ = [
    "CorrelationMiddleware",
    "ListQueueBroker",
    "RedisAsyncResultBackend",
    "TaskiqScheduler",
    "broker",
    "broker_url",
    "build_broker",
    "run_backtest_task",
    "scheduler",
]


@broker.task
async def run_backtest_task(
    strategy_name: str,
    symbol: str,
    start_date: str,
    end_date: str,
    initial_capital: float = 100_000.0,
) -> dict:
    """Run a full backtest as an async task with proper error propagation."""
    from engine.core.backtest_runner import BacktestConfig, BacktestRunner
    from engine.data.feeds import get_data_provider

    logger.info(
        "backtest_task.start",
        strategy=strategy_name,
        symbol=symbol,
        start=start_date,
        end=end_date,
    )

    try:
        provider = get_data_provider("yahoo")
        config = BacktestConfig(
            strategy_name=strategy_name,
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            initial_capital=initial_capital,
        )

        from engine.plugins.registry import PluginRegistry

        registry = PluginRegistry()
        strategy = registry.load_strategy(strategy_name)
        if strategy is None:
            raise ValueError(f"Strategy not found: {strategy_name}")

        runner = BacktestRunner(config=config, strategy=strategy, provider=provider)
        result = await runner.run()

        logger.info(
            "backtest_task.complete",
            strategy=strategy_name,
            total_trades=len(result.trades),
            total_return_pct=round(result.total_return_pct, 2),
        )

        return {
            "status": "completed",
            "strategy_name": strategy_name,
            "symbol": symbol,
            "total_trades": len(result.trades),
            "total_return_pct": result.total_return_pct,
            "final_capital": result.final_capital,
            "metrics": result.metrics,
            "equity_curve": result.equity_curve,
            "trades": result.trades,
        }

    except Exception as e:
        logger.exception(
            "backtest_task.failed",
            strategy=strategy_name,
            symbol=symbol,
            error=str(e),
            traceback=traceback.format_exc(),
        )
        return {
            "status": "failed",
            "strategy_name": strategy_name,
            "symbol": symbol,
            "error": str(e),
            "error_type": type(e).__name__,
        }
