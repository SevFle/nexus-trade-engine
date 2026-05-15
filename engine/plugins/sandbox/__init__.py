from engine.plugins.sandbox._sandbox import SandboxMetrics, StrategySandbox

__all__ = [
    "SandboxMetrics",
    "StrategySandbox",
]


def __getattr__(name: str):
    lazy_exports = {
        "AttributeGuard": "engine.plugins.sandbox.layers.attribute_guard",
        "DnsGuard": "engine.plugins.sandbox.layers.dns_guard",
        "FdTracker": "engine.plugins.sandbox.layers.fd_tracker",
        "FileAuditLog": "engine.plugins.sandbox.layers.file_audit",
        "FilesystemIsolation": "engine.plugins.sandbox.layers.filesystem_isolation",
        "FilesystemPolicy": "engine.plugins.sandbox.core.policy",
        "FilesystemViolation": "engine.plugins.sandbox.core.violation",
        "ImportPolicy": "engine.plugins.sandbox.core.policy",
        "ImportViolation": "engine.plugins.sandbox.core.violation",
        "IntrospectionGuard": "engine.plugins.sandbox.layers.introspection_guard",
        "IntrospectionPolicy": "engine.plugins.sandbox.core.policy",
        "IntrospectionViolation": "engine.plugins.sandbox.core.violation",
        "MemoryGuard": "engine.plugins.sandbox.layers.memory_guard",
        "NetworkGuard": "engine.plugins.sandbox.layers.network_guard",
        "NetworkPolicy": "engine.plugins.sandbox.core.policy",
        "NetworkViolation": "engine.plugins.sandbox.core.violation",
        "PathResolver": "engine.plugins.sandbox.layers.path_resolver",
        "PathValidator": "engine.plugins.sandbox.layers.path_validator",
        "PluginMetrics": "engine.plugins.sandbox.monitoring.metrics",
        "PluginSandboxExecutor": "engine.plugins.sandbox.executor",
        "ResourceExhausted": "engine.plugins.sandbox.core.violation",
        "ResourceLimiter": "engine.plugins.sandbox.layers.resource_limiter",
        "ResourcePolicy": "engine.plugins.sandbox.core.policy",
        "RestrictedImporter": "engine.plugins.sandbox.layers.import_restriction",
        "SandboxAdminAPI": "engine.plugins.sandbox.monitoring.admin_api",
        "SandboxContext": "engine.plugins.sandbox.core.context",
        "SandboxLifecycle": "engine.plugins.sandbox.core.lifecycle",
        "SandboxMetricsCollector": "engine.plugins.sandbox.monitoring.metrics",
        "SandboxPolicy": "engine.plugins.sandbox.core.policy",
        "SandboxViolation": "engine.plugins.sandbox.core.violation",
        "SandboxViolationCategory": "engine.plugins.sandbox.core.violation",
        "SecurityEvent": "engine.plugins.sandbox.monitoring.event_logger",
        "SecurityEventLogger": "engine.plugins.sandbox.monitoring.event_logger",
        "VirtualPathResolver": "engine.plugins.sandbox.layers.virtual_path",
        "ViolationReport": "engine.plugins.sandbox.monitoring.violation_report",
    }
    if name in lazy_exports:
        import importlib  # noqa: PLC0415

        mod = importlib.import_module(lazy_exports[name])
        value = getattr(mod, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
