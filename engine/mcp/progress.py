"""MCP progress notification helper.

Backtests can take minutes. MCP lets a server emit ``notifications/progress``
so the assistant can surface progress to the user. The
:class:`ProgressReporter` wraps the low-level session call so adapters do not
need to know about the MCP transport.

Because the engine's :class:`~engine.core.backtest_runner.BacktestRunner.run`
loop is currently monolithic, finer-grained intra-run progress would require
runner instrumentation. The reporter is therefore invoked at well-defined
lifecycle points (start/complete); the abstraction is complete and tested so
that finer granularity can be wired in without touching adapter code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from mcp.server.session import ServerSession

logger = structlog.get_logger()


class ProgressReporter:
    """No-op-safe wrapper around ``session.send_progress_notification``.

    ``enabled=False`` (the default when no progress token was supplied by the
    client) makes every call a cheap no-op, so adapters can always call
    :meth:`report` without guarding.
    """

    def __init__(
        self,
        session: ServerSession | None = None,
        progress_token: str | int | None = None,
        *,
        enabled: bool | None = None,
    ) -> None:
        self._session = session
        self._progress_token = progress_token
        if enabled is None:
            enabled = session is not None and progress_token is not None
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def report(
        self,
        progress: float,
        *,
        total: float | None = None,
        message: str | None = None,
    ) -> None:
        if not self._enabled or self._session is None or self._progress_token is None:
            return
        try:
            await self._session.send_progress_notification(
                progress_token=self._progress_token,
                progress=progress,
                total=total,
                message=message,
            )
        except Exception:
            logger.debug("mcp.progress_notification_failed", progress=progress)


__all__ = ["ProgressReporter"]
