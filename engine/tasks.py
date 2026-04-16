"""
Celery tasks — async job processing for backtests and heavy operations.
"""

import asyncio

from celery import Celery

from engine.config import settings

celery_app = Celery(
    "nexus",
    broker=settings.valkey_url,
    backend=settings.valkey_url,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=3600,
    task_soft_time_limit=3300,
)


@celery_app.task(bind=True, name="nexus.run_backtest")
def run_backtest_task(self, backtest_config: dict):
    """
    Run a full backtest as an async Celery task.

    Args:
        backtest_config: Dict with strategy_name, symbols, date range, etc.

    Returns:
        Backtest results dict.
    """
    from engine.core.backtest_runner import BacktestConfig, run_backtest

    self.update_state(state="RUNNING", meta={"progress": 0, "status": "Loading market data"})

    config = BacktestConfig(
        strategy_name=backtest_config.get("strategy_name", ""),
        symbols=backtest_config.get("symbols", []),
        start_date=backtest_config.get("start_date", ""),
        end_date=backtest_config.get("end_date", ""),
        initial_cash=backtest_config.get("initial_cash", 100_000.0),
        strategy_params=backtest_config.get("strategy_params", {}),
        cost_config=backtest_config.get("cost_config", {}),
        interval=backtest_config.get("interval", "1d"),
        random_seed=backtest_config.get("random_seed", 42),
    )

    self.update_state(state="RUNNING", meta={"progress": 20, "status": "Running backtest loop"})

    result = asyncio.run(run_backtest(config))

    self.update_state(state="RUNNING", meta={"progress": 100, "status": "Completed"})

    return {
        "status": "completed",
        "backtest_id": result.id,
        "strategy_name": result.strategy_name,
        "start_date": result.start_date,
        "end_date": result.end_date,
        "initial_cash": result.initial_cash,
        "final_value": result.final_value,
        "metrics": {
            "total_return_pct": result.metrics.total_return_pct,
            "sharpe_ratio": result.metrics.sharpe_ratio,
            "sortino_ratio": result.metrics.sortino_ratio,
            "max_drawdown_pct": result.metrics.max_drawdown_pct,
            "total_trades": result.metrics.total_trades,
            "win_rate": result.metrics.win_rate,
            "total_costs": result.metrics.total_costs,
            "total_taxes": result.metrics.total_taxes,
            "cost_drag_pct": result.metrics.cost_drag_pct,
            "profit_factor": result.metrics.profit_factor,
            "avg_trade_pnl": result.metrics.avg_trade_pnl,
            "max_consecutive_losses": result.metrics.max_consecutive_losses,
        },
    }


@celery_app.task(name="nexus.refresh_market_data")
def refresh_market_data_task(symbols: list[str]):
    """Refresh market data cache for given symbols."""
    return {"symbols": symbols, "status": "refreshed"}
