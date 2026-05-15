from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from engine.plugins.sandbox.core.policy import SandboxPolicy

from engine.plugins.sandbox.layers import (
    FilesystemIsolation,
    IntrospectionGuard,
    NetworkGuard,
    ResourceLimiter,
    RestrictedImporter,
)
from engine.plugins.sandbox.monitoring.event_logger import SecurityEventLogger


class SandboxContext:
    def __init__(self, policy: SandboxPolicy) -> None:
        self._policy = policy
        self._event_logger = SecurityEventLogger(plugin_id=policy.plugin_id)
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
    def work_dir(self) -> str:
        return self._filesystem_layer.work_dir

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
        for v in self._import_layer.get_violations():
            self._event_logger.log_violation(v)
        self._import_layer.clear_violations()
        for v in self._network_layer.get_violations():
            self._event_logger.log_violation(v)
        self._network_layer.clear_violations()
        for v in self._resource_layer.get_violations():
            self._event_logger.log_violation(v)
        self._resource_layer.clear_violations()
        for v in self._filesystem_layer.get_violations():
            self._event_logger.log_violation(v)
        self._filesystem_layer.clear_violations()
        for v in self._introspection_layer.get_violations():
            self._event_logger.log_violation(v)
        self._introspection_layer.clear_violations()

    def cleanup(self) -> None:
        self.deactivate()
        self._filesystem_layer.cleanup()

    def __enter__(self) -> SandboxContext:
        self.activate()
        return self

    def __exit__(self, *args: Any) -> None:
        self.deactivate()
