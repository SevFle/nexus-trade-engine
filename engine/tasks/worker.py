from __future__ import annotations

import traceback
from typing import Any
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


def _load_strategy(registry: Any, name: str) -> Any:
    s = registry.load_strategy(name)
    if s is None:
        msg = f"Strategy not found: {name}"
        raise ValueError(msg)
    return s


@broker.task
async def run_backtest_task(
    backtest_id: str,
    user_id: str,
    strategy_name: str,
    symbol: str,
    start_date: str,
    end_date: str,
    initial_capital: float = 100_000.0,
    symbols: list[str] | None = None,
    strategy_params: dict | None = None,
    cost_config: dict | None = None,
    interval: str = "1d",
) -> dict:
    """Run a full backtest as an async task, persisting results to Redis."""
    from engine.core.backtest_runner import BacktestConfig, BacktestRunner  # noqa: PLC0415
    from engine.data.feeds import get_data_provider  # noqa: PLC0415
    from engine.tasks.result_store import get_result_store  # noqa: PLC0415

    logger.info(
        "backtest_task.start",
        backtest_id=backtest_id,
        strategy=strategy_name,
        symbol=symbol,
        start=start_date,
        end=end_date,
    )

    store = await get_result_store()

    try:
        provider = get_data_provider("yahoo")
        config = BacktestConfig(
            strategy_name=strategy_name,
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            initial_capital=initial_capital,
            symbols=symbols,
            strategy_params=strategy_params or {},
            cost_config=cost_config or {},
            interval=interval,
        )

        from engine.plugins.registry import PluginRegistry  # noqa: PLC0415

        registry = PluginRegistry()
        strategy = _load_strategy(registry, strategy_name)

        runner = BacktestRunner(config=config, strategy=strategy, provider=provider)
        result = await runner.run()

        await store.set_completed(
            backtest_id=backtest_id,
            user_id=user_id,
            result_data={
                "strategy_name": strategy_name,
                "symbol": symbol,
                "initial_capital": initial_capital,
                "final_value": result.final_capital,
                "metrics": result.metrics,
                "equity_curve": result.equity_curve,
                "trades": result.trades,
            },
        )

        logger.info(
            "backtest_task.complete",
            backtest_id=backtest_id,
            strategy=strategy_name,
            total_trades=len(result.trades),
            total_return_pct=round(result.total_return_pct, 2),
        )

        return {
            "status": "completed",
            "backtest_id": backtest_id,
            "strategy_name": strategy_name,
            "symbol": symbol,
            "total_trades": len(result.trades),
            "total_return_pct": result.total_return_pct,
            "final_capital": result.final_capital,
        }

    except Exception as e:
        logger.exception(
            "backtest_task.failed",
            backtest_id=backtest_id,
            strategy=strategy_name,
            symbol=symbol,
            error=str(e),
            traceback=traceback.format_exc(),
        )
        await store.set_failed(
            backtest_id=backtest_id,
            user_id=user_id,
            strategy_name=strategy_name,
            symbol=symbol,
            error=str(e),
            error_type=type(e).__name__,
        )
        return {
            "status": "failed",
            "backtest_id": backtest_id,
            "strategy_name": strategy_name,
            "symbol": symbol,
            "error": str(e),
            "error_type": type(e).__name__,
        }
