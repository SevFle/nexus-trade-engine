from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
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


def _build_introspection_policy(data: dict[str, Any]) -> IntrospectionPolicy:
    bb = data.get("blocked_builtins")
    ba = data.get("blocked_attributes")
    return IntrospectionPolicy(
        blocked_builtins=set(bb) if bb is not None else None,
        blocked_attributes=set(ba) if ba is not None else None,
        block_gc=data.get("block_gc", True),
        block_inspect=data.get("block_inspect", True),
        block_frame_access=data.get("block_frame_access", True),
    )


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

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SandboxPolicy:
        import_data = data.get("import_policy", {})
        network_data = data.get("network_policy", {})
        resource_data = data.get("resource_policy", {})
        fs_data = data.get("filesystem_policy", {})
        intro_data = data.get("introspection_policy", {})

        return cls(
            plugin_id=data.get("plugin_id", "unknown"),
            trust_level=data.get("trust_level", "untrusted"),
            import_policy=ImportPolicy(
                allowed_modules=set(import_data.get("allowed_modules", [])),
                blocked_modules=set(import_data.get("blocked_modules", [])),
            ),
            network_policy=NetworkPolicy(
                allowed_endpoints=network_data.get("allowed_endpoints", []),
                allowed_cidrs=network_data.get("allowed_cidrs", []),
                allowed_ports=set(network_data.get("allowed_ports", [])),
                block_dns=network_data.get("block_dns", True),
            ),
            resource_policy=ResourcePolicy(
                max_cpu_seconds=float(resource_data.get("max_cpu_seconds", 30.0)),
                max_memory_bytes=int(resource_data.get("max_memory_bytes", 512 * 1024 * 1024)),
                max_file_descriptors=int(resource_data.get("max_file_descriptors", 64)),
                max_threads=int(resource_data.get("max_threads", 1)),
                wall_time_seconds=float(resource_data.get("wall_time_seconds", 60.0)),
            ),
            filesystem_policy=FilesystemPolicy(
                read_only_paths=fs_data.get("read_only_paths", []),
                read_write_paths=fs_data.get("read_write_paths", []),
                virtual_root=fs_data.get("virtual_root"),
                block_symlinks=fs_data.get("block_symlinks", True),
                block_absolute_paths=fs_data.get("block_absolute_paths", True),
            ),
            introspection_policy=_build_introspection_policy(intro_data),
        )

    @classmethod
    def from_json(cls, json_str: str) -> SandboxPolicy:
        return cls.from_dict(json.loads(json_str))

    @classmethod
    def from_json_file(cls, path: str | Path) -> SandboxPolicy:
        return cls.from_dict(json.loads(Path(path).read_text()))

    @classmethod
    def from_yaml(cls, yaml_str: str) -> SandboxPolicy:
        import yaml  # noqa: PLC0415

        return cls.from_dict(yaml.safe_load(yaml_str))

    @classmethod
    def from_yaml_file(cls, path: str | Path) -> SandboxPolicy:
        import yaml  # noqa: PLC0415

        with Path(path).open() as f:
            return cls.from_dict(yaml.safe_load(f))

    def to_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "trust_level": self.trust_level,
            "import_policy": {
                "allowed_modules": sorted(self.import_policy.allowed_modules),
                "blocked_modules": sorted(self.import_policy.blocked_modules),
            },
            "network_policy": {
                "allowed_endpoints": self.network_policy.allowed_endpoints,
                "allowed_cidrs": self.network_policy.allowed_cidrs,
                "allowed_ports": sorted(self.network_policy.allowed_ports),
                "block_dns": self.network_policy.block_dns,
            },
            "resource_policy": {
                "max_cpu_seconds": self.resource_policy.max_cpu_seconds,
                "max_memory_bytes": self.resource_policy.max_memory_bytes,
                "max_file_descriptors": self.resource_policy.max_file_descriptors,
                "max_threads": self.resource_policy.max_threads,
                "wall_time_seconds": self.resource_policy.wall_time_seconds,
            },
            "filesystem_policy": {
                "read_only_paths": self.filesystem_policy.read_only_paths,
                "read_write_paths": self.filesystem_policy.read_write_paths,
                "virtual_root": self.filesystem_policy.virtual_root,
                "block_symlinks": self.filesystem_policy.block_symlinks,
                "block_absolute_paths": self.filesystem_policy.block_absolute_paths,
            },
            "introspection_policy": {
                "blocked_builtins": sorted(self.introspection_policy.blocked_builtins),
                "blocked_attributes": sorted(self.introspection_policy.blocked_attributes),
                "block_gc": self.introspection_policy.block_gc,
                "block_inspect": self.introspection_policy.block_inspect,
                "block_frame_access": self.introspection_policy.block_frame_access,
            },
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


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
