from __future__ import annotations

from typing import TYPE_CHECKING, Any

from engine.plugins.sandbox.monitoring.metrics import SandboxMetricsCollector
from engine.plugins.sandbox.monitoring.violation_report import ViolationReport

if TYPE_CHECKING:
    from engine.plugins.sandbox.core.policy import SandboxPolicy
    from engine.plugins.sandbox.monitoring.event_logger import SecurityEventLogger


class SandboxAdminAPI:
    def __init__(
        self,
        metrics_collector: SandboxMetricsCollector | None = None,
    ) -> None:
        self._metrics = metrics_collector or SandboxMetricsCollector()
        self._loggers: dict[str, SecurityEventLogger] = {}
        self._policies: dict[str, SandboxPolicy] = {}

    def register_plugin(
        self,
        plugin_id: str,
        event_logger: SecurityEventLogger,
        policy: SandboxPolicy | None = None,
    ) -> None:
        self._loggers[plugin_id] = event_logger
        if policy is not None:
            self._policies[plugin_id] = policy

    def unregister_plugin(self, plugin_id: str) -> None:
        self._loggers.pop(plugin_id, None)
        self._policies.pop(plugin_id, None)

    def get_plugin_events(
        self,
        plugin_id: str,
        category: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        logger = self._loggers.get(plugin_id)
        if logger is None:
            return []
        from engine.plugins.sandbox.core.violation import SandboxViolationCategory  # noqa: PLC0415

        cat = None
        if category is not None:
            try:
                cat = SandboxViolationCategory(category)
            except ValueError:
                return []
        return logger.to_dicts(limit=limit) if cat is None else [
            e.__dict__ if hasattr(e, "__dict__") else {}
            for e in logger.get_events(category=cat, limit=limit)
        ]

    def get_violation_report(self, plugin_id: str | None = None) -> ViolationReport:
        if plugin_id is not None:
            logger = self._loggers.get(plugin_id)
            if logger is None:
                return ViolationReport(plugin_id=plugin_id)
            return ViolationReport.from_events(logger.get_events(), plugin_id=plugin_id)

        all_events: list[Any] = []
        for logger in self._loggers.values():
            all_events.extend(logger.get_events())
        return ViolationReport.from_events(all_events)

    def get_metrics(self, plugin_id: str | None = None) -> dict[str, Any]:
        if plugin_id is not None:
            result = self._metrics.get_plugin_metrics(plugin_id)
            return result if result is not None else {}
        return self._metrics.get_all_metrics()

    def get_all_plugin_ids(self) -> list[str]:
        return list(self._loggers.keys())

    def get_plugin_policy(self, plugin_id: str) -> dict[str, Any] | None:
        policy = self._policies.get(plugin_id)
        if policy is None:
            return None
        return policy.to_dict()

    def reset_metrics(self, plugin_id: str | None = None) -> None:
        self._metrics.reset(plugin_id)
