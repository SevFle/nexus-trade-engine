from __future__ import annotations

from taskiq import TaskiqScheduler
from taskiq_redis import ListQueueBroker, RedisAsyncResultBackend

from engine.config import settings

_broker_url = settings.valkey_url.replace("valkey://", "redis://", 1)

broker = ListQueueBroker(url=_broker_url).with_result_backend(
    RedisAsyncResultBackend(redis_url=_broker_url)
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
    """Stub — dispatched by POST /api/v1/backtest/run."""
    raise NotImplementedError
