from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from engine.plugins.trust_levels import TrustLevel, get_trust_level, get_trust_policy


@dataclass
class ImportPolicy:
    allowed_modules: set[str] = field(default_factory=set)
    blocked_modules: set[str] = field(default_factory=set)
    blocked_categories: dict[str, set[str]] = field(default_factory=dict)

    def is_allowed(self, module_name: str) -> bool:
        root = module_name.split(".", maxsplit=1)[0]
        return not (
            root in self.blocked_modules
            or (self.allowed_modules and root not in self.allowed_modules)
        )


@dataclass
class NetworkPolicy:
    allowed_endpoints: list[str] = field(default_factory=list)
    allowed_cidrs: list[str] = field(default_factory=list)
    allowed_ports: set[int] = field(default_factory=set)
    block_dns: bool = True
    allowed_dns_servers: list[str] = field(default_factory=list)

    def is_host_allowed(self, host: str) -> bool:
        if not self.allowed_endpoints:
            return False
        return any(host == ep or host.endswith(f".{ep}") for ep in self.allowed_endpoints)


@dataclass
class ResourcePolicy:
    max_cpu_seconds: float = 30.0
    max_memory_bytes: int = 512 * 1024 * 1024
    max_file_descriptors: int = 64
    max_threads: int = 1
    wall_time_seconds: float = 60.0


@dataclass
class FilesystemPolicy:
    read_only_paths: list[str] = field(default_factory=list)
    read_write_paths: list[str] = field(default_factory=list)
    virtual_root: str | None = None
    block_symlinks: bool = True
    block_absolute_paths: bool = True


@dataclass
class IntrospectionPolicy:
    blocked_builtins: set[str] = field(
        default_factory=lambda: {
            "eval",
            "exec",
            "compile",
            "breakpoint",
            "credits",
            "license",
            "quit",
            "exit",
            "vars",
            "globals",
            "locals",
        }
    )
    blocked_attributes: set[str] = field(
        default_factory=lambda: {
            "__subclasses__",
            "__bases__",
            "__mro__",
            "__globals__",
            "__closure__",
            "__code__",
            "__dict__",
            "__class__",
        }
    )
    blocked_dunder_access: bool = True
    block_gc: bool = True
    block_inspect: bool = True
    block_frame_access: bool = True


def _get_full_blocked_modules() -> set[str]:
    try:
        from engine.plugins.restricted_importer import BLOCKED_MODULES  # noqa: PLC0415

        return set(BLOCKED_MODULES)
    except ImportError:
        return {
            "os", "subprocess", "shutil", "pathlib", "io", "_io",
            "socket", "_socket", "http", "urllib", "ftplib", "smtplib",
            "ctypes", "_ctypes", "multiprocessing", "signal", "sys",
            "importlib", "threading", "_thread", "concurrent", "gc",
            "inspect", "code", "codeop", "ast", "dis", "pkgutil",
            "zipimport", "runpy", "pickle", "shelve", "marshal",
            "atexit", "sched", "pty", "tty", "pdb", "bdb", "site",
        }


_TRUST_IMPORT_PRESETS: dict[TrustLevel, set[str]] = {
    TrustLevel.TRUSTED_FULL: {"subprocess", "ctypes", "_ctypes"},
    TrustLevel.TRUSTED_LIMITED: _get_full_blocked_modules(),
    TrustLevel.UNTRUSTED: _get_full_blocked_modules(),
}

_TRUST_INTROSPECTION_PRESETS: dict[TrustLevel, IntrospectionPolicy] = {
    TrustLevel.TRUSTED_FULL: IntrospectionPolicy(
        blocked_builtins={"exec", "compile"},
        blocked_attributes={"__subclasses__", "__globals__"},
    ),
    TrustLevel.TRUSTED_LIMITED: IntrospectionPolicy(
        blocked_builtins={"eval", "exec", "compile", "breakpoint"},
        blocked_attributes={"__subclasses__", "__globals__", "__bases__", "__mro__"},
    ),
    TrustLevel.UNTRUSTED: IntrospectionPolicy(),
}

_TRUST_RESOURCE_MULTIPLIERS: dict[TrustLevel, float] = {
    TrustLevel.TRUSTED_FULL: 4.0,
    TrustLevel.TRUSTED_LIMITED: 2.0,
    TrustLevel.UNTRUSTED: 1.0,
}

_TRUST_FILESYSTEM_RW: dict[TrustLevel, bool] = {
    TrustLevel.TRUSTED_FULL: True,
    TrustLevel.TRUSTED_LIMITED: True,
    TrustLevel.UNTRUSTED: False,
}


@dataclass
class SandboxPolicy:
    plugin_id: str = "unknown"
    trust_level: str = "untrusted"
    import_policy: ImportPolicy = field(default_factory=ImportPolicy)
    network_policy: NetworkPolicy = field(default_factory=NetworkPolicy)
    resource_policy: ResourcePolicy = field(default_factory=ResourcePolicy)
    filesystem_policy: FilesystemPolicy = field(default_factory=FilesystemPolicy)
    introspection_policy: IntrospectionPolicy = field(default_factory=IntrospectionPolicy)

    @classmethod
    def from_manifest(cls, manifest: Any) -> SandboxPolicy:
        trust = get_trust_level(manifest)
        trust_dict = get_trust_policy(trust)
        multiplier = trust_dict.get("resource_multiplier", 1.0)

        import_blocked = _TRUST_IMPORT_PRESETS[trust]

        network_endpoints: list[str] = []
        if (
            hasattr(manifest, "network")
            and hasattr(manifest, "requires_network")
            and manifest.requires_network()
        ):
            network_endpoints = manifest.network.allowed_endpoints

        base_cpu_seconds = 30
        max_memory_str = "512MB"
        if hasattr(manifest, "resources"):
            base_cpu_seconds = manifest.resources.max_cpu_seconds
            max_memory_str = manifest.resources.max_memory

        memory_bytes = _parse_memory(max_memory_str)

        artifacts: list[str] = []
        if hasattr(manifest, "artifacts"):
            artifacts = list(manifest.artifacts)

        rw_paths: list[str] = []
        if (
            _TRUST_FILESYSTEM_RW[trust]
            and hasattr(manifest, "permissions")
            and hasattr(manifest, "has_permission")
            and manifest.has_permission("filesystem_write")
        ):
            rw_paths = artifacts

        return cls(
            plugin_id=getattr(manifest, "id", "unknown"),
            trust_level=trust.value,
            import_policy=ImportPolicy(blocked_modules=import_blocked),
            network_policy=NetworkPolicy(allowed_endpoints=network_endpoints),
            resource_policy=ResourcePolicy(
                max_cpu_seconds=base_cpu_seconds * multiplier,
                max_memory_bytes=int(memory_bytes * multiplier),
            ),
            filesystem_policy=FilesystemPolicy(
                read_only_paths=artifacts,
                read_write_paths=rw_paths,
            ),
            introspection_policy=_TRUST_INTROSPECTION_PRESETS[trust],
        )

    @classmethod
    def from_trust_level(
        cls,
        trust_level: TrustLevel,
        plugin_id: str = "unknown",
        *,
        network_endpoints: list[str] | None = None,
        max_cpu_seconds: float = 30.0,
        max_memory_bytes: int = 512 * 1024 * 1024,
        read_only_paths: list[str] | None = None,
    ) -> SandboxPolicy:
        multiplier = _TRUST_RESOURCE_MULTIPLIERS[trust_level]
        return cls(
            plugin_id=plugin_id,
            trust_level=trust_level.value,
            import_policy=ImportPolicy(blocked_modules=_TRUST_IMPORT_PRESETS[trust_level]),
            network_policy=NetworkPolicy(allowed_endpoints=network_endpoints or []),
            resource_policy=ResourcePolicy(
                max_cpu_seconds=max_cpu_seconds * multiplier,
                max_memory_bytes=int(max_memory_bytes * multiplier),
            ),
            filesystem_policy=FilesystemPolicy(read_only_paths=read_only_paths or []),
            introspection_policy=_TRUST_INTROSPECTION_PRESETS[trust_level],
        )

    @classmethod
    def trusted_policy(cls, plugin_id: str = "trusted") -> SandboxPolicy:
        return cls(
            plugin_id=plugin_id,
            trust_level="trusted",
            import_policy=ImportPolicy(blocked_modules={"subprocess", "ctypes", "_ctypes"}),
            resource_policy=ResourcePolicy(max_cpu_seconds=300, max_memory_bytes=2 * 1024**3),
            introspection_policy=IntrospectionPolicy(
                blocked_builtins={"exec", "compile"},
                blocked_attributes={"__subclasses__", "__globals__"},
            ),
        )


def _parse_memory(mem_str: str) -> int:
    val = mem_str.strip().upper()
    units: dict[str, int] = {
        "GB": 1024**3,
        "MB": 1024**2,
        "KB": 1024,
        "B": 1,
    }
    for suffix, multiplier in sorted(units.items(), key=lambda x: -len(x[0])):
        if val.endswith(suffix):
            return int(float(val[: -len(suffix)]) * multiplier)
    return int(val)
