"""Broker-facing adapters.

This package holds the thin, broker-specific adapters the engine uses to
route orders and queries to individual brokers. Each adapter delegates the
heavy lifting (HTTP transport, auth, retry, typed-error mapping) to an
existing :class:`~engine.core.execution.base.ExecutionBackend` so the
broker integration stays a small, well-tested facade rather than a second
copy of the request machinery in :mod:`engine.core.brokers.base`.

Public surface:
- :class:`~engine.brokers.alpaca.AlpacaBrokerAdapter` — Alpaca REST adapter
  (market/limit orders, cancel, position query) on top of
  :class:`~engine.execution.live_backend.LiveExecutionBackend`.
"""

from engine.brokers.alpaca import AlpacaBrokerAdapter

__all__ = ["AlpacaBrokerAdapter"]
