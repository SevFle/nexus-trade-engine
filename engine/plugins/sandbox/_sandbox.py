"""
Strategy Sandbox - isolated execution environment for plugins.

Enforces five security layers:
  1. Import restrictions (blocked modules via RestrictedImporter)
  2. Network whitelist (NetworkGuard for declared endpoints)
  3. Resource limits (memory, file descriptors, CPU via resource on Linux)
  4. Filesystem isolation (temp working dir, read-only artifacts)
  5. Introspection blocking (restricted builtins, getattr, object)

All strategy method calls go through the sandbox, which:
- Restricts imports to a safe allowlist
- Whitelists network endpoints from the manifest
- Enforces resource limits (memory, file descriptors, CPU timeout)
- Isolates filesystem access to a temp directory
- Blocks dangerous introspection (__subclasses__, __globals__, etc.)
- Serialises concurrent evaluations to prevent global-state races
- Tracks metrics for the dashboard
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from engine.core.signal import Signal
from engine.plugins.sandbox.core.context import SandboxContext
from engine.plugins.sandbox.core.policy import SandboxPolicy
from engine.plugins.sandbox.monitoring.metrics import SandboxMetricsCollector

if TYPE_CHECKING:
    from collections.abc import Callable

    from engine.core.cost_model import ICostModel
    from engine.core.portfolio import PortfolioSnapshot
    from engine.plugins.manifest import StrategyManifest
    from engine.plugins.sandboxed_http import SandboxedHttpClient

logger = structlog.get_logger()

_eval_lock: asyncio.Lock = asyncio.Lock()
_metrics_collector = SandboxMetricsCollector()


@dataclass
class SandboxMetrics:
    total_evaluations: int = 0
    total_signals_emitted: int = 0
    total_cpu_time_ms: float = 0.0
    avg_evaluation_ms: float = 0.0
    peak_memory_mb: float = 0.0
    errors: int = 0
    last_error: str | None = None
    api_calls: int = 0


class _PlaceholderStrategy:
    name = "_placeholder"
    version = "0.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        return []


class StrategySandbox:
    def __init__(self, strategy: Any, manifest: StrategyManifest) -> None:
        self.strategy = strategy
        self.manifest = manifest
        self.metrics = SandboxMetrics()
        self._max_eval_seconds = manifest.resources.max_cpu_seconds

        self._policy = SandboxPolicy.from_manifest(manifest)
        self._context = SandboxContext(self._policy, metrics_collector=_metrics_collector)

        endpoints = self._policy.network_policy.allowed_endpoints
        if endpoints:
            from engine.plugins.sandboxed_http import SandboxedHttpClient  # noqa: PLC0415

            self._http_client: SandboxedHttpClient | None = SandboxedHttpClient(
                allowed_endpoints=endpoints,
            )
        else:
            self._http_client = None

    @classmethod
    def from_factory(
        cls,
        strategy_factory: Callable[[], Any],
        manifest: StrategyManifest,
    ) -> StrategySandbox:
        sandbox = cls(_PlaceholderStrategy(), manifest)
        sandbox._context.activate()
        try:
            sandbox.strategy = strategy_factory()
        finally:
            sandbox._context.deactivate()
        return sandbox

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
                timeout=self._max_eval_seconds,
            )
        except TimeoutError:
            elapsed_ms = (time.monotonic() - start) * 1000
            self.metrics.errors += 1
            self.metrics.last_error = f"Timeout after {self._max_eval_seconds}s"
            logger.exception(
                "sandbox.timeout",
                strategy_name=self.strategy.name,
                timeout_s=self._max_eval_seconds,
            )
            return []
        except Exception as e:
            elapsed_ms = (time.monotonic() - start) * 1000
            self.metrics.errors += 1
            self.metrics.last_error = str(e)
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
        self._update_metrics(elapsed_ms, len(signals))
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

    def _update_metrics(self, elapsed_ms: float, signal_count: int) -> None:
        self.metrics.total_evaluations += 1
        self.metrics.total_signals_emitted += signal_count
        self.metrics.total_cpu_time_ms += elapsed_ms
        self.metrics.avg_evaluation_ms = (
            self.metrics.total_cpu_time_ms / self.metrics.total_evaluations
        )
        _metrics_collector.record_evaluation(
            self._policy.plugin_id,
            elapsed_ms,
            signal_count,
        )

    @property
    def event_logger(self):
        return self._context.event_logger

    @property
    def _work_dir(self) -> str | None:
        return self._context.work_dir

    def cleanup(self) -> None:
        self._context.cleanup()

    def get_health(self) -> dict:
        plugin_metrics = _metrics_collector.get_plugin_metrics(self._policy.plugin_id)
        return {
            "strategy_name": self.strategy.name,
            "version": self.strategy.version,
            "evaluations": self.metrics.total_evaluations,
            "signals_emitted": self.metrics.total_signals_emitted,
            "avg_eval_ms": round(self.metrics.avg_evaluation_ms, 2),
            "errors": self.metrics.errors,
            "last_error": self.metrics.last_error,
            "security_violations": (
                plugin_metrics.get("security_violations", 0)
                if plugin_metrics
                else 0
            ),
        }

    @staticmethod
    def _parse_memory(mem_str: str) -> int:
        val = mem_str.strip().upper()
        units: dict[str, int] = {
            "GB": 1024**3,
            "MB": 1024**2,
            "KB": 1024,
            "B": 1,
        }
        for suffix, multiplier in sorted(units.items(), key=lambda x: -len(x[0])):
            if val.endswith(suffix):
                return int(float(val[: -len(suffix)]) * multiplier)
        return int(val)
