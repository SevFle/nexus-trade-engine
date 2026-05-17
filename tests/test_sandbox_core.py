"""Comprehensive tests for sandbox core modules: policy, violation, and context."""

from __future__ import annotations

from types import SimpleNamespace

from engine.plugins.sandbox.core.policy import (
    _TRUST_ENVIRONMENT_PRESETS,
    _TRUST_MAX_CPU_HARD_LIMITS,
    _TRUST_MAX_MEMORY_HARD_LIMITS,
    EnvironmentPolicy,
    FilesystemPolicy,
    ImportPolicy,
    IntrospectionPolicy,
    NetworkPolicy,
    ResourcePolicy,
    SandboxPolicy,
    _parse_memory,
)
from engine.plugins.sandbox.core.violation import (
    FilesystemViolation,
    ImportViolation,
    IntrospectionViolation,
    NetworkViolation,
    ResourceExhausted,
    SandboxViolationCategory,
)
from engine.plugins.trust_levels import TrustLevel


class TestImportPolicy:
    def test_default_allows_unblocked_module(self) -> None:
        policy = ImportPolicy()
        assert policy.is_allowed("json") is True

    def test_blocked_module_rejected(self) -> None:
        policy = ImportPolicy(blocked_modules={"os"})
        assert policy.is_allowed("os") is False

    def test_blocked_submodule_rejected(self) -> None:
        policy = ImportPolicy(blocked_modules={"os"})
        assert policy.is_allowed("os.path") is False

    def test_allowlist_blocks_non_member(self) -> None:
        policy = ImportPolicy(allowed_modules={"json"})
        assert policy.is_allowed("os") is False

    def test_allowlist_allows_member(self) -> None:
        policy = ImportPolicy(allowed_modules={"json"})
        assert policy.is_allowed("json") is True

    def test_empty_allowlist_allows_all(self) -> None:
        policy = ImportPolicy(allowed_modules=set())
        assert policy.is_allowed("anything") is True

    def test_blocked_takes_precedence(self) -> None:
        policy = ImportPolicy(allowed_modules={"os"}, blocked_modules={"os"})
        assert policy.is_allowed("os") is False


class TestNetworkPolicy:
    def test_no_endpoints_blocks_all(self) -> None:
        policy = NetworkPolicy()
        assert policy.is_host_allowed("example.com") is False

    def test_exact_match_allowed(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["api.example.com"])
        assert policy.is_host_allowed("api.example.com") is True

    def test_subdomain_allowed(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["example.com"])
        assert policy.is_host_allowed("api.example.com") is True

    def test_unrelated_host_blocked(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["example.com"])
        assert policy.is_host_allowed("evil.com") is False

    def test_partial_name_not_matched(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["example.com"])
        assert policy.is_host_allowed("notexample.com") is False

    def test_default_block_dns(self) -> None:
        policy = NetworkPolicy()
        assert policy.block_dns is True


class TestResourcePolicy:
    def test_defaults(self) -> None:
        policy = ResourcePolicy()
        assert policy.max_cpu_seconds == 30.0
        assert policy.max_memory_bytes == 512 * 1024 * 1024
        assert policy.max_file_descriptors == 64
        assert policy.max_threads == 1
        assert policy.wall_time_seconds == 60.0


class TestFilesystemPolicy:
    def test_defaults(self) -> None:
        policy = FilesystemPolicy()
        assert policy.read_only_paths == []
        assert policy.read_write_paths == []
        assert policy.virtual_root is None
        assert policy.block_symlinks is True
        assert policy.block_absolute_paths is True


class TestIntrospectionPolicy:
    def test_blocked_builtins_default(self) -> None:
        policy = IntrospectionPolicy()
        assert "eval" in policy.blocked_builtins
        assert "exec" in policy.blocked_builtins
        assert "compile" in policy.blocked_builtins
        assert "breakpoint" in policy.blocked_builtins

    def test___import___removed_from_default(self) -> None:
        policy = IntrospectionPolicy()
        assert "__import__" not in policy.blocked_builtins

    def test_blocked_attributes_default(self) -> None:
        policy = IntrospectionPolicy()
        assert "__subclasses__" in policy.blocked_attributes
        assert "__globals__" in policy.blocked_attributes
        assert "__dict__" in policy.blocked_attributes

    def test_default_flags(self) -> None:
        policy = IntrospectionPolicy()
        assert policy.blocked_dunder_access is True
        assert policy.block_gc is True
        assert policy.block_inspect is True
        assert policy.block_frame_access is True


class TestParseMemory:
    def test_gb(self) -> None:
        assert _parse_memory("2GB") == 2 * 1024**3

    def test_mb(self) -> None:
        assert _parse_memory("512MB") == 512 * 1024**2

    def test_kb(self) -> None:
        assert _parse_memory("256KB") == 256 * 1024

    def test_bytes(self) -> None:
        assert _parse_memory("1024B") == 1024

    def test_plain_number(self) -> None:
        assert _parse_memory("1048576") == 1_048_576

    def test_case_insensitive(self) -> None:
        assert _parse_memory("512mb") == 512 * 1024**2

    def test_with_spaces(self) -> None:
        assert _parse_memory("  1GB  ") == 1 * 1024**3

    def test_float_value(self) -> None:
        assert _parse_memory("1.5GB") == int(1.5 * 1024**3)


class TestSandboxPolicyFromManifest:
    def test_basic_manifest(self) -> None:
        manifest = SimpleNamespace(
            id="test_plugin",
            resources=SimpleNamespace(max_cpu_seconds=60, max_memory="1GB"),
            artifacts=["/data/weights.bin"],
            network=SimpleNamespace(allowed_endpoints=["api.example.com"]),
            requires_network=lambda: True,
        )
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy.plugin_id == "test_plugin"
        assert policy.resource_policy.max_cpu_seconds == 60
        assert policy.resource_policy.max_memory_bytes == 1 * 1024**3
        assert "api.example.com" in policy.network_policy.allowed_endpoints
        assert "/data/weights.bin" in policy.filesystem_policy.read_only_paths

    def test_manifest_without_id(self) -> None:
        manifest = SimpleNamespace(
            resources=SimpleNamespace(max_cpu_seconds=30, max_memory="512MB"),
        )
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy.plugin_id == "unknown"

    def test_manifest_without_network(self) -> None:
        manifest = SimpleNamespace(
            resources=SimpleNamespace(max_cpu_seconds=30, max_memory="512MB"),
        )
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy.network_policy.allowed_endpoints == []

    def test_manifest_without_artifacts(self) -> None:
        manifest = SimpleNamespace(
            resources=SimpleNamespace(max_cpu_seconds=30, max_memory="512MB"),
        )
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy.filesystem_policy.read_only_paths == []

    def test_import_policy_has_blocked_modules(self) -> None:
        manifest = SimpleNamespace(
            resources=SimpleNamespace(max_cpu_seconds=30, max_memory="512MB"),
        )
        policy = SandboxPolicy.from_manifest(manifest)
        assert len(policy.import_policy.blocked_modules) > 0
        assert "os" in policy.import_policy.blocked_modules


class TestSandboxPolicyTrustedPolicy:
    def test_trusted_policy_relaxed_import(self) -> None:
        policy = SandboxPolicy.trusted_policy("my_plugin")
        assert policy.plugin_id == "my_plugin"
        assert policy.trust_level == "trusted_full"
        assert "subprocess" in policy.import_policy.blocked_modules
        assert "ctypes" in policy.import_policy.blocked_modules

    def test_trusted_policy_higher_resource_limits(self) -> None:
        policy = SandboxPolicy.trusted_policy()
        assert policy.resource_policy.max_cpu_seconds == 300
        assert policy.resource_policy.max_memory_bytes == 2 * 1024**3

    def test_trusted_policy_relaxed_introspection(self) -> None:
        policy = SandboxPolicy.trusted_policy()
        assert "__subclasses__" in policy.introspection_policy.blocked_attributes
        assert "__globals__" in policy.introspection_policy.blocked_attributes
        assert "eval" not in policy.introspection_policy.blocked_builtins
        assert "exec" in policy.introspection_policy.blocked_builtins


class TestSandboxViolationCategory:
    def test_all_categories(self) -> None:
        assert SandboxViolationCategory.IMPORT.value == "import"
        assert SandboxViolationCategory.NETWORK.value == "network"
        assert SandboxViolationCategory.RESOURCE.value == "resource"
        assert SandboxViolationCategory.FILESYSTEM.value == "filesystem"
        assert SandboxViolationCategory.INTROSPECTION.value == "introspection"

    def test_member_count(self) -> None:
        assert len(list(SandboxViolationCategory)) == 6


class TestImportViolation:
    def test_message(self) -> None:
        v = ImportViolation("os")
        assert "os" in str(v)
        assert "blocked" in str(v).lower()

    def test_category(self) -> None:
        v = ImportViolation("subprocess")
        assert v.category is SandboxViolationCategory.IMPORT

    def test_module_name(self) -> None:
        v = ImportViolation("os.path")
        assert v.module_name == "os.path"

    def test_attempted_action(self) -> None:
        v = ImportViolation("sys")
        assert v.attempted_action == "import sys"

    def test_plugin_id(self) -> None:
        v = ImportViolation("os", plugin_id="test_plugin")
        assert v.plugin_id == "test_plugin"

    def test_plugin_id_default_none(self) -> None:
        v = ImportViolation("os")
        assert v.plugin_id is None

    def test_to_dict(self) -> None:
        v = ImportViolation("os", plugin_id="p1")
        d = v.to_dict()
        assert d["category"] == "import"
        assert d["plugin_id"] == "p1"
        assert "os" in d["detail"]
        assert d["attempted_action"] == "import os"

    def test_is_exception(self) -> None:
        v = ImportViolation("os")
        assert isinstance(v, Exception)


class TestNetworkViolation:
    def test_host_only(self) -> None:
        v = NetworkViolation("evil.com")
        assert v.host == "evil.com"
        assert v.port is None

    def test_with_port(self) -> None:
        v = NetworkViolation("evil.com", port=443)
        assert "443" in str(v)

    def test_category(self) -> None:
        v = NetworkViolation("evil.com")
        assert v.category is SandboxViolationCategory.NETWORK

    def test_attempted_action(self) -> None:
        v = NetworkViolation("evil.com", port=80)
        assert v.attempted_action == "connect:evil.com:80"

    def test_to_dict(self) -> None:
        v = NetworkViolation("evil.com", plugin_id="p1")
        d = v.to_dict()
        assert d["category"] == "network"
        assert "evil.com" in d["detail"]


class TestFilesystemViolation:
    def test_path_and_operation(self) -> None:
        v = FilesystemViolation("/etc/passwd", "read")
        assert v.path == "/etc/passwd"
        assert v.operation == "read"

    def test_category(self) -> None:
        v = FilesystemViolation("/var/log/test", "write")
        assert v.category is SandboxViolationCategory.FILESYSTEM

    def test_attempted_action(self) -> None:
        v = FilesystemViolation("/var/log/test", "write")
        assert v.attempted_action == "write:/var/log/test"

    def test_message_includes_operation(self) -> None:
        v = FilesystemViolation("/secret", "read")
        msg = str(v)
        assert "read" in msg
        assert "/secret" in msg


class TestIntrospectionViolation:
    def test_attribute(self) -> None:
        v = IntrospectionViolation("__subclasses__")
        assert v.attribute == "__subclasses__"

    def test_category(self) -> None:
        v = IntrospectionViolation("__globals__")
        assert v.category is SandboxViolationCategory.INTROSPECTION

    def test_attempted_action(self) -> None:
        v = IntrospectionViolation("__code__")
        assert v.attempted_action == "access:__code__"

    def test_message(self) -> None:
        v = IntrospectionViolation("__globals__")
        assert "__globals__" in str(v)
        assert "not accessible" in str(v)


class TestResourceExhausted:
    def test_fields(self) -> None:
        v = ResourceExhausted("memory", limit=512, current=600)
        assert v.resource_type == "memory"
        assert v.limit == 512
        assert v.current == 600

    def test_category(self) -> None:
        v = ResourceExhausted("fd", limit=64, current=65)
        assert v.category is SandboxViolationCategory.RESOURCE

    def test_message(self) -> None:
        v = ResourceExhausted("memory", limit=512, current=600)
        msg = str(v)
        assert "memory" in msg
        assert "512" in msg
        assert "600" in msg

    def test_attempted_action(self) -> None:
        v = ResourceExhausted("cpu", limit=30, current=35)
        assert v.attempted_action == "allocate:cpu"


class TestViolationToDict:
    def test_to_dict_all_fields_present(self) -> None:
        v = ImportViolation("os", plugin_id="p1")
        d = v.to_dict()
        assert "category" in d
        assert "detail" in d
        assert "plugin_id" in d
        assert "attempted_action" in d


class TestEnvironmentPolicy:
    def test_defaults(self) -> None:
        policy = EnvironmentPolicy()
        assert policy.allowed_env_vars == set()
        assert policy.block_os_environ is True
        assert policy.sanitized_env == {}

    def test_custom_allowed_vars(self) -> None:
        policy = EnvironmentPolicy(allowed_env_vars={"HOME", "PATH"})
        assert "HOME" in policy.allowed_env_vars
        assert "PATH" in policy.allowed_env_vars

    def test_trusted_full_allows_env(self) -> None:
        policy = _TRUST_ENVIRONMENT_PRESETS[TrustLevel.TRUSTED_FULL]
        assert policy.block_os_environ is False
        assert len(policy.allowed_env_vars) > 0

    def test_untrusted_blocks_env(self) -> None:
        policy = _TRUST_ENVIRONMENT_PRESETS[TrustLevel.UNTRUSTED]
        assert policy.block_os_environ is True
        assert len(policy.allowed_env_vars) == 0

    def test_limited_partial_env(self) -> None:
        policy = _TRUST_ENVIRONMENT_PRESETS[TrustLevel.TRUSTED_LIMITED]
        assert policy.block_os_environ is True
        assert "HOME" in policy.allowed_env_vars


class TestNetworkPolicyEnhanced:
    def test_block_metadata_endpoints_default(self) -> None:
        policy = NetworkPolicy()
        assert policy.block_metadata_endpoints is True

    def test_max_connections_default(self) -> None:
        policy = NetworkPolicy()
        assert policy.max_connections_per_host == 100

    def test_custom_max_connections(self) -> None:
        policy = NetworkPolicy(max_connections_per_host=10)
        assert policy.max_connections_per_host == 10


class TestFilesystemPolicyEnhanced:
    def test_block_env_access_default(self) -> None:
        policy = FilesystemPolicy()
        assert policy.block_env_access is True


class TestIntrospectionPolicyEnhanced:
    def test_block_type_abuse_default(self) -> None:
        policy = IntrospectionPolicy()
        assert policy.block_type_abuse is True

    def test_import_not_in_blocked_builtins(self) -> None:
        policy = IntrospectionPolicy()
        assert "__import__" not in policy.blocked_builtins

    def test_builtins_access_in_blocked_attrs(self) -> None:
        policy = IntrospectionPolicy()
        assert "__builtins__" in policy.blocked_attributes

    def test_func_self_in_blocked_attrs(self) -> None:
        policy = IntrospectionPolicy()
        assert "__func__" in policy.blocked_attributes
        assert "__self__" in policy.blocked_attributes


class TestSandboxPolicyIntegrity:
    def test_compute_integrity_hash_deterministic(self) -> None:
        policy = SandboxPolicy(plugin_id="test", trust_level="untrusted")
        h1 = policy.compute_integrity_hash()
        h2 = policy.compute_integrity_hash()
        assert h1 == h2

    def test_integrity_hash_changes_on_modification(self) -> None:
        policy = SandboxPolicy(plugin_id="test", trust_level="untrusted")
        policy.set_integrity_hash()
        policy.resource_policy.max_cpu_seconds = 999
        assert policy.verify_integrity() is False

    def test_set_and_verify_integrity(self) -> None:
        policy = SandboxPolicy(plugin_id="test", trust_level="untrusted")
        policy.set_integrity_hash()
        assert policy.verify_integrity() is True

    def test_verify_integrity_without_hash(self) -> None:
        policy = SandboxPolicy(plugin_id="test")
        assert policy.verify_integrity() is True


class TestSandboxPolicyHardLimits:
    def test_untrusted_cpu_hard_limit(self) -> None:
        assert _TRUST_MAX_CPU_HARD_LIMITS[TrustLevel.UNTRUSTED] == 120.0

    def test_untrusted_memory_hard_limit(self) -> None:
        assert _TRUST_MAX_MEMORY_HARD_LIMITS[TrustLevel.UNTRUSTED] == 1024**3

    def test_trusted_full_cpu_hard_limit(self) -> None:
        assert _TRUST_MAX_CPU_HARD_LIMITS[TrustLevel.TRUSTED_FULL] == 600.0

    def test_trusted_full_memory_hard_limit(self) -> None:
        assert _TRUST_MAX_MEMORY_HARD_LIMITS[TrustLevel.TRUSTED_FULL] == 4 * 1024**3

    def test_enforce_hard_limits_no_violations(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "test")
        violations = policy.enforce_hard_limits(TrustLevel.UNTRUSTED)
        assert violations == []

    def test_enforce_hard_limits_cpu_exceeded(self) -> None:
        policy = SandboxPolicy(
            plugin_id="test",
            trust_level="untrusted",
            resource_policy=ResourcePolicy(max_cpu_seconds=999),
        )
        violations = policy.enforce_hard_limits(TrustLevel.UNTRUSTED)
        assert any("max_cpu_seconds" in v for v in violations)

    def test_enforce_hard_limits_memory_exceeded(self) -> None:
        policy = SandboxPolicy(
            plugin_id="test",
            trust_level="untrusted",
            resource_policy=ResourcePolicy(max_memory_bytes=10 * 1024**3),
        )
        violations = policy.enforce_hard_limits(TrustLevel.UNTRUSTED)
        assert any("max_memory_bytes" in v for v in violations)

    def test_enforce_hard_limits_untrusted_with_rw(self) -> None:
        policy = SandboxPolicy(
            plugin_id="test",
            trust_level="untrusted",
            filesystem_policy=FilesystemPolicy(read_write_paths=["/tmp"]),  # noqa: S108
        )
        violations = policy.enforce_hard_limits(TrustLevel.UNTRUSTED)
        assert any("write paths" in v for v in violations)

    def test_enforce_hard_limits_untrusted_with_threads(self) -> None:
        policy = SandboxPolicy(
            plugin_id="test",
            trust_level="untrusted",
            resource_policy=ResourcePolicy(max_threads=4),
        )
        violations = policy.enforce_hard_limits(TrustLevel.UNTRUSTED)
        assert any("threads" in v for v in violations)

    def test_enforce_hard_limits_untrusted_no_metadata_block(self) -> None:
        policy = SandboxPolicy(
            plugin_id="test",
            trust_level="untrusted",
            network_policy=NetworkPolicy(block_metadata_endpoints=False),
        )
        violations = policy.enforce_hard_limits(TrustLevel.UNTRUSTED)
        assert any("metadata" in v for v in violations)

    def test_from_manifest_clamps_cpu(self) -> None:
        manifest = SimpleNamespace(
            id="clamp_test",
            resources=SimpleNamespace(max_cpu_seconds=1000, max_memory="10GB"),
        )
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy.resource_policy.max_cpu_seconds <= _TRUST_MAX_CPU_HARD_LIMITS[TrustLevel.UNTRUSTED]

    def test_from_trust_level_clamps_memory(self) -> None:
        policy = SandboxPolicy.from_trust_level(
            TrustLevel.UNTRUSTED,
            "test",
            max_memory_bytes=10 * 1024**3,
        )
        assert policy.resource_policy.max_memory_bytes <= _TRUST_MAX_MEMORY_HARD_LIMITS[TrustLevel.UNTRUSTED]

    def test_trusted_policy_has_integrity_hash(self) -> None:
        policy = SandboxPolicy.trusted_policy("test")
        assert policy._integrity_hash is not None

    def test_from_manifest_sets_integrity_hash(self) -> None:
        manifest = SimpleNamespace(
            id="hash_test",
            resources=SimpleNamespace(max_cpu_seconds=30, max_memory="512MB"),
        )
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy._integrity_hash is not None

    def test_from_trust_level_sets_integrity_hash(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "test")
        assert policy._integrity_hash is not None
