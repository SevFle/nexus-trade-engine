from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import structlog

from engine.core.signal import Signal
from engine.plugins.sandbox.core.context import SandboxContext
from engine.plugins.sandbox.monitoring.event_logger import SecurityEventLogger
from engine.plugins.sandbox.monitoring.metrics import SandboxMetricsCollector

if TYPE_CHECKING:
    from collections.abc import Callable

    from engine.core.cost_model import ICostModel
    from engine.core.portfolio import PortfolioSnapshot
    from engine.plugins.sandbox.core.policy import SandboxPolicy

logger = structlog.get_logger()

_metrics_collector = SandboxMetricsCollector()
_eval_lock: asyncio.Lock = asyncio.Lock()


class PluginSandboxExecutor:
    def __init__(
        self,
        strategy: Any,
        policy: SandboxPolicy,
        metrics_collector: SandboxMetricsCollector | None = None,
    ) -> None:
        self.strategy = strategy
        self.policy = policy
        self._metrics = metrics_collector or _metrics_collector
        self._event_logger = SecurityEventLogger(plugin_id=policy.plugin_id)
        self._context = SandboxContext(policy)

    @classmethod
    def from_factory(
        cls,
        strategy_factory: Callable[[], Any],
        policy: SandboxPolicy,
        metrics_collector: SandboxMetricsCollector | None = None,
    ) -> PluginSandboxExecutor:
        class _Placeholder:
            name = "_placeholder"
            version = "0.0.0"

            def on_bar(self, _s: Any, _p: Any) -> list[Any]:
                return []

        executor = cls(_Placeholder(), policy, metrics_collector)
        executor._context.activate()
        try:
            executor.strategy = strategy_factory()
        finally:
            executor._context.deactivate()
        return executor

    async def safe_evaluate(
        self,
        portfolio: PortfolioSnapshot,
        market: Any,
        _costs: ICostModel,
    ) -> list[Signal]:
        async with _eval_lock:
            return await self._evaluate_inner(portfolio, market)

    async def _evaluate_inner(
        self,
        portfolio: PortfolioSnapshot,
        market: Any,
    ) -> list[Signal]:
        start = time.monotonic()
        self._context.activate()

        try:
            raw_signals = await asyncio.wait_for(
                self._call_strategy(portfolio, market),
                timeout=self.policy.resource_policy.max_cpu_seconds,
            )
        except TimeoutError:
            elapsed_ms = (time.monotonic() - start) * 1000
            error_msg = f"Timeout after {self.policy.resource_policy.max_cpu_seconds}s"
            self._metrics.record_evaluation(
                self.policy.plugin_id,
                elapsed_ms,
                0,
                error=error_msg,
            )
            logger.exception(
                "sandbox.timeout",
                strategy_name=self.strategy.name,
                timeout_s=self.policy.resource_policy.max_cpu_seconds,
            )
            return []
        except Exception as e:
            elapsed_ms = (time.monotonic() - start) * 1000
            self._metrics.record_evaluation(
                self.policy.plugin_id,
                elapsed_ms,
                0,
                error=str(e),
            )
            logger.exception(
                "sandbox.evaluation_error",
                strategy_name=self.strategy.name,
                error=str(e),
                elapsed_ms=elapsed_ms,
            )
            return []
        finally:
            self._context.deactivate()

        elapsed_ms = (time.monotonic() - start) * 1000
        signals = self._convert_signals(raw_signals)
        self._metrics.record_evaluation(
            self.policy.plugin_id,
            elapsed_ms,
            len(signals),
        )
        return signals

    async def _call_strategy(
        self,
        portfolio: PortfolioSnapshot,
        market: Any,
    ) -> list[Any]:
        result = self.strategy.on_bar(market, portfolio)
        if asyncio.iscoroutine(result):
            result = await result
        return result

    def _convert_signals(self, raw_signals: list[Any]) -> list[Signal]:
        validated: list[Signal] = []
        for s in raw_signals:
            if isinstance(s, Signal):
                if not s.strategy_id:
                    s.strategy_id = self.strategy.name
                validated.append(s)
            else:
                logger.warning(
                    "sandbox.invalid_signal",
                    strategy_name=self.strategy.name,
                    signal_type=type(s).__name__,
                )
        return validated

    def get_health(self) -> dict[str, Any]:
        metrics = self._metrics.get_plugin_metrics(self.policy.plugin_id) or {}
        return {
            "strategy_name": self.strategy.name,
            "version": self.strategy.version,
            "plugin_id": self.policy.plugin_id,
            "trust_level": self.policy.trust_level,
            **metrics,
        }

    def cleanup(self) -> None:
        self._context.cleanup()
