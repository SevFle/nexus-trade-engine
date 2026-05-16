from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from engine.plugins.sandbox.core.lifecycle import LifecycleManager, SandboxLifecycle
from engine.plugins.sandbox.core.policy import SandboxPolicy
from engine.plugins.sandbox.core.state import SandboxTLS, get_default_tls
from engine.plugins.sandbox.monitoring.event_logger import SecurityEventLogger
from engine.plugins.sandbox.monitoring.metrics import SandboxMetricsCollector

if TYPE_CHECKING:
    from engine.plugins.sandbox.core.context import SandboxContext

logger = structlog.get_logger()


class SandboxIntegration:
    def __init__(
        self,
        metrics_collector: SandboxMetricsCollector | None = None,
        tls: SandboxTLS | None = None,
    ) -> None:
        self._metrics = metrics_collector or SandboxMetricsCollector()
        self._tls = tls or get_default_tls()
        self._lifecycle_manager = LifecycleManager()
        self._contexts: dict[str, SandboxContext] = {}

    def register(self, context: SandboxContext) -> SandboxLifecycle:
        plugin_id = context.policy.plugin_id
        self._contexts[plugin_id] = context
        return self._lifecycle_manager.create(context)

    def unregister(self, plugin_id: str) -> None:
        self._lifecycle_manager.cleanup(plugin_id)
        self._contexts.pop(plugin_id, None)

    def activate(self, plugin_id: str) -> SandboxLifecycle | None:
        lc = self._lifecycle_manager.get(plugin_id)
        if lc is None:
            return None
        start = __import__("time").monotonic()
        try:
            lc.activate()
            elapsed = (__import__("time").monotonic() - start) * 1000
            self._metrics.record_evaluation(plugin_id, elapsed, 0)
            logger.info("sandbox.activated", plugin_id=plugin_id, elapsed_ms=round(elapsed, 2))
        except Exception as e:
            self._metrics.record_evaluation(plugin_id, 0, 0, error=str(e))
            logger.exception("sandbox.activation_failed", plugin_id=plugin_id, error=str(e))
            raise
        return lc

    def deactivate(self, plugin_id: str) -> None:
        lc = self._lifecycle_manager.get(plugin_id)
        if lc is not None:
            lc.deactivate()
            logger.info("sandbox.deactivated", plugin_id=plugin_id)

    def get_context(self, plugin_id: str) -> SandboxContext | None:
        return self._contexts.get(plugin_id)

    def get_lifecycle(self, plugin_id: str) -> SandboxLifecycle | None:
        return self._lifecycle_manager.get(plugin_id)

    def get_metrics(self, plugin_id: str) -> dict[str, Any] | None:
        return self._metrics.get_plugin_metrics(plugin_id)

    def get_all_metrics(self) -> dict[str, dict[str, Any]]:
        return self._metrics.get_all_metrics()

    def get_active_plugins(self) -> list[str]:
        return self._lifecycle_manager.get_active()

    def get_all_states(self) -> dict[str, dict[str, Any]]:
        return self._lifecycle_manager.get_all_states()

    def shutdown(self) -> None:
        self._lifecycle_manager.cleanup_all()
        self._contexts.clear()
        logger.info("sandbox.shutdown", active_count=0)

    @staticmethod
    def create_policy(
        plugin_id: str = "unknown",
        trust_level: str = "untrusted",
        **overrides: Any,
    ) -> SandboxPolicy:
        from engine.plugins.trust_levels import TrustLevel

        try:
            tl = TrustLevel(trust_level)
        except ValueError:
            tl = TrustLevel.UNTRUSTED

        policy = SandboxPolicy.from_trust_level(tl, plugin_id=plugin_id)

        if "allowed_endpoints" in overrides:
            policy.network_policy.allowed_endpoints = overrides["allowed_endpoints"]
        if "max_cpu_seconds" in overrides:
            policy.resource_policy.max_cpu_seconds = overrides["max_cpu_seconds"]
        if "max_memory_bytes" in overrides:
            policy.resource_policy.max_memory_bytes = overrides["max_memory_bytes"]
        if "blocked_modules" in overrides:
            policy.import_policy.blocked_modules = overrides["blocked_modules"]

        return policy
