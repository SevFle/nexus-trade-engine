"""Structured logging and metrics for MCP tool invocations.

Reuses the engine's existing observability primitives (``structlog`` and the
pluggable :mod:`engine.observability.metrics` backend) so MCP traffic shows up
in the same dashboards as REST/WebSocket traffic. Metric names use the
``mcp.<signal>`` namespace.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import structlog

from engine.observability.metrics import get_metrics

if TYPE_CHECKING:
    from engine.mcp.auth import AuthPrincipal

logger = structlog.get_logger()

_METRIC_CALL = "mcp.tool.call"
_METRIC_DURATION = "mcp.tool.duration_ms"
_METRIC_ERROR = "mcp.tool.error"


def _base_tags(
    tool_name: str,
    principal: AuthPrincipal,
    *,
    status: str,
) -> dict[str, str]:
    return {
        "tool": tool_name,
        "role": principal.role,
        "status": status,
        "auth_method": principal.auth_method,
    }


def record_start(tool_name: str, principal: AuthPrincipal) -> float:
    logger.info(
        "mcp.tool.invoke",
        tool=tool_name,
        user_id=principal.user_id,
        role=principal.role,
        auth_method=principal.auth_method,
    )
    return time.monotonic()


def record_success(
    tool_name: str,
    principal: AuthPrincipal,
    started_at: float,
) -> None:
    duration_ms = (time.monotonic() - started_at) * 1000.0
    tags = _base_tags(tool_name, principal, status="ok")
    metrics = get_metrics()
    metrics.counter(_METRIC_CALL, tags=tags)
    metrics.histogram(_METRIC_DURATION, duration_ms, tags=tags)
    logger.info(
        "mcp.tool.complete",
        tool=tool_name,
        duration_ms=round(duration_ms, 2),
        user_id=principal.user_id,
    )


def record_error(
    tool_name: str,
    principal: AuthPrincipal,
    started_at: float,
    error: BaseException,
) -> None:
    duration_ms = (time.monotonic() - started_at) * 1000.0
    status = type(error).__name__
    tags = _base_tags(tool_name, principal, status=status)
    metrics = get_metrics()
    metrics.counter(_METRIC_CALL, tags=tags)
    metrics.counter(_METRIC_ERROR, tags=tags)
    metrics.histogram(_METRIC_DURATION, duration_ms, tags=tags)
    logger.warning(
        "mcp.tool.error",
        tool=tool_name,
        duration_ms=round(duration_ms, 2),
        error=type(error).__name__,
        message=str(error),
        user_id=principal.user_id,
    )


def render_metrics() -> str:
    """Expose recorded metrics in Prometheus text-exposition format.

    Uses the engine's :mod:`engine.observability.prometheus` renderer against
    whatever backend is currently installed via
    :func:`engine.observability.metrics.set_metrics`.
    """
    from engine.observability.metrics import get_metrics
    from engine.observability.prometheus import render_prometheus

    backend = get_metrics()
    if hasattr(backend, "render"):
        return backend.render()  # type: ignore[no-any-return]
    # Fall back to the standalone renderer for plain RecordingBackend.
    if hasattr(backend, "counters"):
        return render_prometheus(backend)  # type: ignore[arg-type]
    return ""


__all__ = [
    "record_error",
    "record_start",
    "record_success",
    "render_metrics",
]
