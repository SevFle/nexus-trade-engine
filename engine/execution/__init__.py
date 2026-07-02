"""Execution backends — live / paper / backtest adapters.

This package re-exports the abstract :class:`~engine.core.execution.base.ExecutionBackend`
ABC from :mod:`engine.core.execution.base` alongside the concrete
:class:`LiveExecutionBackend`, an Alpaca-compatible REST-backed live adapter.

LiveExecutionBackend is the swappable layer the order manager uses to route
orders to a real broker without changing strategy code. It implements the
ExecutionBackend ABC (``connect`` / ``disconnect`` / ``execute``) *and*
exposes broker-direct async helpers (``submit_order`` / ``cancel_order`` /
``get_order_status``) that talk to an Alpaca-compatible REST API over an
injectable ``httpx.AsyncClient``.
"""

from __future__ import annotations

from engine.core.execution.base import ExecutionBackend, FillResult
from engine.execution.live_backend import LiveExecutionBackend

__all__ = ["ExecutionBackend", "FillResult", "LiveExecutionBackend"]
