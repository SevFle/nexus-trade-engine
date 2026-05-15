from __future__ import annotations

import time
import traceback
from dataclasses import dataclass
from typing import Any

from engine.plugins.sandbox.core.violation import (
    SandboxViolation,
    SandboxViolationCategory,
)


@dataclass
class SecurityEvent:
    timestamp: float
    category: SandboxViolationCategory
    detail: str
    plugin_id: str | None
    attempted_action: str | None
    stack_trace: str | None


class SecurityEventLogger:
    def __init__(self, plugin_id: str | None = None) -> None:
        self._plugin_id = plugin_id
        self._events: list[SecurityEvent] = []

    def log_violation(self, violation: SandboxViolation) -> None:
        event = SecurityEvent(
            timestamp=time.time(),
            category=violation.category,
            detail=violation.detail,
            plugin_id=violation.plugin_id or self._plugin_id,
            attempted_action=violation.attempted_action,
            stack_trace=traceback.format_stack(),
        )
        self._events.append(event)

    def log_event(
        self,
        category: SandboxViolationCategory,
        detail: str,
        attempted_action: str | None = None,
    ) -> None:
        event = SecurityEvent(
            timestamp=time.time(),
            category=category,
            detail=detail,
            plugin_id=self._plugin_id,
            attempted_action=attempted_action,
            stack_trace=traceback.format_stack(),
        )
        self._events.append(event)

    def get_events(
        self,
        category: SandboxViolationCategory | None = None,
        limit: int = 100,
    ) -> list[SecurityEvent]:
        events = self._events
        if category is not None:
            events = [e for e in events if e.category == category]
        return events[-limit:]

    def get_events_since(self, since: float, limit: int = 100) -> list[SecurityEvent]:
        return [e for e in self._events if e.timestamp >= since][-limit:]

    def clear(self) -> None:
        self._events.clear()

    @property
    def event_count(self) -> int:
        return len(self._events)

    def to_dicts(self, limit: int = 100) -> list[dict[str, Any]]:
        return [
            {
                "timestamp": e.timestamp,
                "category": e.category.value,
                "detail": e.detail,
                "plugin_id": e.plugin_id,
                "attempted_action": e.attempted_action,
            }
            for e in self.get_events(limit=limit)
        ]
