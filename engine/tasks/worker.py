from __future__ import annotations

import traceback
from urllib.parse import urlparse, urlunparse

import structlog
from taskiq import TaskiqScheduler
from taskiq_redis import ListQueueBroker, RedisAsyncResultBackend

from engine.config import settings
from engine.observability.taskiq_middleware import CorrelationMiddleware

logger = structlog.get_logger()

_parsed = urlparse(settings.valkey_url)
_broker_url = urlunparse(_parsed._replace(scheme="redis"))

broker = (
    ListQueueBroker(url=_broker_url)
    .with_result_backend(RedisAsyncResultBackend(redis_url=_broker_url))
    .with_middlewares(CorrelationMiddleware())
)

scheduler = TaskiqScheduler(broker=broker, sources=[])


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
