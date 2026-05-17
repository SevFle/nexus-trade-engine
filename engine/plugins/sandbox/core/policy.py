from __future__ import annotations

import hashlib
import json
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
    block_metadata_endpoints: bool = True
    max_connections_per_host: int = 100

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
    block_env_access: bool = True


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
            "__builtins__",
            "__func__",
            "__self__",
        }
    )
    blocked_dunder_access: bool = True
    block_gc: bool = True
    block_inspect: bool = True
    block_frame_access: bool = True
    block_type_abuse: bool = True


@dataclass
class EnvironmentPolicy:
    allowed_env_vars: set[str] = field(default_factory=set)
    block_os_environ: bool = True
    sanitized_env: dict[str, str] = field(default_factory=dict)


_TRUST_ENVIRONMENT_PRESETS: dict[TrustLevel, EnvironmentPolicy] = {
    TrustLevel.TRUSTED_FULL: EnvironmentPolicy(
        allowed_env_vars={"HOME", "PATH", "LANG", "LC_ALL", "TZ"},
        block_os_environ=False,
    ),
    TrustLevel.TRUSTED_LIMITED: EnvironmentPolicy(
        allowed_env_vars={"HOME", "PATH", "LANG"},
        block_os_environ=True,
    ),
    TrustLevel.UNTRUSTED: EnvironmentPolicy(
        allowed_env_vars=set(),
        block_os_environ=True,
    ),
}


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

_TRUST_MAX_CPU_HARD_LIMITS: dict[TrustLevel, float] = {
    TrustLevel.TRUSTED_FULL: 600.0,
    TrustLevel.TRUSTED_LIMITED: 300.0,
    TrustLevel.UNTRUSTED: 120.0,
}

_TRUST_MAX_MEMORY_HARD_LIMITS: dict[TrustLevel, int] = {
    TrustLevel.TRUSTED_FULL: 4 * 1024**3,
    TrustLevel.TRUSTED_LIMITED: 2 * 1024**3,
    TrustLevel.UNTRUSTED: 1024**3,
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
    environment_policy: EnvironmentPolicy = field(default_factory=EnvironmentPolicy)
    _integrity_hash: str | None = field(default=None, repr=False)

    def compute_integrity_hash(self) -> str:
        data = json.dumps(
            {
                "plugin_id": self.plugin_id,
                "trust_level": self.trust_level,
                "blocked_modules": sorted(self.import_policy.blocked_modules),
                "max_cpu_seconds": self.resource_policy.max_cpu_seconds,
                "max_memory_bytes": self.resource_policy.max_memory_bytes,
                "max_file_descriptors": self.resource_policy.max_file_descriptors,
                "max_threads": self.resource_policy.max_threads,
                "wall_time_seconds": self.resource_policy.wall_time_seconds,
                "allowed_endpoints": sorted(self.network_policy.allowed_endpoints),
                "read_only_paths": sorted(self.filesystem_policy.read_only_paths),
                "read_write_paths": sorted(self.filesystem_policy.read_write_paths),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(data.encode()).hexdigest()

    def set_integrity_hash(self) -> None:
        self._integrity_hash = self.compute_integrity_hash()

    def verify_integrity(self) -> bool:
        if self._integrity_hash is None:
            return True
        return self._integrity_hash == self.compute_integrity_hash()

    def enforce_hard_limits(self, trust: TrustLevel) -> list[str]:
        violations: list[str] = []
        max_cpu = _TRUST_MAX_CPU_HARD_LIMITS.get(trust, 120.0)
        max_mem = _TRUST_MAX_MEMORY_HARD_LIMITS.get(trust, 1024**3)
        if self.resource_policy.max_cpu_seconds > max_cpu:
            violations.append(
                f"max_cpu_seconds={self.resource_policy.max_cpu_seconds} exceeds hard limit {max_cpu}"
            )
        if self.resource_policy.max_memory_bytes > max_mem:
            violations.append(
                f"max_memory_bytes={self.resource_policy.max_memory_bytes} exceeds hard limit {max_mem}"
            )
        if trust == TrustLevel.UNTRUSTED:
            if self.filesystem_policy.read_write_paths:
                violations.append("untrusted plugins cannot have write paths")
            if self.resource_policy.max_threads > 1:
                violations.append("untrusted plugins cannot create threads")
            if not self.network_policy.block_metadata_endpoints:
                violations.append("untrusted plugins must block metadata endpoints")
        return violations

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

        cpu_with_mult = base_cpu_seconds * multiplier
        mem_with_mult = int(memory_bytes * multiplier)

        max_cpu_hard = _TRUST_MAX_CPU_HARD_LIMITS.get(trust, 120.0)
        max_mem_hard = _TRUST_MAX_MEMORY_HARD_LIMITS.get(trust, 1024**3)
        cpu_with_mult = min(cpu_with_mult, max_cpu_hard)
        mem_with_mult = min(mem_with_mult, max_mem_hard)

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

        env_policy = _TRUST_ENVIRONMENT_PRESETS.get(trust, EnvironmentPolicy())

        policy = cls(
            plugin_id=getattr(manifest, "id", "unknown"),
            trust_level=trust.value,
            import_policy=ImportPolicy(blocked_modules=import_blocked),
            network_policy=NetworkPolicy(allowed_endpoints=network_endpoints),
            resource_policy=ResourcePolicy(
                max_cpu_seconds=cpu_with_mult,
                max_memory_bytes=mem_with_mult,
            ),
            filesystem_policy=FilesystemPolicy(
                read_only_paths=artifacts,
                read_write_paths=rw_paths,
            ),
            introspection_policy=_TRUST_INTROSPECTION_PRESETS[trust],
            environment_policy=env_policy,
        )
        policy.set_integrity_hash()
        return policy

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

        cpu_with_mult = max_cpu_seconds * multiplier
        mem_with_mult = int(max_memory_bytes * multiplier)

        max_cpu_hard = _TRUST_MAX_CPU_HARD_LIMITS.get(trust_level, 120.0)
        max_mem_hard = _TRUST_MAX_MEMORY_HARD_LIMITS.get(trust_level, 1024**3)
        cpu_with_mult = min(cpu_with_mult, max_cpu_hard)
        mem_with_mult = min(mem_with_mult, max_mem_hard)

        env_policy = _TRUST_ENVIRONMENT_PRESETS.get(trust_level, EnvironmentPolicy())

        policy = cls(
            plugin_id=plugin_id,
            trust_level=trust_level.value,
            import_policy=ImportPolicy(blocked_modules=_TRUST_IMPORT_PRESETS[trust_level]),
            network_policy=NetworkPolicy(allowed_endpoints=network_endpoints or []),
            resource_policy=ResourcePolicy(
                max_cpu_seconds=cpu_with_mult,
                max_memory_bytes=mem_with_mult,
            ),
            filesystem_policy=FilesystemPolicy(read_only_paths=read_only_paths or []),
            introspection_policy=_TRUST_INTROSPECTION_PRESETS[trust_level],
            environment_policy=env_policy,
        )
        policy.set_integrity_hash()
        return policy

    @classmethod
    def trusted_policy(cls, plugin_id: str = "trusted") -> SandboxPolicy:
        policy = cls(
            plugin_id=plugin_id,
            trust_level="trusted_full",
            import_policy=ImportPolicy(blocked_modules={"subprocess", "ctypes", "_ctypes"}),
            resource_policy=ResourcePolicy(max_cpu_seconds=300, max_memory_bytes=2 * 1024**3),
            introspection_policy=IntrospectionPolicy(
                blocked_builtins={"exec", "compile"},
                blocked_attributes={"__subclasses__", "__globals__"},
            ),
            environment_policy=_TRUST_ENVIRONMENT_PRESETS[TrustLevel.TRUSTED_FULL],
        )
        policy.set_integrity_hash()
        return policy


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
