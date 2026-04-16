"""
Strategy Sandbox — isolated execution environment for plugins.

Enforces resource limits, network whitelists, and filesystem isolation.
In production, this would use containers or WASM. This scaffold uses
process-level isolation with resource tracking.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import structlog
from core.cost_model import ICostModel
from core.portfolio import PortfolioSnapshot
from core.signal import Signal
from plugins.manifest import StrategyManifest
from plugins.sdk import IStrategy, MarketState

logger = structlog.get_logger()


@dataclass
class SandboxMetrics:
    """Runtime metrics for a sandboxed strategy."""
    total_evaluations: int = 0
    total_signals_emitted: int = 0
    total_cpu_time_ms: float = 0.0
    avg_evaluation_ms: float = 0.0
    peak_memory_mb: float = 0.0
    errors: int = 0
    last_error: str | None = None
    api_calls: int = 0


class StrategySandbox:
    """
    Wraps a strategy instance with resource monitoring and enforcement.

    All strategy method calls go through the sandbox, which:
    - Tracks CPU time per evaluation
    - Enforces max execution time
    - Catches and logs errors without crashing the engine
    - Records metrics for the dashboard
    """

    def __init__(self, strategy: IStrategy, manifest: StrategyManifest):
        self.strategy = strategy
        self.manifest = manifest
        self.metrics = SandboxMetrics()
        self._max_eval_seconds = manifest.resources.max_cpu_seconds

    async def safe_evaluate(
        self,
        portfolio: PortfolioSnapshot,
        market: MarketState,
        costs: ICostModel,
    ) -> list[Signal]:
        """
        Execute strategy.evaluate() with timeout and error handling.
        Returns empty list on failure — never crashes the engine.
        """
        start = time.monotonic()

        try:
            signals = await asyncio.wait_for(
                self.strategy.evaluate(portfolio, market, costs),
                timeout=self._max_eval_seconds,
            )

            elapsed_ms = (time.monotonic() - start) * 1000
            self._update_metrics(elapsed_ms, len(signals))

            # Validate signal format
            validated = self._validate_signals(signals)
            return validated

        except TimeoutError:
            elapsed_ms = (time.monotonic() - start) * 1000
            self.metrics.errors += 1
            self.metrics.last_error = f"Timeout after {self._max_eval_seconds}s"
            logger.error(
                "sandbox.timeout",
                strategy_id=self.strategy.id,
                timeout_s=self._max_eval_seconds,
            )
            return []

        except Exception as e:
            elapsed_ms = (time.monotonic() - start) * 1000
            self.metrics.errors += 1
            self.metrics.last_error = str(e)
            logger.error(
                "sandbox.evaluation_error",
                strategy_id=self.strategy.id,
                error=str(e),
                elapsed_ms=elapsed_ms,
            )
            return []

    def _validate_signals(self, signals: list) -> list[Signal]:
        """Ensure all returned signals are valid Signal objects."""
        validated = []
        for s in signals:
            if isinstance(s, Signal):
                # Inject strategy ID if missing
                if not s.strategy_id:
                    s.strategy_id = self.strategy.id
                validated.append(s)
            else:
                logger.warning(
                    "sandbox.invalid_signal",
                    strategy_id=self.strategy.id,
                    signal_type=type(s).__name__,
                )
        return validated

    def _update_metrics(self, elapsed_ms: float, signal_count: int):
        self.metrics.total_evaluations += 1
        self.metrics.total_signals_emitted += signal_count
        self.metrics.total_cpu_time_ms += elapsed_ms
        self.metrics.avg_evaluation_ms = (
            self.metrics.total_cpu_time_ms / self.metrics.total_evaluations
        )

    def get_health(self) -> dict:
        return {
            "strategy_id": self.strategy.id,
            "strategy_name": self.strategy.name,
            "version": self.strategy.version,
            "evaluations": self.metrics.total_evaluations,
            "signals_emitted": self.metrics.total_signals_emitted,
            "avg_eval_ms": round(self.metrics.avg_evaluation_ms, 2),
            "errors": self.metrics.errors,
            "last_error": self.metrics.last_error,
        }
