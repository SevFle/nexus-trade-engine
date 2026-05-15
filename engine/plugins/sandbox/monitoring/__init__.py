from __future__ import annotations

from engine.plugins.sandbox.monitoring.event_logger import SecurityEventLogger
from engine.plugins.sandbox.monitoring.metrics import SandboxMetricsCollector
from engine.plugins.sandbox.monitoring.violation_report import ViolationReport

__all__ = [
    "SandboxAdminAPI",
    "SandboxMetricsCollector",
    "SecurityEventLogger",
    "ViolationReport",
]


def __getattr__(name: str):
    if name == "SandboxAdminAPI":
        from engine.plugins.sandbox.monitoring.admin_api import SandboxAdminAPI  # noqa: PLC0415

        globals()["SandboxAdminAPI"] = SandboxAdminAPI
        return SandboxAdminAPI
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
