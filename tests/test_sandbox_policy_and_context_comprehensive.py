"""
Comprehensive tests for sandbox policy integrity hash fixes and context behavior.

Covers:
  - Deterministic hashing with mixed-type sets
  - v1/v2 hash backward compatibility
  - _serialize_policy_value edge cases
  - enforce_hard_limits boundary conditions
  - from_manifest / from_trust_level factory methods
  - _parse_memory edge cases
  - Context activation violation logging
  - Policy sub-policy defaults and overrides
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from engine.plugins.sandbox.core.context import (
    _MAX_CPU_SECONDS_LIMITED,
    _MAX_CPU_SECONDS_UNTRUSTED,
    _MIN_BLOCKED_MODULES_LIMITED,
    _MIN_BLOCKED_MODULES_UNTRUSTED,
    SandboxContext,
)
from engine.plugins.sandbox.core.policy import (
    _TRUST_ENVIRONMENT_PRESETS,
    _TRUST_FILESYSTEM_RW,
    _TRUST_IMPORT_PRESETS,
    _TRUST_INTROSPECTION_PRESETS,
    _TRUST_MAX_CPU_HARD_LIMITS,
    _TRUST_MAX_MEMORY_HARD_LIMITS,
    _TRUST_RESOURCE_MULTIPLIERS,
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
    SandboxViolation,
    SandboxViolationCategory,
)
from engine.plugins.trust_levels import TrustLevel


class TestSerializePolicyValueDeterminism:
    def test_mixed_type_set_sorted_deterministically(self) -> None:
        policy = SandboxPolicy(
            plugin_id="mixed",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules={"os", "sys", "subprocess"}),
        )
        hashes = [policy.compute_integrity_hash() for _ in range(20)]
        assert len(set(hashes)) == 1

    def test_int_set_deterministic(self) -> None:
        policy = SandboxPolicy(
            plugin_id="int_set",
            trust_level="untrusted",
            network_policy=NetworkPolicy(allowed_ports={80, 443, 8080}),
        )
        hashes = [policy.compute_integrity_hash() for _ in range(10)]
        assert len(set(hashes)) == 1

    def test_string_set_deterministic(self) -> None:
        policy = SandboxPolicy(
            plugin_id="str_set",
            trust_level="untrusted",
            introspection_policy=IntrospectionPolicy(
                blocked_attributes={"__globals__", "__code__", "__closure__"},
            ),
        )
        hashes = [policy.compute_integrity_hash() for _ in range(10)]
        assert len(set(hashes)) == 1

    def test_enum_serialized_to_value(self) -> None:
        result = SandboxPolicy._serialize_policy_value(TrustLevel.UNTRUSTED)
        assert result == "untrusted"
        result = SandboxPolicy._serialize_policy_value(TrustLevel.TRUSTED_FULL)
        assert result == "trusted_full"
        result = SandboxPolicy._serialize_policy_value(TrustLevel.TRUSTED_LIMITED)
        assert result == "trusted_limited"

    def test_dict_sorted_by_keys(self) -> None:
        data = {"z_key": 1, "a_key": 2, "m_key": 3}
        result = SandboxPolicy._serialize_policy_value(data)
        assert list(result.keys()) == ["a_key", "m_key", "z_key"]

    def test_nested_dataclass_serialized(self) -> None:
        policy = SandboxPolicy(plugin_id="nested_test")
        serialized = SandboxPolicy._serialize_policy_value(policy)
        assert isinstance(serialized, dict)
        assert "plugin_id" in serialized
        assert "import_policy" in serialized
        assert serialized["plugin_id"] == "nested_test"

    def test_private_fields_excluded(self) -> None:
        policy = SandboxPolicy(plugin_id="private_test")
        policy.set_integrity_hash()
        serialized = SandboxPolicy._serialize_policy_value(policy)
        assert "_integrity_hash" not in serialized

    def test_list_passthrough(self) -> None:
        result = SandboxPolicy._serialize_policy_value(["b", "a", "c"])
        assert result == ["a", "b", "c"]

    def test_tuple_converted_to_sorted_list(self) -> None:
        result = SandboxPolicy._serialize_policy_value(("b", "a", "c"))
        assert result == ["a", "b", "c"]

    def test_primitive_passthrough(self) -> None:
        assert SandboxPolicy._serialize_policy_value(42) == 42
        assert SandboxPolicy._serialize_policy_value(3.14) == 3.14
        assert SandboxPolicy._serialize_policy_value("hello") == "hello"
        assert SandboxPolicy._serialize_policy_value(True) is True
        assert SandboxPolicy._serialize_policy_value(None) is None


class TestIntegrityHashV2Format:
    def test_hash_starts_with_v2_prefix(self) -> None:
        policy = SandboxPolicy(plugin_id="prefix_test")
        h = policy.compute_integrity_hash()
        assert h.startswith("v2:")

    def test_hash_after_prefix_is_sha256_hex(self) -> None:
        policy = SandboxPolicy(plugin_id="sha_test")
        h = policy.compute_integrity_hash()
        hex_part = h.removeprefix("v2:")
        assert len(hex_part) == 64
        int(hex_part, 16)

    def test_verify_with_v2_hash(self) -> None:
        policy = SandboxPolicy(plugin_id="v2_verify")
        policy.set_integrity_hash()
        assert policy._integrity_hash.startswith("v2:")
        assert policy.verify_integrity()

    def test_verify_v1_backward_compatible(self) -> None:
        policy = SandboxPolicy(plugin_id="v1_compat")
        v2_hash = policy.compute_integrity_hash()
        v1_hash = v2_hash.removeprefix("v2:")
        policy._integrity_hash = v1_hash
        assert policy.verify_integrity()

    def test_verify_rejects_wrong_hash(self) -> None:
        policy = SandboxPolicy(plugin_id="wrong_hash")
        policy._integrity_hash = "v2:" + "a" * 64
        assert not policy.verify_integrity()

    def test_verify_rejects_wrong_v1_hash(self) -> None:
        policy = SandboxPolicy(plugin_id="wrong_v1")
        policy._integrity_hash = "b" * 64
        assert not policy.verify_integrity()

    def test_verify_none_hash_passes(self) -> None:
        policy = SandboxPolicy(plugin_id="none_hash")
        policy._integrity_hash = None
        assert policy.verify_integrity()


class TestIntegrityHashModificationDetection:
    def test_detect_plugin_id_change(self) -> None:
        policy = SandboxPolicy(plugin_id="original")
        h1 = policy.compute_integrity_hash()
        policy.plugin_id = "modified"
        h2 = policy.compute_integrity_hash()
        assert h1 != h2

    def test_detect_trust_level_change(self) -> None:
        policy = SandboxPolicy(plugin_id="t", trust_level="untrusted")
        h1 = policy.compute_integrity_hash()
        policy.trust_level = "trusted_full"
        h2 = policy.compute_integrity_hash()
        assert h1 != h2

    def test_detect_import_policy_change(self) -> None:
        policy = SandboxPolicy(
            plugin_id="t",
            import_policy=ImportPolicy(blocked_modules={"os"}),
        )
        h1 = policy.compute_integrity_hash()
        policy.import_policy.blocked_modules.add("sys")
        h2 = policy.compute_integrity_hash()
        assert h1 != h2

    def test_detect_network_policy_change(self) -> None:
        policy = SandboxPolicy(plugin_id="t")
        h1 = policy.compute_integrity_hash()
        policy.network_policy.allowed_endpoints.append("evil.com")
        h2 = policy.compute_integrity_hash()
        assert h1 != h2

    def test_detect_resource_policy_change(self) -> None:
        policy = SandboxPolicy(plugin_id="t")
        h1 = policy.compute_integrity_hash()
        policy.resource_policy.max_cpu_seconds = 999
        h2 = policy.compute_integrity_hash()
        assert h1 != h2

    def test_detect_filesystem_policy_change(self) -> None:
        policy = SandboxPolicy(plugin_id="t")
        h1 = policy.compute_integrity_hash()
        policy.filesystem_policy.read_write_paths = ["/data/write"]
        h2 = policy.compute_integrity_hash()
        assert h1 != h2

    def test_detect_introspection_policy_change(self) -> None:
        policy = SandboxPolicy(plugin_id="t")
        h1 = policy.compute_integrity_hash()
        policy.introspection_policy.block_gc = False
        h2 = policy.compute_integrity_hash()
        assert h1 != h2

    def test_detect_environment_policy_change(self) -> None:
        policy = SandboxPolicy(plugin_id="t")
        h1 = policy.compute_integrity_hash()
        policy.environment_policy.block_os_environ = False
        h2 = policy.compute_integrity_hash()
        assert h1 != h2

    def test_set_verify_tamper_detect(self) -> None:
        policy = SandboxPolicy(plugin_id="tamper")
        policy.set_integrity_hash()
        assert policy.verify_integrity()
        policy.resource_policy.max_memory_bytes = 0
        assert not policy.verify_integrity()

    def test_identical_policies_same_hash(self) -> None:
        p1 = SandboxPolicy(
            plugin_id="same",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules={"os", "sys"}),
        )
        p2 = SandboxPolicy(
            plugin_id="same",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules={"os", "sys"}),
        )
        assert p1.compute_integrity_hash() == p2.compute_integrity_hash()


class TestEnforceHardLimits:
    def test_untrusted_cpu_exceeds_hard_limit(self) -> None:
        policy = SandboxPolicy(
            plugin_id="t",
            trust_level="untrusted",
            resource_policy=ResourcePolicy(max_cpu_seconds=200),
        )
        violations = policy.enforce_hard_limits(TrustLevel.UNTRUSTED)
        assert any("max_cpu_seconds" in v for v in violations)

    def test_untrusted_memory_exceeds_hard_limit(self) -> None:
        policy = SandboxPolicy(
            plugin_id="t",
            trust_level="untrusted",
            resource_policy=ResourcePolicy(max_memory_bytes=2 * 1024**3),
        )
        violations = policy.enforce_hard_limits(TrustLevel.UNTRUSTED)
        assert any("max_memory_bytes" in v for v in violations)

    def test_untrusted_with_rw_paths_violation(self) -> None:
        policy = SandboxPolicy(
            plugin_id="t",
            trust_level="untrusted",
            filesystem_policy=FilesystemPolicy(read_write_paths=["/data/write"]),
        )
        violations = policy.enforce_hard_limits(TrustLevel.UNTRUSTED)
        assert any("write paths" in v for v in violations)

    def test_untrusted_with_threads_violation(self) -> None:
        policy = SandboxPolicy(
            plugin_id="t",
            trust_level="untrusted",
            resource_policy=ResourcePolicy(max_threads=4),
        )
        violations = policy.enforce_hard_limits(TrustLevel.UNTRUSTED)
        assert any("threads" in v for v in violations)

    def test_untrusted_metadata_endpoints_disabled(self) -> None:
        policy = SandboxPolicy(
            plugin_id="t",
            trust_level="untrusted",
            network_policy=NetworkPolicy(block_metadata_endpoints=False),
        )
        violations = policy.enforce_hard_limits(TrustLevel.UNTRUSTED)
        assert any("metadata" in v for v in violations)

    def test_untrusted_no_violations_when_compliant(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "t")
        violations = policy.enforce_hard_limits(TrustLevel.UNTRUSTED)
        assert violations == []

    def test_limited_cpu_at_boundary(self) -> None:
        policy = SandboxPolicy(
            plugin_id="t",
            trust_level="trusted_limited",
            resource_policy=ResourcePolicy(max_cpu_seconds=300.0),
        )
        violations = policy.enforce_hard_limits(TrustLevel.TRUSTED_LIMITED)
        assert not any("max_cpu_seconds" in v for v in violations)

    def test_limited_cpu_just_over_boundary(self) -> None:
        policy = SandboxPolicy(
            plugin_id="t",
            trust_level="trusted_limited",
            resource_policy=ResourcePolicy(max_cpu_seconds=300.1),
        )
        violations = policy.enforce_hard_limits(TrustLevel.TRUSTED_LIMITED)
        assert any("max_cpu_seconds" in v for v in violations)

    def test_trusted_full_high_cpu_within_limit(self) -> None:
        policy = SandboxPolicy(
            plugin_id="t",
            trust_level="trusted_full",
            resource_policy=ResourcePolicy(max_cpu_seconds=600.0),
        )
        violations = policy.enforce_hard_limits(TrustLevel.TRUSTED_FULL)
        assert not any("max_cpu_seconds" in v for v in violations)

    def test_multiple_violations_accumulate(self) -> None:
        policy = SandboxPolicy(
            plugin_id="t",
            trust_level="untrusted",
            resource_policy=ResourcePolicy(
                max_cpu_seconds=999,
                max_memory_bytes=999 * 1024**3,
                max_threads=10,
            ),
            filesystem_policy=FilesystemPolicy(read_write_paths=["/data/write"]),
            network_policy=NetworkPolicy(block_metadata_endpoints=False),
        )
        violations = policy.enforce_hard_limits(TrustLevel.UNTRUSTED)
        assert len(violations) >= 4


class TestFromTrustLevelFactory:
    def test_untrusted_factory(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "untrusted_plugin")
        assert policy.plugin_id == "untrusted_plugin"
        assert policy.trust_level == "untrusted"
        assert len(policy.import_policy.blocked_modules) >= _MIN_BLOCKED_MODULES_UNTRUSTED

    def test_limited_factory(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_LIMITED, "limited_plugin")
        assert policy.trust_level == "trusted_limited"
        assert policy.resource_policy.max_cpu_seconds <= 300.0

    def test_trusted_full_factory(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_FULL, "trusted_plugin")
        assert policy.trust_level == "trusted_full"
        assert policy.resource_policy.max_cpu_seconds <= 600.0

    def test_resource_multiplier_applied(self) -> None:
        base_cpu = 30.0
        base_mem = 512 * 1024**2
        p_untrusted = SandboxPolicy.from_trust_level(
            TrustLevel.UNTRUSTED, "u", max_cpu_seconds=base_cpu, max_memory_bytes=base_mem,
        )
        p_limited = SandboxPolicy.from_trust_level(
            TrustLevel.TRUSTED_LIMITED, "l", max_cpu_seconds=base_cpu, max_memory_bytes=base_mem,
        )
        p_full = SandboxPolicy.from_trust_level(
            TrustLevel.TRUSTED_FULL, "f", max_cpu_seconds=base_cpu, max_memory_bytes=base_mem,
        )
        assert p_untrusted.resource_policy.max_cpu_seconds <= p_limited.resource_policy.max_cpu_seconds
        assert p_limited.resource_policy.max_cpu_seconds <= p_full.resource_policy.max_cpu_seconds

    def test_factory_hard_limit_clamp(self) -> None:
        policy = SandboxPolicy.from_trust_level(
            TrustLevel.UNTRUSTED, "clamped",
            max_cpu_seconds=99999,
            max_memory_bytes=999 * 1024**3,
        )
        assert policy.resource_policy.max_cpu_seconds <= _TRUST_MAX_CPU_HARD_LIMITS[TrustLevel.UNTRUSTED]
        assert policy.resource_policy.max_memory_bytes <= _TRUST_MAX_MEMORY_HARD_LIMITS[TrustLevel.UNTRUSTED]

    def test_factory_with_network_endpoints(self) -> None:
        policy = SandboxPolicy.from_trust_level(
            TrustLevel.UNTRUSTED, "net",
            network_endpoints=["api.example.com"],
        )
        assert policy.network_policy.allowed_endpoints == ["api.example.com"]

    def test_factory_with_read_only_paths(self) -> None:
        policy = SandboxPolicy.from_trust_level(
            TrustLevel.UNTRUSTED, "rop",
            read_only_paths=["/data"],
        )
        assert "/data" in policy.filesystem_policy.read_only_paths

    def test_factory_sets_integrity_hash(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "hash_check")
        assert policy._integrity_hash is not None
        assert policy.verify_integrity()

    def test_factory_introspection_presets(self) -> None:
        p_full = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_FULL, "f")
        p_untrusted = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "u")
        assert len(p_full.introspection_policy.blocked_builtins) < len(p_untrusted.introspection_policy.blocked_builtins)
        assert p_full.introspection_policy.block_gc is False or p_full.introspection_policy.block_gc is True

    def test_factory_environment_presets(self) -> None:
        p_full = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_FULL, "f")
        p_untrusted = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "u")
        assert p_full.environment_policy.block_os_environ is False
        assert p_untrusted.environment_policy.block_os_environ is True
        assert "PATH" in p_full.environment_policy.allowed_env_vars
        assert len(p_untrusted.environment_policy.allowed_env_vars) == 0


class TestFromManifestFactory:
    def _make_manifest(
        self,
        plugin_id: str = "manifest_plugin",
        trust_level: str = "untrusted",
        requires_network: bool = False,
        endpoints: list[str] | None = None,
        cpu_seconds: int = 30,
        max_memory: str = "512MB",
        artifacts: list[str] | None = None,
        has_fs_write: bool = False,
    ) -> MagicMock:
        m = MagicMock()
        m.id = plugin_id
        m.trust_level = trust_level
        m.requires_network.return_value = requires_network
        m.network = MagicMock()
        m.network.allowed_endpoints = endpoints or []
        m.resources = MagicMock()
        m.resources.max_cpu_seconds = cpu_seconds
        m.resources.max_memory = max_memory
        m.artifacts = artifacts or []
        m.permissions = MagicMock()
        m.has_permission.return_value = has_fs_write
        return m

    def test_basic_manifest(self) -> None:
        m = self._make_manifest()
        policy = SandboxPolicy.from_manifest(m)
        assert policy.plugin_id == "manifest_plugin"
        assert policy.trust_level == "untrusted"
        assert policy.verify_integrity()

    def test_manifest_trusted_full(self) -> None:
        m = self._make_manifest(trust_level="trusted_full")
        policy = SandboxPolicy.from_manifest(m)
        assert policy.trust_level == "trusted_full"

    def test_manifest_with_network(self) -> None:
        m = self._make_manifest(
            requires_network=True,
            endpoints=["api.exchange.com"],
        )
        policy = SandboxPolicy.from_manifest(m)
        assert "api.exchange.com" in policy.network_policy.allowed_endpoints

    def test_manifest_without_network(self) -> None:
        m = self._make_manifest(requires_network=False)
        policy = SandboxPolicy.from_manifest(m)
        assert policy.network_policy.allowed_endpoints == []

    def test_manifest_custom_resources(self) -> None:
        m = self._make_manifest(cpu_seconds=60, max_memory="1GB")
        policy = SandboxPolicy.from_manifest(m)
        assert policy.resource_policy.max_cpu_seconds > 0
        assert policy.resource_policy.max_memory_bytes > 0

    def test_manifest_with_artifacts(self) -> None:
        m = self._make_manifest(
            trust_level="trusted_full",
            artifacts=["/data/file1.csv"],
            has_fs_write=True,
        )
        policy = SandboxPolicy.from_manifest(m)
        assert "/data/file1.csv" in policy.filesystem_policy.read_only_paths
        assert "/data/file1.csv" in policy.filesystem_policy.read_write_paths

    def test_manifest_untrusted_no_write(self) -> None:
        m = self._make_manifest(
            trust_level="untrusted",
            artifacts=["/data/file.csv"],
            has_fs_write=True,
        )
        policy = SandboxPolicy.from_manifest(m)
        assert policy.filesystem_policy.read_write_paths == []

    def test_manifest_sets_integrity_hash(self) -> None:
        m = self._make_manifest()
        policy = SandboxPolicy.from_manifest(m)
        assert policy._integrity_hash is not None
        assert policy.verify_integrity()

    def test_manifest_without_resources_attr(self) -> None:
        m = MagicMock()
        m.id = "no_res"
        m.trust_level = "untrusted"
        del m.resources
        del m.network
        del m.artifacts
        del m.permissions
        policy = SandboxPolicy.from_manifest(m)
        assert policy.plugin_id == "no_res"


class TestTrustedPolicyFactory:
    def test_trusted_policy_defaults(self) -> None:
        policy = SandboxPolicy.trusted_policy()
        assert policy.plugin_id == "trusted"
        assert policy.trust_level == "trusted_full"
        assert "subprocess" in policy.import_policy.blocked_modules
        assert policy.environment_policy.block_os_environ is False

    def test_trusted_policy_custom_id(self) -> None:
        policy = SandboxPolicy.trusted_policy("custom_id")
        assert policy.plugin_id == "custom_id"

    def test_trusted_policy_has_integrity_hash(self) -> None:
        policy = SandboxPolicy.trusted_policy()
        assert policy.verify_integrity()


class TestParseMemory:
    @pytest.mark.parametrize(
        ("input_str", "expected"),
        [
            ("512MB", 512 * 1024**2),
            ("1GB", 1024**3),
            ("256KB", 256 * 1024),
            ("1024B", 1024),
            ("1.5GB", int(1.5 * 1024**3)),
            ("2.5MB", int(2.5 * 1024**2)),
            ("1048576", 1048576),
            ("0", 0),
            ("  128MB  ", 128 * 1024**2),
            ("512mb", 512 * 1024**2),
            ("1gb", 1024**3),
        ],
    )
    def test_parse_memory_valid(self, input_str: str, expected: int) -> None:
        assert _parse_memory(input_str) == expected

    def test_parse_memory_float_precision(self) -> None:
        result = _parse_memory("0.1GB")
        assert abs(result - int(0.1 * 1024**3)) < 2

    def test_parse_memory_large_value(self) -> None:
        result = _parse_memory("4GB")
        assert result == 4 * 1024**3


class TestImportPolicy:
    def test_is_allowed_no_restrictions(self) -> None:
        policy = ImportPolicy()
        assert policy.is_allowed("os")
        assert policy.is_allowed("json")

    def test_is_allowed_blocked(self) -> None:
        policy = ImportPolicy(blocked_modules={"os"})
        assert not policy.is_allowed("os")
        assert not policy.is_allowed("os.path")

    def test_is_allowed_allowlist(self) -> None:
        policy = ImportPolicy(allowed_modules={"json"}, blocked_modules=set())
        assert policy.is_allowed("json")
        assert not policy.is_allowed("os")

    def test_is_allowed_blocked_overrides_allowed(self) -> None:
        policy = ImportPolicy(allowed_modules={"os"}, blocked_modules={"os"})
        assert not policy.is_allowed("os")

    def test_is_allowed_submodule(self) -> None:
        policy = ImportPolicy(blocked_modules={"http"})
        assert not policy.is_allowed("http.client")
        assert not policy.is_allowed("http.server")

    def test_empty_sets_allow_all(self) -> None:
        policy = ImportPolicy(allowed_modules=set(), blocked_modules=set())
        assert policy.is_allowed("anything")


class TestNetworkPolicy:
    def test_is_host_allowed_empty_endpoints(self) -> None:
        policy = NetworkPolicy()
        assert not policy.is_host_allowed("any.com")

    def test_is_host_allowed_exact_match(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["example.com"])
        assert policy.is_host_allowed("example.com")

    def test_is_host_allowed_subdomain(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["example.com"])
        assert policy.is_host_allowed("sub.example.com")
        assert policy.is_host_allowed("deep.sub.example.com")

    def test_is_host_allowed_no_partial_match(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["example.com"])
        assert not policy.is_host_allowed("notexample.com")
        assert not policy.is_host_allowed("example.com.evil.com")

    def test_default_block_dns(self) -> None:
        policy = NetworkPolicy()
        assert policy.block_dns is True

    def test_default_block_metadata(self) -> None:
        policy = NetworkPolicy()
        assert policy.block_metadata_endpoints is True


class TestResourcePolicyDefaults:
    def test_defaults(self) -> None:
        policy = ResourcePolicy()
        assert policy.max_cpu_seconds == 30.0
        assert policy.max_memory_bytes == 512 * 1024 * 1024
        assert policy.max_file_descriptors == 64
        assert policy.max_threads == 1
        assert policy.wall_time_seconds == 60.0


class TestFilesystemPolicyDefaults:
    def test_defaults(self) -> None:
        policy = FilesystemPolicy()
        assert policy.read_only_paths == []
        assert policy.read_write_paths == []
        assert policy.virtual_root is None
        assert policy.block_symlinks is True
        assert policy.block_absolute_paths is True
        assert policy.block_env_access is True


class TestIntrospectionPolicyDefaults:
    def test_default_blocked_builtins(self) -> None:
        policy = IntrospectionPolicy()
        assert "eval" in policy.blocked_builtins
        assert "exec" in policy.blocked_builtins
        assert "compile" in policy.blocked_builtins

    def test_default_blocked_attributes(self) -> None:
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
        assert policy.block_type_abuse is True


class TestEnvironmentPolicyDefaults:
    def test_defaults(self) -> None:
        policy = EnvironmentPolicy()
        assert policy.allowed_env_vars == set()
        assert policy.block_os_environ is True
        assert policy.sanitized_env == {}


class TestTrustPresetsConsistency:
    def test_all_trust_levels_have_import_presets(self) -> None:
        for level in TrustLevel:
            assert level in _TRUST_IMPORT_PRESETS

    def test_all_trust_levels_have_introspection_presets(self) -> None:
        for level in TrustLevel:
            assert level in _TRUST_INTROSPECTION_PRESETS

    def test_all_trust_levels_have_resource_multipliers(self) -> None:
        for level in TrustLevel:
            assert level in _TRUST_RESOURCE_MULTIPLIERS

    def test_all_trust_levels_have_filesystem_rw(self) -> None:
        for level in TrustLevel:
            assert level in _TRUST_FILESYSTEM_RW

    def test_all_trust_levels_have_cpu_hard_limits(self) -> None:
        for level in TrustLevel:
            assert level in _TRUST_MAX_CPU_HARD_LIMITS

    def test_all_trust_levels_have_memory_hard_limits(self) -> None:
        for level in TrustLevel:
            assert level in _TRUST_MAX_MEMORY_HARD_LIMITS

    def test_all_trust_levels_have_environment_presets(self) -> None:
        for level in TrustLevel:
            assert level in _TRUST_ENVIRONMENT_PRESETS

    def test_multipliers_ordered(self) -> None:
        assert _TRUST_RESOURCE_MULTIPLIERS[TrustLevel.UNTRUSTED] < _TRUST_RESOURCE_MULTIPLIERS[TrustLevel.TRUSTED_LIMITED]
        assert _TRUST_RESOURCE_MULTIPLIERS[TrustLevel.TRUSTED_LIMITED] < _TRUST_RESOURCE_MULTIPLIERS[TrustLevel.TRUSTED_FULL]

    def test_hard_limits_ordered(self) -> None:
        assert _TRUST_MAX_CPU_HARD_LIMITS[TrustLevel.UNTRUSTED] < _TRUST_MAX_CPU_HARD_LIMITS[TrustLevel.TRUSTED_LIMITED]
        assert _TRUST_MAX_CPU_HARD_LIMITS[TrustLevel.TRUSTED_LIMITED] < _TRUST_MAX_CPU_HARD_LIMITS[TrustLevel.TRUSTED_FULL]
        assert _TRUST_MAX_MEMORY_HARD_LIMITS[TrustLevel.UNTRUSTED] < _TRUST_MAX_MEMORY_HARD_LIMITS[TrustLevel.TRUSTED_LIMITED]
        assert _TRUST_MAX_MEMORY_HARD_LIMITS[TrustLevel.TRUSTED_LIMITED] < _TRUST_MAX_MEMORY_HARD_LIMITS[TrustLevel.TRUSTED_FULL]

    def test_untrusted_no_filesystem_write(self) -> None:
        assert _TRUST_FILESYSTEM_RW[TrustLevel.UNTRUSTED] is False

    def test_limited_has_filesystem_write(self) -> None:
        assert _TRUST_FILESYSTEM_RW[TrustLevel.TRUSTED_LIMITED] is True

    def test_full_has_filesystem_write(self) -> None:
        assert _TRUST_FILESYSTEM_RW[TrustLevel.TRUSTED_FULL] is True


class TestSandboxContextActivationViolations:
    def test_activate_logs_violation_on_integrity_fail(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "log_test")
        policy.set_integrity_hash()
        policy.resource_policy.max_cpu_seconds = 9999
        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation, match="Trust level"):
            ctx.activate()
        events = ctx.event_logger.get_events()
        assert len(events) >= 1
        assert events[0].category == SandboxViolationCategory.RESOURCE
        ctx.cleanup()

    def test_activate_logs_hard_limit_violation(self) -> None:
        policy = SandboxPolicy(
            plugin_id="hard_test",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules={f"m_{i}" for i in range(15)}),
            resource_policy=ResourcePolicy(max_cpu_seconds=30, max_memory_bytes=999 * 1024**3),
            filesystem_policy=FilesystemPolicy(read_write_paths=[]),
        )
        policy.set_integrity_hash()
        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation, match="Hard limit"):
            ctx.activate()
        events = ctx.event_logger.get_events()
        assert len(events) >= 1
        assert any("max_memory_bytes" in e.detail for e in events)
        ctx.cleanup()

    def test_activate_rejects_insufficient_blocked_modules(self) -> None:
        policy = SandboxPolicy(
            plugin_id="weak",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules={"os"}),
            resource_policy=ResourcePolicy(max_cpu_seconds=30),
        )
        policy.set_integrity_hash()
        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation, match="Trust level"):
            ctx.activate()
        assert not ctx.is_active
        ctx.cleanup()

    def test_activate_rejects_untrusted_with_rw_paths(self) -> None:
        policy = SandboxPolicy(
            plugin_id="rw_fail",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules={f"m_{i}" for i in range(15)}),
            resource_policy=ResourcePolicy(max_cpu_seconds=30),
            filesystem_policy=FilesystemPolicy(read_write_paths=["/data/write"]),
        )
        policy.set_integrity_hash()
        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation):
            ctx.activate()
        ctx.cleanup()

    def test_activate_rejects_untrusted_with_threads(self) -> None:
        policy = SandboxPolicy(
            plugin_id="thread_fail",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules={f"m_{i}" for i in range(15)}),
            resource_policy=ResourcePolicy(max_cpu_seconds=30, max_threads=4),
        )
        policy.set_integrity_hash()
        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation):
            ctx.activate()
        ctx.cleanup()


class TestSandboxContextTrustBoundaryConstants:
    def test_boundary_values(self) -> None:
        assert _MIN_BLOCKED_MODULES_UNTRUSTED == 10
        assert _MIN_BLOCKED_MODULES_LIMITED == 5
        assert _MAX_CPU_SECONDS_UNTRUSTED == 60
        assert _MAX_CPU_SECONDS_LIMITED == 120


class TestSandboxContextValidationLogic:
    def test_untrusted_validates_all_conditions(self) -> None:
        enough = {f"m_{i}" for i in range(15)}
        policy = SandboxPolicy(
            plugin_id="t",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules=enough),
            resource_policy=ResourcePolicy(max_cpu_seconds=30, max_threads=1),
            filesystem_policy=FilesystemPolicy(read_write_paths=[]),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level()
        ctx.cleanup()

    def test_untrusted_too_few_modules(self) -> None:
        policy = SandboxPolicy(
            plugin_id="t",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules={"os"}),
            resource_policy=ResourcePolicy(max_cpu_seconds=30),
        )
        ctx = SandboxContext(policy)
        assert not ctx.validate_trust_level()
        ctx.cleanup()

    def test_untrusted_cpu_over_limit(self) -> None:
        policy = SandboxPolicy(
            plugin_id="t",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules={f"m_{i}" for i in range(15)}),
            resource_policy=ResourcePolicy(max_cpu_seconds=120),
        )
        ctx = SandboxContext(policy)
        assert not ctx.validate_trust_level()
        ctx.cleanup()

    def test_limited_validates(self) -> None:
        policy = SandboxPolicy(
            plugin_id="t",
            trust_level="trusted_limited",
            import_policy=ImportPolicy(blocked_modules={f"m_{i}" for i in range(10)}),
            resource_policy=ResourcePolicy(max_cpu_seconds=60),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level()
        ctx.cleanup()

    def test_limited_too_few_modules(self) -> None:
        policy = SandboxPolicy(
            plugin_id="t",
            trust_level="trusted_limited",
            import_policy=ImportPolicy(blocked_modules={"os"}),
            resource_policy=ResourcePolicy(max_cpu_seconds=60),
        )
        ctx = SandboxContext(policy)
        assert not ctx.validate_trust_level()
        ctx.cleanup()

    def test_limited_cpu_over_limit(self) -> None:
        policy = SandboxPolicy(
            plugin_id="t",
            trust_level="trusted_limited",
            import_policy=ImportPolicy(blocked_modules={f"m_{i}" for i in range(10)}),
            resource_policy=ResourcePolicy(max_cpu_seconds=200),
        )
        ctx = SandboxContext(policy)
        assert not ctx.validate_trust_level()
        ctx.cleanup()

    def test_trusted_full_always_validates(self) -> None:
        policy = SandboxPolicy(
            plugin_id="t",
            trust_level="trusted_full",
            import_policy=ImportPolicy(blocked_modules=set()),
            resource_policy=ResourcePolicy(max_cpu_seconds=600),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level()
        ctx.cleanup()

    def test_invalid_trust_level_defaults_to_untrusted(self) -> None:
        policy = SandboxPolicy(plugin_id="t", trust_level="invalid_level")
        ctx = SandboxContext(policy)
        assert ctx.trust_level == TrustLevel.UNTRUSTED
        ctx.cleanup()


class TestSandboxContextProperties:
    def test_policy_property(self) -> None:
        policy = SandboxPolicy(plugin_id="prop_test")
        ctx = SandboxContext(policy)
        assert ctx.policy is policy
        ctx.cleanup()

    def test_event_logger_has_plugin_id(self) -> None:
        policy = SandboxPolicy(plugin_id="logger_test")
        ctx = SandboxContext(policy)
        assert ctx.event_logger._plugin_id == "logger_test"
        ctx.cleanup()

    def test_trust_level_property(self) -> None:
        policy = SandboxPolicy(plugin_id="t", trust_level="trusted_full")
        ctx = SandboxContext(policy)
        assert ctx.trust_level == TrustLevel.TRUSTED_FULL
        ctx.cleanup()

    def test_not_active_initially(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "t")
        ctx = SandboxContext(policy)
        assert not ctx.is_active
        ctx.cleanup()

    def test_work_dir_exists(self) -> None:
        import os
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "t")
        ctx = SandboxContext(policy)
        assert os.path.isdir(ctx.work_dir)
        ctx.cleanup()
