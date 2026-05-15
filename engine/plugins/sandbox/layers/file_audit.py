from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from engine.plugins.sandbox.core.violation import FilesystemViolation


class FileAuditLog:
    def __init__(self, plugin_id: str | None = None) -> None:
        self._plugin_id = plugin_id
        self._entries: list[dict[str, Any]] = []

    def log_access(self, path: str, operation: str, allowed: bool) -> None:
        self._entries.append({
            "timestamp": time.time(),
            "plugin_id": self._plugin_id,
            "path": path,
            "operation": operation,
            "allowed": allowed,
        })

    def log_violation(self, violation: FilesystemViolation) -> None:
        self._entries.append({
            "timestamp": time.time(),
            "plugin_id": violation.plugin_id or self._plugin_id,
            "path": violation.path,
            "operation": violation.operation,
            "allowed": False,
            "detail": violation.detail,
        })

    def get_entries(
        self,
        path_prefix: str | None = None,
        operation: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        entries = self._entries
        if path_prefix is not None:
            entries = [e for e in entries if e["path"].startswith(path_prefix)]
        if operation is not None:
            entries = [e for e in entries if e["operation"] == operation]
        return entries[-limit:]

    def get_all_entries(self) -> list[dict[str, Any]]:
        return list(self._entries)

    def clear(self) -> None:
        self._entries.clear()

    @property
    def entry_count(self) -> int:
        return len(self._entries)
