from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from engine.plugins.sandbox.monitoring.event_logger import SecurityEvent


class ViolationReport:
    def __init__(self, plugin_id: str | None = None) -> None:
        self.plugin_id = plugin_id
        self.generated_at: float = time.time()
        self.total_violations: int = 0
        self.by_category: dict[str, int] = {}
        self.by_layer: dict[str, list[dict[str, Any]]] = {
            "import": [],
            "network": [],
            "resource": [],
            "filesystem": [],
            "introspection": [],
        }

    @classmethod
    def from_events(
        cls,
        events: list[SecurityEvent],
        plugin_id: str | None = None,
    ) -> ViolationReport:
        report = cls(plugin_id=plugin_id)
        report.total_violations = len(events)
        for event in events:
            cat = event.category.value
            report.by_category[cat] = report.by_category.get(cat, 0) + 1
            entry: dict[str, Any] = {
                "timestamp": event.timestamp,
                "detail": event.detail,
                "attempted_action": event.attempted_action,
                "plugin_id": event.plugin_id,
            }
            report.by_layer[cat].append(entry)
        return report

    def to_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "generated_at": self.generated_at,
            "total_violations": self.total_violations,
            "by_category": self.by_category,
            "by_layer": self.by_layer,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)

    def summary(self) -> str:
        lines = [
            f"Violation Report for plugin: {self.plugin_id or 'all'}",
            "Generated at: "
            f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.generated_at))}",
            f"Total violations: {self.total_violations}",
        ]
        if self.by_category:
            lines.append("By category:")
            for cat, count in sorted(self.by_category.items()):
                lines.append(f"  {cat}: {count}")
        return "\n".join(lines)
