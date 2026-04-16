"""
Celery tasks — async job processing for backtests and heavy operations.
"""

from celery import Celery
from config import get_settings

settings = get_settings()

celery_app = Celery(
    "nexus",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=3600,  # 1 hour max per task
    task_soft_time_limit=3300,  # Soft limit at 55 min
)


@celery_app.task(bind=True, name="nexus.run_backtest")
def run_backtest_task(self, backtest_config: dict):
    """
    Run a full backtest as an async Celery task.

    Args:
        backtest_config: Dict with strategy_id, symbols, date range, etc.

    Returns:
        Backtest results dict.
    """
    self.update_state(state="RUNNING", meta={"progress": 0})

    # TODO: Implement full backtest loop
    # 1. Load strategy plugin
    # 2. Load historical data
    # 3. Initialize Portfolio + CostModel + BacktestBackend
    # 4. Loop through bars, calling strategy.evaluate()
    # 5. Process signals, record equity curve
    # 6. Calculate metrics
    # 7. Persist to DB

    self.update_state(state="RUNNING", meta={"progress": 100})

    return {
        "status": "completed",
        "config": backtest_config,
        "results": {},
    }


@celery_app.task(name="nexus.refresh_market_data")
def refresh_market_data_task(symbols: list[str]):
    """Refresh market data cache for given symbols."""
    # TODO: Fetch latest data from provider, update cache
    return {"symbols": symbols, "status": "refreshed"}
