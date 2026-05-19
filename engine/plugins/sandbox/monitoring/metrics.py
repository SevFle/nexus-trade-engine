from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

logger = structlog.get_logger()


@dataclass
class PluginMetrics:
    plugin_id: str
    total_evaluations: int = 0
    total_signals_emitted: int = 0
    total_cpu_time_ms: float = 0.0
    avg_evaluation_ms: float = 0.0
    peak_memory_bytes: int = 0
    current_memory_bytes: int = 0
    api_calls: int = 0
    errors: int = 0
    last_error: str | None = None
    security_violations: int = 0
    file_operations: int = 0
    network_requests: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "total_evaluations": self.total_evaluations,
            "total_signals_emitted": self.total_signals_emitted,
            "total_cpu_time_ms": round(self.total_cpu_time_ms, 2),
            "avg_evaluation_ms": round(self.avg_evaluation_ms, 2),
            "peak_memory_bytes": self.peak_memory_bytes,
            "current_memory_bytes": self.current_memory_bytes,
            "api_calls": self.api_calls,
            "errors": self.errors,
            "last_error": self.last_error,
            "security_violations": self.security_violations,
            "file_operations": self.file_operations,
            "network_requests": self.network_requests,
        }


class SandboxMetricsCollector:
    def __init__(self) -> None:
        self._plugins: dict[str, PluginMetrics] = {}

    def get_or_create(self, plugin_id: str) -> PluginMetrics:
        if plugin_id not in self._plugins:
            self._plugins[plugin_id] = PluginMetrics(plugin_id=plugin_id)
        return self._plugins[plugin_id]

    def record_evaluation(
        self,
        plugin_id: str,
        elapsed_ms: float,
        signal_count: int,
        error: str | None = None,
    ) -> None:
        m = self.get_or_create(plugin_id)
        m.total_evaluations += 1
        m.total_signals_emitted += signal_count
        m.total_cpu_time_ms += elapsed_ms
        m.avg_evaluation_ms = m.total_cpu_time_ms / m.total_evaluations
        if error:
            m.errors += 1
            m.last_error = error
        logger.debug(
            "metrics.evaluation",
            plugin_id=plugin_id,
            elapsed_ms=round(elapsed_ms, 2),
            signals=signal_count,
        )

    def record_violation(self, plugin_id: str) -> None:
        m = self.get_or_create(plugin_id)
        m.security_violations += 1

    def get_all_metrics(self) -> dict[str, dict[str, Any]]:
        return {pid: m.to_dict() for pid, m in self._plugins.items()}

    def get_plugin_metrics(self, plugin_id: str) -> dict[str, Any] | None:
        if plugin_id in self._plugins:
            return self._plugins[plugin_id].to_dict()
        return None

    def reset(self, plugin_id: str | None = None) -> None:
        if plugin_id:
            self._plugins.pop(plugin_id, None)
        else:
            self._plugins.clear()
