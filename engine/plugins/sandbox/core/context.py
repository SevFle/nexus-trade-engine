from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from engine.plugins.sandbox.core.policy import SandboxPolicy
    from engine.plugins.sandbox.monitoring.metrics import SandboxMetricsCollector

from engine.plugins.sandbox.core.violation import SandboxViolationCategory
from engine.plugins.sandbox.layers import (
    FilesystemIsolation,
    IntrospectionGuard,
    NetworkGuard,
    ResourceLimiter,
    RestrictedImporter,
)
from engine.plugins.sandbox.monitoring.event_logger import SecurityEventLogger
from engine.plugins.sandbox.core.violation import SandboxViolationCategory
from engine.plugins.trust_levels import TrustLevel

_MIN_BLOCKED_MODULES_UNTRUSTED = 10
_MIN_BLOCKED_MODULES_LIMITED = 5
_MAX_CPU_SECONDS_UNTRUSTED = 60
_MAX_CPU_SECONDS_LIMITED = 120

_UNTRUSTED_MIN_BLOCKED_MODULES = 10
_UNTRUSTED_MAX_CPU_SECONDS = 60
_LIMITED_MIN_BLOCKED_MODULES = 5
_LIMITED_MAX_CPU_SECONDS = 120


class SandboxContext:
    def __init__(
        self,
        policy: SandboxPolicy,
        metrics_collector: SandboxMetricsCollector | None = None,
    ) -> None:
        self._policy = policy
        self._trust_level = self._resolve_trust_level()
        self._event_logger = SecurityEventLogger(plugin_id=policy.plugin_id)
        self._metrics_collector = metrics_collector
        self._import_layer = RestrictedImporter(
            blocked=policy.import_policy.blocked_modules,
            allowed=policy.import_policy.allowed_modules or None,
            plugin_id=policy.plugin_id,
        )
        self._network_layer = NetworkGuard(
            policy=policy.network_policy,
            plugin_id=policy.plugin_id,
        )
        self._resource_layer = ResourceLimiter(
            policy=policy.resource_policy,
            plugin_id=policy.plugin_id,
        )
        self._filesystem_layer = FilesystemIsolation(
            policy=policy.filesystem_policy,
            plugin_id=policy.plugin_id,
        )
        self._introspection_layer = IntrospectionGuard(
            policy=policy.introspection_policy,
            plugin_id=policy.plugin_id,
        )
        self._active = False

    @property
    def policy(self) -> SandboxPolicy:
        return self._policy

    @property
    def event_logger(self) -> SecurityEventLogger:
        return self._event_logger

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def trust_level(self) -> TrustLevel:
        return self._trust_level

    @property
    def work_dir(self) -> str:
        return self._filesystem_layer.work_dir

    def _resolve_trust_level(self) -> TrustLevel:
        try:
            return TrustLevel(self._policy.trust_level)
        except ValueError:
            return TrustLevel.UNTRUSTED

    def validate_trust_level(self) -> bool:
        policy = self._policy
        trust = self._trust_level
        if trust == TrustLevel.UNTRUSTED:
            if len(policy.import_policy.blocked_modules) < _MIN_BLOCKED_MODULES_UNTRUSTED:
                return False
            if policy.resource_policy.max_cpu_seconds > _MAX_CPU_SECONDS_UNTRUSTED:
                return False
            if policy.filesystem_policy.read_write_paths:
                return False
        elif trust == TrustLevel.TRUSTED_LIMITED:
            if len(policy.import_policy.blocked_modules) < _MIN_BLOCKED_MODULES_LIMITED:
                return False
            if policy.resource_policy.max_cpu_seconds > _MAX_CPU_SECONDS_LIMITED:
                return False
        return True

    def activate(self) -> None:
        if self._active:
            return
        try:
            self._network_layer.install()
            self._resource_layer.install()
            self._filesystem_layer.install()
            self._introspection_layer.install()
            self._import_layer.install()
            self._active = True
            if not self.validate_trust_level():
                self._event_logger.log_event(
                    category=SandboxViolationCategory.INTROSPECTION,
                    detail=f"Trust level policy validation failed for {self._policy.trust_level}",
                    attempted_action="trust_level_validation",
                )
        except Exception:
            self._force_deactivate()
            raise

    def deactivate(self) -> None:
        if not self._active:
            return
        self._force_deactivate()

    def _force_deactivate(self) -> None:
        self._import_layer.uninstall()
        self._introspection_layer.uninstall()
        self._filesystem_layer.uninstall()
        self._resource_layer.uninstall()
        self._network_layer.uninstall()
        self._collect_violations()
        self._active = False

    def _collect_violations(self) -> None:
        all_violations: list[Any] = []
        for v in self._import_layer.get_violations():
            self._event_logger.log_violation(v)
            all_violations.append(v)
        self._import_layer.clear_violations()
        for v in self._network_layer.get_violations():
            self._event_logger.log_violation(v)
            all_violations.append(v)
        self._network_layer.clear_violations()
        for v in self._resource_layer.get_violations():
            self._event_logger.log_violation(v)
            all_violations.append(v)
        self._resource_layer.clear_violations()
        for v in self._filesystem_layer.get_violations():
            self._event_logger.log_violation(v)
            all_violations.append(v)
        self._filesystem_layer.clear_violations()
        for v in self._introspection_layer.get_violations():
            self._event_logger.log_violation(v)
            all_violations.append(v)
        self._introspection_layer.clear_violations()
        if self._metrics_collector is not None and all_violations:
            for _ in all_violations:
                self._metrics_collector.record_violation(self._policy.plugin_id)

    def cleanup(self) -> None:
        self.deactivate()
        self._filesystem_layer.cleanup()

    def __enter__(self) -> SandboxContext:
        self.activate()
        return self

    def __exit__(self, *args: Any) -> None:
        self.deactivate()
