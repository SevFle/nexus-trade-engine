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
import math
import os
import random
import re
import traceback
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import wraps
from typing import Any, TypeVar

import structlog
from taskiq import AsyncTaskiqTask, TaskiqEvents

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

# Wall-clock cap on a single backtest attempt. Read from the environment at
# import time so operators can tune it per deployment (``.env`` sets
# ``NEXUS_BACKTEST_TIMEOUT_SECONDS``); tests monkeypatch this attribute to
# exercise the timeout path deterministically. ``_execute_backtest`` wraps
# each attempt in ``asyncio.wait_for(..., timeout=BACKTEST_TIMEOUT)`` so a
# wedged data-provider call cannot pin a worker slot indefinitely. A breach
# surfaces as ``TimeoutError`` (== ``asyncio.TimeoutError`` on 3.11+), which
# :func:`with_retry` treats as a transient, retryable failure.
BACKTEST_TIMEOUT: float = float(os.environ.get("NEXUS_BACKTEST_TIMEOUT_SECONDS", "300.0"))

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

# C0 + C1 control characters and DEL. Stripped (not rejected) from free-text
# fields like ``strategy_name`` / ``symbol`` so a pasted value with a stray
# tab/newline is normalised rather than rejected outright; an all-control-char
# value still fails the subsequent emptiness check.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")



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
            # ``attempt`` counts how many tries have *completed*. It starts
            # at 0 and is incremented *after* the call so it always reflects
            # finished work. The loop therefore runs the initial try plus
            # ``max_retries`` follow-ups == ``max_retries + 1`` attempts total
            # (1 initial + ``max_retries`` retries).
            attempt = 0
            last_exc: BaseException | None = None
            while attempt <= max_retries:
                try:
                    return await func(*args, **kwargs)
                except retryable as exc:
                    last_exc = exc
                    # No retries left once we have already completed
                    # ``max_retries`` tries (the budget is the initial try
                    # plus this many follow-ups). Log exhaustion and stop;
                    # the post-loop raise wraps the final failure.
                    if attempt >= max_retries:
                        logger.warning(
                            "task.retry_exhausted",
                            func=getattr(func, "__name__", "callable"),
                            attempts=attempt + 1,
                            error=str(exc),
                            error_type=type(exc).__name__,
                            correlation_id=get_correlation_id(),
                        )
                        break
                    ceiling = min(max_delay, base_delay * (2**attempt))
                    delay = ceiling * random.random()  # noqa: S311 - full-jitter backoff delay, non-cryptographic
                    logger.info(
                        "task.retry_scheduled",
                        func=getattr(func, "__name__", "callable"),
                        attempt=attempt + 1,
                        next_attempt=attempt + 2,
                        delay=round(delay, 3),
                        error=str(exc),
                        correlation_id=get_correlation_id(),
                    )
                    await _retry_sleep(delay)
                attempt += 1
            # Reached either by breaking out after exhausting retries, or
            # by ``max_retries < 0`` (defensive; treated as zero retries).
            raise TaskExecutionError(
                f"{getattr(func, '__name__', 'callable')} failed after "
                f"{attempt + 1} attempts: {last_exc}"
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

    The whole attempt is bounded by :data:`BACKTEST_TIMEOUT` via
    :func:`asyncio.wait_for` so a wedged data-provider call cannot pin a
    worker slot indefinitely; a timeout surfaces as :class:`TimeoutError`
    (== ``asyncio.TimeoutError``), which :func:`with_retry` treats as a
    transient, retryable failure. An unknown strategy raises
    ``ValueError`` which is *not* retryable and surfaces immediately.
    """
    return await asyncio.wait_for(
        _run_backtest_once(
            strategy_name=strategy_name,
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            initial_capital=initial_capital,
        ),
        timeout=BACKTEST_TIMEOUT,
    )


async def _run_backtest_once(
    *,
    strategy_name: str,
    symbol: str,
    start_date: str,
    end_date: str,
    initial_capital: float,
) -> Any:
    """Run a single backtest attempt (no retry, no timeout wrapper).

    Kept separate from :func:`_execute_backtest` so the timeout wrapper and
    the retry decorator compose cleanly, and so tests can target one attempt
    in isolation. Heavy imports stay lazy inside the body to keep worker
    startup cheap and avoid circular imports at module load time.
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
# Input validation
#
# The public backtest tasks accept untrusted, user-supplied parameters that
# travel through the Redis result backend (JSON). Rather than letting bad
# inputs surface as opaque downstream failures (or, worse, burn retry budget
# on a value that can never succeed), every public entry point validates and
# normalises its arguments via :func:`_validate_backtest_inputs` *before*
# enqueueing/executing. The helper raises :class:`TypeError` for invalid
# types and :class:`ValueError` for invalid values; neither is in
# :data:`_DEFAULT_RETRYABLE`, so both fail fast and the public tasks translate
# them into a ``failed`` result envelope.
# --------------------------------------------------------------------------- #
def _parse_iso_date(value: str, field: str) -> datetime:
    """Parse a strict ``YYYY-MM-DD`` date, raising ``TypeError``/``ValueError`` on failure.

    :param value: the raw date string supplied by the caller.
    :param field: logical field name (``"start_date"`` / ``"end_date"``)
        used to build a helpful error message.
    :returns: the parsed :class:`~datetime.datetime` (midnight UTC).
    :raises TypeError: if ``value`` is not a string.
    :raises ValueError: if ``value`` is not parseable as an ISO calendar date.
    """
    if not isinstance(value, str):
        raise TypeError(
            f"{field} must be a string in YYYY-MM-DD format, got "
            f"{type(value).__name__}"
        )
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValueError(
            f"{field} {value!r} is not a valid YYYY-MM-DD date: {exc}"
        ) from exc


def _validate_backtest_inputs(
    *,
    strategy_name: str,
    symbol: str,
    start_date: str,
    end_date: str,
    initial_capital: float,
) -> tuple[str, str, str, str, float]:
    """Validate and normalise the inputs to a backtest task.

    Applied at the top of every public task (:func:`run_backtest` and
    :func:`submit_backtest_job`) so invalid input is rejected at the API
    boundary rather than mid-run. The function both *validates* (raising
    :class:`TypeError` for non-string/non-number inputs and
    :class:`ValueError` for NaN/inf/negative capital, unparseable or
    reversed dates, or empty identifiers) and *normalises* (stripping C0/C1
    control characters and surrounding whitespace from ``strategy_name``
    and ``symbol``).

    :returns: the cleaned ``(strategy_name, symbol, start_date, end_date,
        initial_capital)`` tuple. ``start_date`` / ``end_date`` are returned
        verbatim (already validated as ISO strings) and ``initial_capital``
        is coerced to a plain ``float``.
    :raises TypeError: when an argument has an unexpected type.
    :raises ValueError: with a descriptive message for any invalid value.
    """
    # Strategy name / symbol: strip control chars + whitespace, reject empty.
    if not isinstance(strategy_name, str):
        raise TypeError("strategy_name must be a string")
    cleaned_strategy = _CONTROL_CHARS.sub("", strategy_name).strip()
    if not cleaned_strategy:
        raise ValueError("strategy_name must not be empty after cleaning")

    if not isinstance(symbol, str):
        raise TypeError("symbol must be a string")
    cleaned_symbol = _CONTROL_CHARS.sub("", symbol).strip()
    if not cleaned_symbol:
        raise ValueError("symbol must not be empty after cleaning")

    # initial_capital: reject non-numbers, booleans, NaN, inf and negatives.
    if isinstance(initial_capital, bool) or not isinstance(
        initial_capital, (int, float)
    ):
        raise TypeError(
            f"initial_capital must be a number, got "
            f"{type(initial_capital).__name__}"
        )
    if math.isnan(initial_capital) or math.isinf(initial_capital):
        raise ValueError("initial_capital must be a finite number")
    if initial_capital < 0:
        raise ValueError("initial_capital must not be negative")

    # Dates: parse strictly, then enforce a non-empty window.
    start_dt = _parse_iso_date(start_date, "start_date")
    end_dt = _parse_iso_date(end_date, "end_date")
    if start_dt >= end_dt:
        raise ValueError(
            f"start_date {start_date!r} must be strictly before "
            f"end_date {end_date!r}"
        )

    return cleaned_strategy, cleaned_symbol, start_date, end_date, float(initial_capital)


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
        # Validate + normalise before execution so bad input fails fast
        # (ValueError is non-retryable) instead of burning retry budget.
        strategy_name, symbol, start_date, end_date, initial_capital = (
            _validate_backtest_inputs(
                strategy_name=strategy_name,
                symbol=symbol,
                start_date=start_date,
                end_date=end_date,
                initial_capital=initial_capital,
            )
        )
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


# --------------------------------------------------------------------------- #
# Job submission / result collection
#
# The tasks above (``run_backtest`` / ``run_strategy_evaluation``) execute
# synchronously: the caller awaits the whole run and gets the final payload.
# The pair below completes the Taskiq integration by adding the canonical
# fire-and-forget *job* pattern — submit a backtest, get back a task id, and
# collect the outcome later — which is what long-running backtests need so
# they don't hold an HTTP/RPC slot open for minutes at a time.
# --------------------------------------------------------------------------- #
def _build_result_task(task_id: str) -> AsyncTaskiqTask[Any]:
    """Rebind a previously-submitted ``task_id`` to the broker's result backend.

    Taskiq stores results against the task id in the (Redis) result backend
    rather than against the decorated task object, so to poll a job
    submitted by :func:`submit_backtest_job` we reconstruct a bare
    :class:`~taskiq.AsyncTaskiqTask` bound to the shared
    :attr:`broker.result_backend`. No ``return_type`` is supplied, so the
    stored payload (the dict produced by :func:`run_backtest`) is returned
    verbatim without a pydantic re-parse.

    Factored as a named helper rather than inlined so tests can swap it for
    a stub and drive :func:`collect_backtest_result` without a live result
    backend.
    """
    return AsyncTaskiqTask(task_id=task_id, result_backend=broker.result_backend)


@broker.task
async def submit_backtest_job(
    strategy_name: str,
    symbol: str,
    start_date: str,
    end_date: str,
    initial_capital: float = 100_000.0,
) -> dict[str, Any]:
    """Enqueue a backtest for asynchronous execution and return its task id.

    This is the *fire* half of the submit/collect pair. Rather than
    awaiting :func:`run_backtest` inline (which blocks the caller for the
    full backtest duration), it kicks the work onto the broker via
    ``run_backtest.kiq(...)`` and returns immediately with the Taskiq task
    id. The caller then polls :func:`collect_backtest_result` with that id
    to retrieve the outcome.

    Returns a JSON-serialisable dict: ``status == "submitted"`` with the
    ``task_id`` on success, or ``status == "failed"`` (with
    ``error``/``error_type``) when the broker rejects the enqueue — e.g.
    the broker is down, or the payload cannot be serialised.
    """
    ensure_correlation_id()
    cid = get_correlation_id()
    try:
        # Reject bad input at submit time so the caller gets immediate
        # feedback rather than discovering it later from the worker.
        strategy_name, symbol, start_date, end_date, initial_capital = (
            _validate_backtest_inputs(
                strategy_name=strategy_name,
                symbol=symbol,
                start_date=start_date,
                end_date=end_date,
                initial_capital=initial_capital,
            )
        )
    except (ValueError, TypeError) as exc:
        logger.exception(
            "submit_backtest_job.invalid_input",
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
    logger.info(
        "submit_backtest_job.start",
        strategy=strategy_name,
        symbol=symbol,
        start=start_date,
        end=end_date,
        initial_capital=initial_capital,
        correlation_id=cid,
    )
    try:
        task = await run_backtest.kiq(
            strategy_name,
            symbol,
            start_date,
            end_date,
            initial_capital,
        )
    except Exception as exc:
        logger.exception(
            "submit_backtest_job.failed",
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

    task_id = getattr(task, "task_id", None)
    if task_id is None:
        # The broker accepted the enqueue but returned no task_id. This is
        # an unexpected broker state (every ListQueueBroker task is assigned
        # an id on kiq), so surface it as a failed envelope rather than
        # handing the caller an id they can never poll.
        logger.error(
            "submit_backtest_job.no_task_id",
            strategy=strategy_name,
            symbol=symbol,
            correlation_id=cid,
        )
        return {
            "status": "failed",
            "strategy_name": strategy_name,
            "symbol": symbol,
            "error": "Broker accepted the task but returned no task_id",
            "error_type": "RuntimeError",
            "correlation_id": cid,
        }
    logger.info(
        "submit_backtest_job.submitted",
        strategy=strategy_name,
        symbol=symbol,
        task_id=task_id,
        correlation_id=cid,
    )
    return {
        "status": "submitted",
        "task_id": task_id,
        "strategy_name": strategy_name,
        "symbol": symbol,
        "start_date": start_date,
        "end_date": end_date,
        "initial_capital": initial_capital,
        "correlation_id": cid,
    }


@broker.task
async def collect_backtest_result(task_id: str) -> dict[str, Any]:
    """Collect the outcome of a backtest job previously submitted.

    This is the *collect* half of the submit/collect pair. Given the
    ``task_id`` returned by :func:`submit_backtest_job`, it rebuilds a
    result handle via :func:`_build_result_task` and consults the result
    backend:

    * **not ready** → ``status == "pending"`` so the caller can poll again.
    * **ready, success** → ``status == "completed"`` with the full
      :func:`run_backtest` payload under ``result`` plus ``execution_time``.
    * **ready, error** → ``status == "failed"`` with the worker-side error.

    Any failure to reach the result backend (e.g. ``ResultGetError``, a
    dropped Redis connection) is caught and surfaced as a ``failed``
    envelope so the caller always receives a JSON-serialisable dict rather
    than an exception. The worker-side ``error`` is a ``BaseException`` and
    therefore not JSON-serialisable, so it is stringified via ``repr``
    before it can reach the result backend.
    """
    ensure_correlation_id()
    cid = get_correlation_id()
    logger.info(
        "collect_backtest_result.start",
        task_id=task_id,
        correlation_id=cid,
    )
    try:
        task = _build_result_task(task_id)
        ready = await task.is_ready()
    except Exception as exc:
        logger.exception(
            "collect_backtest_result.failed",
            task_id=task_id,
            error=str(exc),
            error_type=type(exc).__name__,
            correlation_id=cid,
            traceback=traceback.format_exc(),
        )
        return {
            "status": "failed",
            "task_id": task_id,
            "error": str(exc),
            "error_type": type(exc).__name__,
            "correlation_id": cid,
        }

    if not ready:
        logger.info(
            "collect_backtest_result.pending",
            task_id=task_id,
            correlation_id=cid,
        )
        return {
            "status": "pending",
            "task_id": task_id,
            "correlation_id": cid,
        }

    try:
        result = await task.get_result()
    except Exception as exc:
        logger.exception(
            "collect_backtest_result.failed",
            task_id=task_id,
            error=str(exc),
            error_type=type(exc).__name__,
            correlation_id=cid,
            traceback=traceback.format_exc(),
        )
        return {
            "status": "failed",
            "task_id": task_id,
            "error": str(exc),
            "error_type": type(exc).__name__,
            "correlation_id": cid,
        }

    execution_time = getattr(result, "execution_time", 0.0)
    is_err = bool(getattr(result, "is_err", False))
    # The worker-side error is a BaseException and therefore not
    # JSON-serialisable; stringify it before it can reach the result backend.
    raw_error = getattr(result, "error", None)
    error_str = repr(raw_error) if raw_error is not None else None

    if is_err:
        logger.warning(
            "collect_backtest_result.task_error",
            task_id=task_id,
            error=error_str,
            execution_time=execution_time,
            correlation_id=cid,
        )
        return {
            "status": "failed",
            "task_id": task_id,
            "error": error_str or "Task reported an error",
            "error_type": type(raw_error).__name__
            if raw_error is not None
            else "TaskError",
            "execution_time": execution_time,
            "correlation_id": cid,
        }

    return_value = getattr(result, "return_value", None)
    result_status = (
        return_value.get("status") if isinstance(return_value, dict) else None
    )
    logger.info(
        "collect_backtest_result.complete",
        task_id=task_id,
        execution_time=execution_time,
        result_status=result_status,
        correlation_id=cid,
    )
    return {
        "status": "completed",
        "task_id": task_id,
        "result": return_value,
        "execution_time": execution_time,
        "correlation_id": cid,
    }


__all__ = [
    "TaskExecutionError",
    "_validate_backtest_inputs",
    "broker",
    "collect_backtest_result",
    "on_worker_shutdown",
    "on_worker_startup",
    "run_backtest",
    "run_strategy_evaluation",
    "submit_backtest_job",
    "with_retry",
]
