"""Taskiq task definitions for backtest execution and strategy evaluation.

This module defines the async tasks that run on the Taskiq worker and
registers them on the shared broker from :mod:`engine.tasks.worker`.
Reusing that single broker means every task defined here inherits the
``CorrelationMiddleware`` (which threads correlation/request/span ids
across the producer→consumer boundary via message labels) and the Redis
async result backend, without opening a second connection pool.

Design notes
------------
* **Lifecycle hooks** — ``WORKER_STARTUP`` / ``WORKER_SHUTDOWN`` handlers
  run exactly once per worker process to log the registered task surface
  and tear down shared resources.
* **Retries** — transient infra failures (connection resets, timeouts) are
  retried with exponential backoff + full jitter via :func:`with_retry`.
  Permanent errors (unknown strategy, bad config) raise non-retryable
  exceptions and fail fast rather than burning retries.
* **Correlation ids** — every task calls :func:`ensure_correlation_id` so a
  log line is traceable end-to-end even when the task is invoked locally
  via ``task(...)`` instead of being enqueued with ``task.kiq(...)``.
* **Error envelope** — failures are returned as structured dicts
  (``status == "failed"``) so the result is always JSON-serialisable for
  the result backend, matching the contract of ``run_backtest_task`` in
  ``worker.py``.  The retried helper re-raises :class:`TaskExecutionError`
  once retries are exhausted; the public task catches it and wraps it.
"""

from __future__ import annotations

import asyncio
import random
import traceback
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, TypeVar

import structlog
from taskiq import TaskiqEvents

from engine.observability.context import (
    ensure_correlation_id,
    get_correlation_id,
)
from engine.tasks.worker import broker

logger = structlog.get_logger()

# Indirection point so tests can swap backoff sleeps for instant no-ops
# without monkeypatching the global ``asyncio.sleep``. Kept as a module
# attribute rather than baked into ``with_retry`` so production timing is
# still governed by the real event-loop sleeper.
_retry_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

_F = TypeVar("_F", bound=Callable[..., Awaitable[Any]])

# Exception types that represent transient infra hiccups rather than bugs or
# invalid input. ``OSError`` is intentionally excluded on its own because it
# also covers non-transient filesystem errors; its concrete transient
# subclasses are listed explicitly instead.
_DEFAULT_RETRYABLE: tuple[type[BaseException], ...] = (
    ConnectionError,
    TimeoutError,
    asyncio.TimeoutError,
)


class TaskExecutionError(RuntimeError):
    """Raised when retryable work exhausts its retries.

    The original exception is attached as ``__cause__`` so callers can
    inspect the underlying failure. Kept as a distinct type so worker
    middleware can tell task-level exhaustion apart from unrelated errors.
    """


def with_retry(
    *,
    max_retries: int = 3,
    base_delay: float = 0.2,
    max_delay: float = 5.0,
    retryable: tuple[type[BaseException], ...] = _DEFAULT_RETRYABLE,
) -> Callable[[_F], _F]:
    """Retry a transient async failure with exponential backoff + jitter.

    Only the configured ``retryable`` exception types are retried; anything
    else (e.g. ``ValueError`` for an unknown strategy) propagates on the
    first attempt so permanent mistakes fail fast. Backoff uses full jitter
    (``delay = random(0, base * 2**(attempt-1))``) to avoid thundering-herd
    retries against the data provider when many workers recover together.

    :param max_retries: number of *additional* attempts after the first.
    :param base_delay:  seconds to wait before the first retry.
    :param max_delay:   cap on any single backoff sleep.
    :param retryable:   exception types eligible for retry.
    """

    def decorator(func: _F) -> _F:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            attempt = 0
            # +1 for the initial try, +max_retries for the retries.
            last_exc: BaseException | None = None
            while attempt <= max_retries:
                attempt += 1
                try:
                    return await func(*args, **kwargs)
                except retryable as exc:
                    last_exc = exc
                    if attempt > max_retries:
                        logger.warning(
                            "task.retry_exhausted",
                            func=getattr(func, "__name__", "callable"),
                            attempts=attempt,
                            error=str(exc),
                            error_type=type(exc).__name__,
                            correlation_id=get_correlation_id(),
                        )
                        break
                    ceiling = min(max_delay, base_delay * (2 ** (attempt - 1)))
                    delay = ceiling * random.random()  # noqa: S311 - full-jitter backoff delay, non-cryptographic
                    logger.info(
                        "task.retry_scheduled",
                        func=getattr(func, "__name__", "callable"),
                        attempt=attempt,
                        next_attempt=attempt + 1,
                        delay=round(delay, 3),
                        error=str(exc),
                        correlation_id=get_correlation_id(),
                    )
                    await _retry_sleep(delay)
            # Unreachable in practice (loop returns or breaks), but keeps
            # the type-checker happy and makes the intent explicit.
            raise TaskExecutionError(
                f"{getattr(func, '__name__', 'callable')} failed after "
                f"{attempt} attempts: {last_exc}"
            ) from last_exc

        return wrapper  # type: ignore[return-value]

    return decorator


# --------------------------------------------------------------------------- #
# Lifecycle hooks
# --------------------------------------------------------------------------- #
@broker.on_event(TaskiqEvents.WORKER_STARTUP)
async def on_worker_startup(_state: Any) -> None:
    """Run once when the worker process starts.

    Binds a correlation id so the startup log line is greppable, and logs
    the full set of registered task names so operators can confirm the
    worker picked up every task module it was pointed at.
    """
    ensure_correlation_id()
    try:
        known = sorted(broker.get_all_tasks().keys())
    except Exception:  # pragma: no cover - defensive: broker impl detail
        known = []
    logger.info(
        "tasks.worker.startup",
        correlation_id=get_correlation_id(),
        registered_tasks=known,
        task_count=len(known),
    )


@broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
async def on_worker_shutdown(_state: Any) -> None:
    """Run once when the worker process shuts down.

    Shared resources created per-process (connection pools, plugin caches)
    would be disposed here. Today the worker relies on GC + broker
    teardown, but the hook guarantees an ordered shutdown log line and a
    stable extension point.
    """
    ensure_correlation_id()
    logger.info(
        "tasks.worker.shutdown",
        correlation_id=get_correlation_id(),
    )


# --------------------------------------------------------------------------- #
# Retryable engine work (lazy imports to avoid heavy/circular imports at
# module load time and to keep worker startup cheap).
# --------------------------------------------------------------------------- #
@with_retry(max_retries=3, base_delay=0.1, max_delay=2.0)
async def _execute_backtest(
    *,
    strategy_name: str,
    symbol: str,
    start_date: str,
    end_date: str,
    initial_capital: float,
) -> Any:
    """Resolve the strategy, build the runner, and execute the backtest.

    Wrapped with :func:`with_retry` so transient data-provider failures are
    retried; an unknown strategy raises ``ValueError`` which is *not*
    retryable and surfaces immediately.
    """
    from engine.core.backtest_runner import BacktestConfig, BacktestRunner
    from engine.data.feeds import get_data_provider
    from engine.plugins.registry import PluginRegistry

    provider = get_data_provider("yahoo")
    config = BacktestConfig(
        strategy_name=strategy_name,
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
        initial_capital=initial_capital,
    )
    registry = PluginRegistry()
    strategy = registry.load_strategy(strategy_name)
    if strategy is None:
        raise ValueError(f"Strategy not found: {strategy_name}")
    runner = BacktestRunner(config=config, strategy=strategy, provider=provider)
    return await runner.run()


@with_retry(max_retries=2, base_delay=0.1, max_delay=1.0)
async def _evaluate_strategy(*, strategy: Any, market: Any, portfolio: Any, costs: Any) -> Any:
    """Call ``strategy.evaluate`` with retry on transient errors."""
    return await strategy.evaluate(portfolio, market, costs)


# --------------------------------------------------------------------------- #
# Public tasks
# --------------------------------------------------------------------------- #
@broker.task
async def run_backtest(
    strategy_name: str,
    symbol: str,
    start_date: str,
    end_date: str,
    initial_capital: float = 100_000.0,
) -> dict[str, Any]:
    """Execute a full backtest as a distributed task.

    Returns a structured dict. ``status`` is ``"completed"`` on success or
    ``"failed"`` (with ``error``/``error_type``) otherwise. The dict is
    JSON-serialisable so it round-trips through the Redis result backend.
    """
    ensure_correlation_id()
    cid = get_correlation_id()
    logger.info(
        "run_backtest.start",
        strategy=strategy_name,
        symbol=symbol,
        start=start_date,
        end=end_date,
        initial_capital=initial_capital,
        correlation_id=cid,
    )
    try:
        result = await _execute_backtest(
            strategy_name=strategy_name,
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            initial_capital=initial_capital,
        )
    except Exception as exc:
        logger.exception(
            "run_backtest.failed",
            strategy=strategy_name,
            symbol=symbol,
            error=str(exc),
            error_type=type(exc).__name__,
            correlation_id=cid,
            traceback=traceback.format_exc(),
        )
        return {
            "status": "failed",
            "strategy_name": strategy_name,
            "symbol": symbol,
            "error": str(exc),
            "error_type": type(exc).__name__,
            "correlation_id": cid,
        }

    payload: dict[str, Any] = {
        "status": "completed",
        "strategy_name": strategy_name,
        "symbol": symbol,
        "start_date": start_date,
        "end_date": end_date,
        "total_trades": len(result.trades),
        "total_return_pct": round(result.total_return_pct, 4),
        "final_capital": round(result.final_capital, 2),
        "metrics": result.metrics,
        "correlation_id": cid,
    }
    logger.info(
        "run_backtest.complete",
        strategy=strategy_name,
        symbol=symbol,
        total_trades=payload["total_trades"],
        total_return_pct=payload["total_return_pct"],
        final_capital=payload["final_capital"],
        correlation_id=cid,
    )
    return payload


@broker.task
async def run_strategy_evaluation(
    strategy_name: str,
    market_state: dict[str, Any] | None = None,
    portfolio: dict[str, Any] | None = None,
    costs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate a strategy against a market snapshot and return its signals.

    ``market_state`` is a plain dict (JSON-serialisable) that is rebuilt into
    a :class:`nexus_sdk.MarketState`; the emitted signals are serialised
    back to dicts so the result survives the JSON result backend. Unknown
    strategies return a ``failed`` envelope rather than raising.
    """
    ensure_correlation_id()
    cid = get_correlation_id()
    logger.info(
        "run_strategy_evaluation.start",
        strategy=strategy_name,
        correlation_id=cid,
    )
    try:
        from engine.plugins.registry import PluginRegistry
        from nexus_sdk import MarketState

        registry = PluginRegistry()
        strategy = registry.load_strategy(strategy_name)
        if strategy is None:
            raise ValueError(f"Strategy not found: {strategy_name}")

        market = MarketState.model_validate(market_state or {})
        raw_signals = await _evaluate_strategy(
            strategy=strategy,
            market=market,
            portfolio=portfolio,
            costs=costs or {},
        )
        signals = [
            s.model_dump(mode="json") if hasattr(s, "model_dump") else dict(s)
            for s in (raw_signals or [])
        ]
    except Exception as exc:
        logger.exception(
            "run_strategy_evaluation.failed",
            strategy=strategy_name,
            error=str(exc),
            error_type=type(exc).__name__,
            correlation_id=cid,
            traceback=traceback.format_exc(),
        )
        return {
            "status": "failed",
            "strategy_name": strategy_name,
            "error": str(exc),
            "error_type": type(exc).__name__,
            "correlation_id": cid,
        }

    logger.info(
        "run_strategy_evaluation.complete",
        strategy=strategy_name,
        signal_count=len(signals),
        correlation_id=cid,
    )
    return {
        "status": "completed",
        "strategy_name": strategy_name,
        "signals": signals,
        "signal_count": len(signals),
        "correlation_id": cid,
    }


__all__ = [
    "TaskExecutionError",
    "broker",
    "on_worker_shutdown",
    "on_worker_startup",
    "run_backtest",
    "run_strategy_evaluation",
    "with_retry",
]
