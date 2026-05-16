from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from engine.plugins.sandbox.core.context import SandboxContext
    from engine.plugins.sandbox.core.policy import SandboxPolicy
    from engine.plugins.sandbox.core.violation import SandboxViolationCategory

    from .event_logger import SecurityEvent
    from .metrics import SandboxMetricsCollector
    from .violation_report import ViolationReport

logger = structlog.get_logger()


@dataclass
class PolicyUpdate:
    plugin_id: str
    updated_fields: dict[str, Any]
    applied_at: float = field(default_factory=time.time)
    previous_values: dict[str, Any] = field(default_factory=dict)


@dataclass
class PolicySnapshot:
    plugin_id: str
    trust_level: str
    blocked_modules: set[str]
    allowed_endpoints: list[str]
    max_cpu_seconds: float
    max_memory_bytes: int
    max_file_descriptors: int
    max_threads: int
    wall_time_seconds: float
    read_only_paths: list[str]
    read_write_paths: list[str]
    snapshot_at: float = field(default_factory=time.time)

    @classmethod
    def from_policy(cls, policy: SandboxPolicy) -> PolicySnapshot:
        return cls(
            plugin_id=policy.plugin_id,
            trust_level=policy.trust_level,
            blocked_modules=set(policy.import_policy.blocked_modules),
            allowed_endpoints=list(policy.network_policy.allowed_endpoints),
            max_cpu_seconds=policy.resource_policy.max_cpu_seconds,
            max_memory_bytes=policy.resource_policy.max_memory_bytes,
            max_file_descriptors=policy.resource_policy.max_file_descriptors,
            max_threads=policy.resource_policy.max_threads,
            wall_time_seconds=policy.resource_policy.wall_time_seconds,
            read_only_paths=list(policy.filesystem_policy.read_only_paths),
            read_write_paths=list(policy.filesystem_policy.read_write_paths),
        )


class SandboxAdminAPI:
    def __init__(
        self,
        metrics_collector: SandboxMetricsCollector,
    ) -> None:
        self._metrics = metrics_collector
        self._contexts: dict[str, SandboxContext] = {}
        self._policy_history: dict[str, list[PolicyUpdate]] = {}
        self._policy_snapshots: dict[str, PolicySnapshot] = {}

    def register_context(self, context: SandboxContext) -> None:
        plugin_id = context.policy.plugin_id
        self._contexts[plugin_id] = context
        self._policy_snapshots[plugin_id] = PolicySnapshot.from_policy(context.policy)
        logger.info("admin.context_registered", plugin_id=plugin_id)

    def unregister_context(self, plugin_id: str) -> None:
        self._contexts.pop(plugin_id, None)
        self._policy_snapshots.pop(plugin_id, None)
        logger.info("admin.context_unregistered", plugin_id=plugin_id)

    def get_security_events(
        self,
        plugin_id: str | None = None,
        category: SandboxViolationCategory | None = None,
        since: float | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if plugin_id and plugin_id in self._contexts:
            ctx = self._contexts[plugin_id]
            if since is not None:
                events = ctx.event_logger.get_events_since(since, limit=limit)
            else:
                events = ctx.event_logger.get_events(category=category, limit=limit)
            return self._events_to_dicts(events)

        all_events: list[SecurityEvent] = []
        for ctx in self._contexts.values():
            if since is not None:
                evts = ctx.event_logger.get_events_since(since, limit=limit)
            else:
                evts = ctx.event_logger.get_events(category=category, limit=limit)
            all_events.extend(evts)
        all_events.sort(key=lambda e: e.timestamp, reverse=True)
        return self._events_to_dicts(all_events[:limit])

    def get_plugin_metrics(self, plugin_id: str) -> dict[str, Any] | None:
        return self._metrics.get_plugin_metrics(plugin_id)

    def get_all_metrics(self) -> dict[str, dict[str, Any]]:
        return self._metrics.get_all_metrics()

    def get_policy(self, plugin_id: str) -> dict[str, Any] | None:
        snapshot = self._policy_snapshots.get(plugin_id)
        if snapshot is None:
            return None
        return {
            "plugin_id": snapshot.plugin_id,
            "trust_level": snapshot.trust_level,
            "import_policy": {
                "blocked_modules": sorted(snapshot.blocked_modules),
            },
            "network_policy": {
                "allowed_endpoints": snapshot.allowed_endpoints,
            },
            "resource_policy": {
                "max_cpu_seconds": snapshot.max_cpu_seconds,
                "max_memory_bytes": snapshot.max_memory_bytes,
                "max_file_descriptors": snapshot.max_file_descriptors,
                "max_threads": snapshot.max_threads,
                "wall_time_seconds": snapshot.wall_time_seconds,
            },
            "filesystem_policy": {
                "read_only_paths": snapshot.read_only_paths,
                "read_write_paths": snapshot.read_write_paths,
            },
            "snapshot_at": snapshot.snapshot_at,
        }

    def update_policy(
        self,
        plugin_id: str,
        updates: dict[str, Any],
    ) -> PolicyUpdate | None:
        context = self._contexts.get(plugin_id)
        if context is None:
            logger.warning("admin.update_policy_unknown_plugin", plugin_id=plugin_id)
            return None

        if context.is_active:
            logger.warning(
                "admin.update_policy_active_sandbox",
                plugin_id=plugin_id,
            )
            return None

        policy = context.policy
        previous: dict[str, Any] = {}
        applied: dict[str, Any] = {}

        field_map: dict[str, tuple[Any, Any]] = {
            "max_cpu_seconds": (policy.resource_policy, "max_cpu_seconds"),
            "max_memory_bytes": (policy.resource_policy, "max_memory_bytes"),
            "max_file_descriptors": (policy.resource_policy, "max_file_descriptors"),
            "max_threads": (policy.resource_policy, "max_threads"),
            "wall_time_seconds": (policy.resource_policy, "wall_time_seconds"),
            "block_dns": (policy.network_policy, "block_dns"),
        }

        for key, value in updates.items():
            if key in field_map:
                obj, attr = field_map[key]
                previous[key] = getattr(obj, attr)
                setattr(obj, attr, value)
                applied[key] = value

        if "allowed_endpoints" in updates:
            previous["allowed_endpoints"] = list(policy.network_policy.allowed_endpoints)
            policy.network_policy.allowed_endpoints = updates["allowed_endpoints"]
            applied["allowed_endpoints"] = updates["allowed_endpoints"]

        if "blocked_modules" in updates:
            previous["blocked_modules"] = set(policy.import_policy.blocked_modules)
            policy.import_policy.blocked_modules = set(updates["blocked_modules"])
            applied["blocked_modules"] = updates["blocked_modules"]

        if "read_only_paths" in updates:
            previous["read_only_paths"] = list(policy.filesystem_policy.read_only_paths)
            policy.filesystem_policy.read_only_paths = list(updates["read_only_paths"])
            applied["read_only_paths"] = updates["read_only_paths"]

        if "read_write_paths" in updates:
            previous["read_write_paths"] = list(policy.filesystem_policy.read_write_paths)
            policy.filesystem_policy.read_write_paths = list(updates["read_write_paths"])
            applied["read_write_paths"] = updates["read_write_paths"]

        update_record = PolicyUpdate(
            plugin_id=plugin_id,
            updated_fields=applied,
            previous_values=previous,
        )

        if plugin_id not in self._policy_history:
            self._policy_history[plugin_id] = []
        self._policy_history[plugin_id].append(update_record)

        self._policy_snapshots[plugin_id] = PolicySnapshot.from_policy(policy)

        logger.info(
            "admin.policy_updated",
            plugin_id=plugin_id,
            fields=list(applied.keys()),
        )

        return update_record

    def get_policy_history(
        self,
        plugin_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        history = self._policy_history.get(plugin_id, [])
        return [
            {
                "plugin_id": u.plugin_id,
                "updated_fields": u.updated_fields,
                "previous_values": u.previous_values,
                "applied_at": u.applied_at,
            }
            for u in history[-limit:]
        ]

    def get_violation_report(
        self,
        plugin_id: str | None = None,
    ) -> ViolationReport:
        from engine.plugins.sandbox.monitoring.violation_report import ViolationReport

        all_events: list[SecurityEvent] = []
        contexts = (
            {plugin_id: self._contexts[plugin_id]}
            if plugin_id and plugin_id in self._contexts
            else self._contexts
        )
        for ctx in contexts.values():
            all_events.extend(ctx.event_logger.get_events(limit=10000))

        trust_level = None
        if plugin_id and plugin_id in self._contexts:
            trust_level = self._contexts[plugin_id].policy.trust_level

        return ViolationReport.from_events(
            events=all_events,
            plugin_id=plugin_id,
            trust_level=trust_level,
        )

    def list_plugins(self) -> list[dict[str, Any]]:
        results = []
        for plugin_id, ctx in self._contexts.items():
            results.append({
                "plugin_id": plugin_id,
                "trust_level": ctx.policy.trust_level,
                "is_active": ctx.is_active,
                "work_dir": ctx.work_dir,
                "violation_count": ctx.event_logger.event_count,
            })
        return results

    @staticmethod
    def _events_to_dicts(events: list[SecurityEvent]) -> list[dict[str, Any]]:
        return [
            {
                "timestamp": e.timestamp,
                "category": e.category.value,
                "detail": e.detail,
                "plugin_id": e.plugin_id,
                "attempted_action": e.attempted_action,
            }
            for e in events
        ]
