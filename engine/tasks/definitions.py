"""Taskiq task definitions for the Nexus async worker.

This module is the single source of truth for *what tasks exist* and *how
they execute*. It owns the canonical AsyncBroker (a Redis/Valkey-backed
:class:`ListQueueBroker`) and registers every ``@broker.task``.

Design notes
------------
* **Import-safe.** Building the broker does not open a network connection —
  taskiq only connects lazily (on ``broker.startup()`` or the first
  ``.kiq()``), so importing this module has no side effects and is safe to
  import from tests.
* **Decoupled from wiring.** :mod:`engine.tasks.worker` handles lifecycle
  (scheduler, startup/shutdown) and the CLI entrypoint; this module is
  purely the definitions catalog. ``worker`` can be migrated to re-export
  from here without changing call sites.
* **EventBus integration.** ``run_backtest_task`` emits
  :data:`~engine.events.bus.EventType.BACKTEST_STARTED` and
  :data:`~engine.events.bus.EventType.BACKTEST_COMPLETED` so WebSocket
  clients and other subscribers observe lifecycle progress without polling
  the result backend.
* **Testable.** The EventBus is resolved through :func:`get_event_bus` and
  injectable via :func:`set_event_bus`; heavy collaborators
  (``BacktestRunner``, data provider, strategy registry) are imported
  lazily *inside* the task body so they can be patched per-test.
"""

from __future__ import annotations

import traceback
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse, urlunparse

import structlog
from taskiq_redis import ListQueueBroker, RedisAsyncResultBackend

from engine.config import settings
from engine.observability.taskiq_middleware import CorrelationMiddleware

if TYPE_CHECKING:
    from engine.events.bus import EventBus

logger = structlog.get_logger()

__all__ = [
    "broker",
    "build_broker",
    "get_event_bus",
    "run_backtest_task",
    "set_event_bus",
]


def _redis_url(valkey_url: str | None = None) -> str:
    """Derive a plain ``redis://`` URL from a ``valkey://`` setting.

    Valkey advertises a ``valkey://`` scheme which the redis-py based
    taskiq backend does not understand, so we normalise the scheme while
    preserving host/port/db. ``valkey://localhost:6379/0`` →
    ``redis://localhost:6379/0``.
    """
    parsed = urlparse(valkey_url or settings.valkey_url)
    return urlunparse(parsed._replace(scheme="redis"))


def build_broker(redis_url: str | None = None) -> ListQueueBroker:
    """Construct the AsyncBroker wired to Redis/Valkey.

    Returns a :class:`ListQueueBroker` (the Redis-backed list queue used as
    the worker's async broker) configured with:

    * a :class:`RedisAsyncResultBackend` so ``.kiq().await_result()`` works,
    * the :class:`CorrelationMiddleware` which propagates correlation IDs
      across the producer/consumer boundary via message labels.

    ``redis_url`` defaults to the value derived from ``settings.valkey_url``;
    tests may pass an explicit value. No connection is opened here.
    """
    url = redis_url or _redis_url()
    return (
        ListQueueBroker(url=url)
        .with_result_backend(RedisAsyncResultBackend(redis_url=url))
        .with_middlewares(CorrelationMiddleware())
    )


# Module-level broker. ``@broker.task`` decorators below register against
# this instance, and the worker entrypoint re-exports it.
broker = build_broker()

# Lazily-created EventBus used by tasks. Held at module scope (rather than
# constructed inline inside the task body) so tests can swap it with
# ``set_event_bus(...)`` without monkeypatching internals. Resolved through
# :func:`get_event_bus` on each invocation.
_event_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    """Return the module-scoped EventBus, constructing one on first use.

    The bus is created lazily and intentionally *not* connected here —
    :meth:`EventBus.publish` falls back to in-process delivery when Redis
    is unavailable, so a missing connection is non-fatal for the task.
    Callers that need the Redis pub/sub fan-out should ``await
    bus.connect()`` explicitly.
    """
    global _event_bus  # noqa: PLW0603
    if _event_bus is None:
        from engine.events.bus import EventBus  # noqa: PLC0415

        _event_bus = EventBus(redis_url=_redis_url())
    return _event_bus


def set_event_bus(bus: EventBus | None) -> None:
    """Inject (or clear with ``None``) the module-scoped EventBus.

    Primarily for tests: inject an in-memory or mock bus to assert which
    events a task publishes without standing up Redis.
    """
    global _event_bus  # noqa: PLW0603
    _event_bus = bus


def _summarize_result(result: Any) -> dict[str, Any]:
    """Project a :class:`BacktestResult` into a JSON-serializable payload.

    Kept narrow on purpose — the full ``equity_curve`` / ``trades`` can be
    large and are intentionally *not* shoved through the EventBus (callers
    that need them read the result backend). Downstream subscribers get the
    summary metrics plus provenance fields.
    """
    return {
        "total_trades": len(getattr(result, "trades", []) or []),
        "total_return_pct": getattr(result, "total_return_pct", 0.0),
        "final_capital": getattr(result, "final_capital", 0.0),
        "metrics": getattr(result, "metrics", {}) or {},
    }


@broker.task
async def run_backtest_task(
    strategy_name: str,
    symbol: str,
    start_date: str,
    end_date: str,
    initial_capital: float = 100_000.0,
    **params: Any,
) -> dict[str, Any]:
    """Run a full backtest and publish lifecycle events on the EventBus.

    Parameters
    ----------
    strategy_name:
        Strategy identifier (registry key). Maps to
        :class:`~engine.core.backtest_runner.BacktestConfig.strategy_name`.
    symbol, start_date, end_date, initial_capital:
        Backtest configuration — passed straight through to
        :class:`BacktestConfig`.
    **params:
        Free-form strategy parameters. They are recorded in the published
        events (for provenance/audit) and survive taskiq's JSON wire format.

    Returns
    -------
    dict
        ``{"status": "completed", ...summary...}`` on success, or
        ``{"status": "failed", "error": ..., "error_type": ...}`` on
        failure. Errors are *returned*, not raised — taskiq would otherwise
        surface the traceback only in worker logs, and callers polling the
        result backend expect a structured payload either way. A
        ``BACKTEST_COMPLETED`` event with the failure details is still
        emitted so subscribers are notified.

    Dispatches to :class:`~engine.core.backtest_runner.BacktestRunner` and
    publishes :data:`~engine.events.bus.EventType.BACKTEST_STARTED` /
    :data:`~engine.events.bus.EventType.BACKTEST_COMPLETED` via the
    :class:`~engine.events.bus.EventBus`.
    """
    from engine.core.backtest_runner import BacktestConfig, BacktestRunner  # noqa: PLC0415
    from engine.data.feeds import get_data_provider  # noqa: PLC0415
    from engine.events.bus import EventType  # noqa: PLC0415
    from engine.plugins.registry import PluginRegistry  # noqa: PLC0415

    provenance = {
        "strategy_name": strategy_name,
        "symbol": symbol,
        "start_date": start_date,
        "end_date": end_date,
        "params": params,
    }
    logger.info("backtest_task.start", **provenance)

    bus = get_event_bus()
    await bus.emit(
        EventType.BACKTEST_STARTED,
        data={**provenance, "initial_capital": initial_capital},
        source="taskiq",
    )

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

    try:
        runner = BacktestRunner(config=config, strategy=strategy, provider=provider)
        result = await runner.run()

        payload: dict[str, Any] = {
            "status": "completed",
            "strategy_name": strategy_name,
            "symbol": symbol,
            **_summarize_result(result),
        }
        logger.info(
            "backtest_task.complete",
            strategy=strategy_name,
            total_trades=payload["total_trades"],
            total_return_pct=round(payload["total_return_pct"], 2),
        )
        await bus.emit(
            EventType.BACKTEST_COMPLETED,
            data={**payload, "initial_capital": initial_capital, "params": params},
            source="taskiq",
        )
    except Exception as e:
        logger.exception(
            "backtest_task.failed",
            strategy=strategy_name,
            symbol=symbol,
            error=str(e),
            traceback=traceback.format_exc(),
        )
        failure = {
            "status": "failed",
            "strategy_name": strategy_name,
            "symbol": symbol,
            "error": str(e),
            "error_type": type(e).__name__,
        }
        await bus.emit(
            EventType.BACKTEST_COMPLETED,
            data={**failure, "params": params},
            source="taskiq",
        )
        return failure
    else:
        return payload
