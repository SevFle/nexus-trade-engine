from __future__ import annotations

from typing import ClassVar

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
from engine.plugins.trust_levels import TrustLevel


class TestImportPolicy:
    def test_default_empty_sets(self):
        p = ImportPolicy()
        assert p.allowed_modules == set()
        assert p.blocked_modules == set()

    def test_is_allowed_empty_policy(self):
        p = ImportPolicy()
        assert p.is_allowed("os") is True

    def test_is_allowed_blocked_root(self):
        p = ImportPolicy(blocked_modules={"os"})
        assert p.is_allowed("os") is False
        assert p.is_allowed("os.path") is False

    def test_is_allowed_not_in_blocked(self):
        p = ImportPolicy(blocked_modules={"os"})
        assert p.is_allowed("json") is True

    def test_is_allowed_with_allowlist(self):
        p = ImportPolicy(allowed_modules={"json", "math"})
        assert p.is_allowed("json") is True
        assert p.is_allowed("os") is False
        assert p.is_allowed("json.decoder") is True

    def test_is_allowed_empty_allowlist_blocks_all(self):
        p = ImportPolicy(allowed_modules={"json"})
        assert p.is_allowed("json") is True
        assert p.is_allowed("math") is False

    def test_blocked_overrides_allowed(self):
        p = ImportPolicy(allowed_modules={"os"}, blocked_modules={"os"})
        assert p.is_allowed("os") is False

    def test_submodule_blocked_by_root(self):
        p = ImportPolicy(blocked_modules={"subprocess"})
        assert p.is_allowed("subprocess.run") is False


class TestNetworkPolicy:
    def test_default_blocks_all(self):
        p = NetworkPolicy()
        assert p.is_host_allowed("anything.com") is False

    def test_exact_endpoint_match(self):
        p = NetworkPolicy(allowed_endpoints=["api.example.com"])
        assert p.is_host_allowed("api.example.com") is True

    def test_subdomain_match(self):
        p = NetworkPolicy(allowed_endpoints=["example.com"])
        assert p.is_host_allowed("sub.example.com") is True

    def test_no_partial_match(self):
        p = NetworkPolicy(allowed_endpoints=["example.com"])
        assert p.is_host_allowed("notexample.com") is False

    def test_empty_endpoints_blocks(self):
        p = NetworkPolicy(allowed_endpoints=[])
        assert p.is_host_allowed("any.host") is False

    def test_multiple_endpoints(self):
        p = NetworkPolicy(allowed_endpoints=["a.com", "b.com"])
        assert p.is_host_allowed("a.com") is True
        assert p.is_host_allowed("b.com") is True
        assert p.is_host_allowed("c.com") is False


class TestResourcePolicy:
    def test_defaults(self):
        p = ResourcePolicy()
        assert p.max_cpu_seconds == 30.0
        assert p.max_memory_bytes == 512 * 1024 * 1024
        assert p.max_file_descriptors == 64
        assert p.max_threads == 1
        assert p.wall_time_seconds == 60.0

    def test_custom_values(self):
        p = ResourcePolicy(max_cpu_seconds=10.0, max_memory_bytes=1024, max_threads=4)
        assert p.max_cpu_seconds == 10.0
        assert p.max_memory_bytes == 1024
        assert p.max_threads == 4


class TestFilesystemPolicy:
    def test_defaults(self):
        p = FilesystemPolicy()
        assert p.read_only_paths == []
        assert p.read_write_paths == []
        assert p.virtual_root is None
        assert p.block_symlinks is True
        assert p.block_absolute_paths is True
        assert p.block_env_access is True

    def test_custom_paths(self):
        p = FilesystemPolicy(
            read_only_paths=["/data"],
            read_write_paths=["/test_rw"],
            virtual_root="/sandbox",
        )
        assert p.read_only_paths == ["/data"]
        assert p.read_write_paths == ["/test_rw"]
        assert p.virtual_root == "/sandbox"


class TestIntrospectionPolicy:
    def test_defaults_block_dangerous_builtins(self):
        p = IntrospectionPolicy()
        assert "eval" in p.blocked_builtins
        assert "exec" in p.blocked_builtins
        assert "compile" in p.blocked_builtins

    def test_defaults_block_dangerous_attributes(self):
        p = IntrospectionPolicy()
        assert "__subclasses__" in p.blocked_attributes
        assert "__globals__" in p.blocked_attributes

    def test_all_dunder_blocking_flags(self):
        p = IntrospectionPolicy()
        assert p.blocked_dunder_access is True
        assert p.block_gc is True
        assert p.block_inspect is True
        assert p.block_frame_access is True
        assert p.block_type_abuse is True


class TestEnvironmentPolicy:
    def test_defaults(self):
        p = EnvironmentPolicy()
        assert p.allowed_env_vars == set()
        assert p.block_os_environ is True
        assert p.sanitized_env == {}


class TestSerializePolicyValue:
    def test_serializes_frozenset(self):
        result = SandboxPolicy._serialize_policy_value(frozenset({1, 2, 3}))
        assert result == [1, 2, 3]

    def test_serializes_frozenset_strings(self):
        result = SandboxPolicy._serialize_policy_value(frozenset({"c", "a", "b"}))
        assert result == ["a", "b", "c"]

    def test_serializes_set(self):
        result = SandboxPolicy._serialize_policy_value({3, 1, 2})
        assert result == [1, 2, 3]

    def test_serializes_list(self):
        result = SandboxPolicy._serialize_policy_value([3, 1, 2])
        assert result == [1, 2, 3]

    def test_serializes_tuple(self):
        result = SandboxPolicy._serialize_policy_value((3, 1, 2))
        assert result == [1, 2, 3]

    def test_serializes_dict_sorted_keys(self):
        result = SandboxPolicy._serialize_policy_value({"b": 2, "a": 1})
        assert list(result.keys()) == ["a", "b"]

    def test_serializes_enum(self):
        result = SandboxPolicy._serialize_policy_value(TrustLevel.UNTRUSTED)
        assert result == "untrusted"

    def test_serializes_dataclass(self):
        rp = ResourcePolicy(max_cpu_seconds=10.0)
        result = SandboxPolicy._serialize_policy_value(rp)
        assert isinstance(result, dict)
        assert result["max_cpu_seconds"] == 10.0

    def test_serializes_primitive(self):
        assert SandboxPolicy._serialize_policy_value(42) == 42
        assert SandboxPolicy._serialize_policy_value("hello") == "hello"
        assert SandboxPolicy._serialize_policy_value(3.14) == 3.14
        assert SandboxPolicy._serialize_policy_value(True) is True
        assert SandboxPolicy._serialize_policy_value(None) is None

    def test_serializes_nested_dataclass(self):
        policy = SandboxPolicy(
            plugin_id="test",
            trust_level="untrusted",
            resource_policy=ResourcePolicy(max_cpu_seconds=5.0),
        )
        result = SandboxPolicy._serialize_policy_value(policy)
        assert isinstance(result, dict)
        assert result["plugin_id"] == "test"
        assert result["resource_policy"]["max_cpu_seconds"] == 5.0

    def test_skips_private_fields(self):
        policy = SandboxPolicy(plugin_id="test")
        result = SandboxPolicy._serialize_policy_value(policy)
        assert "_integrity_hash" not in result

    def test_non_sortable_fallback_is_deterministic(self):
        fs = frozenset({object(), object()})
        result1 = SandboxPolicy._serialize_policy_value(fs)
        result2 = SandboxPolicy._serialize_policy_value(fs)
        assert result1 == result2

    def test_non_sortable_set_fallback(self):
        s = {object(), object()}
        result = SandboxPolicy._serialize_policy_value(s)
        assert isinstance(result, list)
        assert len(result) == 2

    def test_empty_frozenset(self):
        result = SandboxPolicy._serialize_policy_value(frozenset())
        assert result == []

    def test_empty_set(self):
        result = SandboxPolicy._serialize_policy_value(set())
        assert result == []


class TestIntegrityHash:
    def test_compute_integrity_hash_returns_string(self):
        p = SandboxPolicy(plugin_id="test")
        h = p.compute_integrity_hash()
        assert isinstance(h, str)
        assert h.startswith("v2:")

    def test_hash_is_deterministic(self):
        p = SandboxPolicy(plugin_id="test", trust_level="untrusted")
        h1 = p.compute_integrity_hash()
        h2 = p.compute_integrity_hash()
        assert h1 == h2

    def test_different_policies_different_hashes(self):
        p1 = SandboxPolicy(plugin_id="a")
        p2 = SandboxPolicy(plugin_id="b")
        assert p1.compute_integrity_hash() != p2.compute_integrity_hash()

    def test_set_integrity_hash(self):
        p = SandboxPolicy(plugin_id="test")
        assert p._integrity_hash is None
        p.set_integrity_hash()
        assert p._integrity_hash is not None
        assert p._integrity_hash.startswith("v2:")

    def test_verify_integrity_none_hash(self):
        p = SandboxPolicy(plugin_id="test")
        assert p._integrity_hash is None
        assert p.verify_integrity() is True

    def test_verify_integrity_unchanged(self):
        p = SandboxPolicy(plugin_id="test")
        p.set_integrity_hash()
        assert p.verify_integrity() is True

    def test_verify_integrity_tampered(self):
        p = SandboxPolicy(plugin_id="test")
        p.set_integrity_hash()
        p.plugin_id = "tampered"
        assert p.verify_integrity() is False

    def test_verify_integrity_resource_tampered(self):
        p = SandboxPolicy(plugin_id="test")
        p.set_integrity_hash()
        p.resource_policy.max_cpu_seconds = 9999
        assert p.verify_integrity() is False

    def test_hash_stable_with_frozenset_fields(self):
        p1 = SandboxPolicy(
            plugin_id="test",
            import_policy=ImportPolicy(blocked_modules=frozenset({"os", "sys"})),
        )
        p2 = SandboxPolicy(
            plugin_id="test",
            import_policy=ImportPolicy(blocked_modules=frozenset({"sys", "os"})),
        )
        assert p1.compute_integrity_hash() == p2.compute_integrity_hash()


class TestEnforceHardLimits:
    def test_untrusted_write_paths_violation(self):
        p = SandboxPolicy(
            filesystem_policy=FilesystemPolicy(read_write_paths=["/test_rw"]),
        )
        violations = p.enforce_hard_limits(TrustLevel.UNTRUSTED)
        assert any("write paths" in v for v in violations)

    def test_untrusted_threads_violation(self):
        p = SandboxPolicy(
            resource_policy=ResourcePolicy(max_threads=4),
        )
        violations = p.enforce_hard_limits(TrustLevel.UNTRUSTED)
        assert any("threads" in v for v in violations)

    def test_untrusted_metadata_endpoints_violation(self):
        p = SandboxPolicy(
            network_policy=NetworkPolicy(block_metadata_endpoints=False),
        )
        violations = p.enforce_hard_limits(TrustLevel.UNTRUSTED)
        assert any("metadata" in v for v in violations)

    def test_untrusted_cpu_exceeds_hard_limit(self):
        p = SandboxPolicy(
            resource_policy=ResourcePolicy(max_cpu_seconds=9999),
        )
        violations = p.enforce_hard_limits(TrustLevel.UNTRUSTED)
        assert any("max_cpu_seconds" in v for v in violations)

    def test_untrusted_memory_exceeds_hard_limit(self):
        p = SandboxPolicy(
            resource_policy=ResourcePolicy(max_memory_bytes=999 * 1024**3),
        )
        violations = p.enforce_hard_limits(TrustLevel.UNTRUSTED)
        assert any("max_memory_bytes" in v for v in violations)

    def test_trusted_full_allows_write_paths(self):
        p = SandboxPolicy(
            filesystem_policy=FilesystemPolicy(read_write_paths=["/test_rw"]),
        )
        violations = p.enforce_hard_limits(TrustLevel.TRUSTED_FULL)
        assert not any("write paths" in v for v in violations)

    def test_trusted_full_allows_threads(self):
        p = SandboxPolicy(
            resource_policy=ResourcePolicy(max_threads=8),
        )
        violations = p.enforce_hard_limits(TrustLevel.TRUSTED_FULL)
        assert not any("threads" in v for v in violations)

    def test_no_violations_under_limits(self):
        p = SandboxPolicy(
            resource_policy=ResourcePolicy(max_cpu_seconds=10, max_memory_bytes=1024),
            filesystem_policy=FilesystemPolicy(read_write_paths=[]),
        )
        violations = p.enforce_hard_limits(TrustLevel.UNTRUSTED)
        assert violations == []

    def test_trusted_limited_cpu_hard_limit(self):
        p = SandboxPolicy(
            resource_policy=ResourcePolicy(max_cpu_seconds=9999),
        )
        violations = p.enforce_hard_limits(TrustLevel.TRUSTED_LIMITED)
        assert any("max_cpu_seconds" in v for v in violations)

    def test_trusted_full_cpu_hard_limit(self):
        p = SandboxPolicy(
            resource_policy=ResourcePolicy(max_cpu_seconds=9999),
        )
        violations = p.enforce_hard_limits(TrustLevel.TRUSTED_FULL)
        assert any("max_cpu_seconds" in v for v in violations)

    def test_all_untrusted_violations_at_once(self):
        p = SandboxPolicy(
            resource_policy=ResourcePolicy(
                max_cpu_seconds=9999,
                max_memory_bytes=999 * 1024**3,
                max_threads=4,
            ),
            filesystem_policy=FilesystemPolicy(read_write_paths=["/test_rw"]),
            network_policy=NetworkPolicy(block_metadata_endpoints=False),
        )
        violations = p.enforce_hard_limits(TrustLevel.UNTRUSTED)
        assert len(violations) >= 4


class TestFromTrustLevel:
    def test_untrusted_defaults(self):
        p = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "test_plugin")
        assert p.plugin_id == "test_plugin"
        assert p.trust_level == "untrusted"
        assert p._integrity_hash is not None

    def test_trusted_full_higher_limits(self):
        p_full = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_FULL, "full")
        p_untrusted = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "untrusted")
        assert p_full.resource_policy.max_cpu_seconds >= p_untrusted.resource_policy.max_cpu_seconds

    def test_resource_multiplier_applied(self):
        base_cpu = 30.0
        base_mem = 512 * 1024 * 1024
        p = SandboxPolicy.from_trust_level(
            TrustLevel.TRUSTED_LIMITED, "test",
            max_cpu_seconds=base_cpu, max_memory_bytes=base_mem,
        )
        expected_cpu = min(base_cpu * 2.0, _TRUST_MAX_CPU_HARD_LIMITS[TrustLevel.TRUSTED_LIMITED])
        expected_mem = min(int(base_mem * 2.0), _TRUST_MAX_MEMORY_HARD_LIMITS[TrustLevel.TRUSTED_LIMITED])
        assert p.resource_policy.max_cpu_seconds == expected_cpu
        assert p.resource_policy.max_memory_bytes == expected_mem

    def test_cpu_capped_at_hard_limit(self):
        p = SandboxPolicy.from_trust_level(
            TrustLevel.UNTRUSTED, "test",
            max_cpu_seconds=9999,
        )
        assert p.resource_policy.max_cpu_seconds <= _TRUST_MAX_CPU_HARD_LIMITS[TrustLevel.UNTRUSTED]

    def test_memory_capped_at_hard_limit(self):
        p = SandboxPolicy.from_trust_level(
            TrustLevel.UNTRUSTED, "test",
            max_memory_bytes=999 * 1024**3,
        )
        assert p.resource_policy.max_memory_bytes <= _TRUST_MAX_MEMORY_HARD_LIMITS[TrustLevel.UNTRUSTED]

    def test_custom_network_endpoints(self):
        p = SandboxPolicy.from_trust_level(
            TrustLevel.UNTRUSTED, "test",
            network_endpoints=["api.example.com"],
        )
        assert p.network_policy.allowed_endpoints == ["api.example.com"]

    def test_custom_read_only_paths(self):
        p = SandboxPolicy.from_trust_level(
            TrustLevel.UNTRUSTED, "test",
            read_only_paths=["/data"],
        )
        assert p.filesystem_policy.read_only_paths == ["/data"]

    def test_integrity_hash_set(self):
        p = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "test")
        assert p.verify_integrity() is True

    def test_import_policy_by_trust_level(self):
        p_untrusted = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "t")
        p_full = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_FULL, "t")
        assert len(p_untrusted.import_policy.blocked_modules) > len(p_full.import_policy.blocked_modules)

    def test_introspection_by_trust_level(self):
        p_untrusted = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "t")
        p_full = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_FULL, "t")
        assert len(p_untrusted.introspection_policy.blocked_builtins) > len(p_full.introspection_policy.blocked_builtins)


class TestFromManifest:
    def _make_manifest(
        self,
        *,
        manifest_id="test_manifest",
        trust_level="untrusted",
        requires_network=False,
        network_endpoints=None,
        max_cpu_seconds=30,
        max_memory="512MB",
        artifacts=None,
        has_fs_write=False,
    ):
        class FakeResources:
            def __init__(self):
                self.max_cpu_seconds = max_cpu_seconds
                self.max_memory = max_memory

        class FakeNetwork:
            def __init__(self):
                self.allowed_endpoints = network_endpoints or []

        class FakeManifest:
            pass

        m = FakeManifest()
        m.id = manifest_id
        m.trust_level = trust_level
        m.resources = FakeResources()
        m.network = FakeNetwork()
        m.artifacts = artifacts or []

        def requires_network():
            return requires_network

        m.requires_network = requires_network

        if has_fs_write:
            m.permissions = {"filesystem_write"}
            m.has_permission = lambda p: p == "filesystem_write"
        else:
            m.permissions = set()
            m.has_permission = lambda p: False

        return m

    def test_basic_untrusted_manifest(self):
        m = self._make_manifest()
        p = SandboxPolicy.from_manifest(m)
        assert p.plugin_id == "test_manifest"
        assert p.trust_level == "untrusted"
        assert p._integrity_hash is not None

    def test_manifest_with_network(self):
        m = self._make_manifest(
            requires_network=True,
            network_endpoints=["api.example.com"],
        )
        p = SandboxPolicy.from_manifest(m)
        assert p.network_policy.allowed_endpoints == ["api.example.com"]

    def test_manifest_without_network_empty_endpoints(self):
        m = self._make_manifest(requires_network=False, network_endpoints=["api.example.com"])
        p = SandboxPolicy.from_manifest(m)
        assert p.network_policy.allowed_endpoints == []

    def test_manifest_trusted_full_with_write(self):
        m = self._make_manifest(
            trust_level="trusted_full",
            has_fs_write=True,
            artifacts=["/output"],
        )
        p = SandboxPolicy.from_manifest(m)
        assert p.filesystem_policy.read_write_paths == ["/output"]
        assert p.filesystem_policy.read_only_paths == ["/output"]

    def test_manifest_untrusted_no_write(self):
        m = self._make_manifest(
            trust_level="untrusted",
            has_fs_write=True,
            artifacts=["/output"],
        )
        p = SandboxPolicy.from_manifest(m)
        assert p.filesystem_policy.read_write_paths == []

    def test_manifest_invalid_trust_defaults_untrusted(self):
        m = self._make_manifest(trust_level="invalid_level")
        p = SandboxPolicy.from_manifest(m)
        assert p.trust_level == "untrusted"

    def test_manifest_resource_limits(self):
        m = self._make_manifest(max_cpu_seconds=60, max_memory="1GB")
        p = SandboxPolicy.from_manifest(m)
        assert p.resource_policy.max_cpu_seconds <= 120.0
        assert p.resource_policy.max_memory_bytes <= 1024**3

    def test_manifest_no_resources_attribute(self):
        class MinimalManifest:
            id = "minimal"
            trust_level = "untrusted"
            artifacts: ClassVar[list[str]] = []

        m = MinimalManifest()
        p = SandboxPolicy.from_manifest(m)
        assert p.plugin_id == "minimal"


class TestTrustedPolicy:
    def test_creates_policy(self):
        p = SandboxPolicy.trusted_policy()
        assert p.plugin_id == "trusted"
        assert p.trust_level == "trusted_full"
        assert p._integrity_hash is not None

    def test_custom_plugin_id(self):
        p = SandboxPolicy.trusted_policy("my_plugin")
        assert p.plugin_id == "my_plugin"

    def test_has_limited_blocked_modules(self):
        p = SandboxPolicy.trusted_policy()
        assert "subprocess" in p.import_policy.blocked_modules
        assert "os" not in p.import_policy.blocked_modules

    def test_high_resource_limits(self):
        p = SandboxPolicy.trusted_policy()
        assert p.resource_policy.max_cpu_seconds == 300
        assert p.resource_policy.max_memory_bytes == 2 * 1024**3

    def test_integrity_valid(self):
        p = SandboxPolicy.trusted_policy()
        assert p.verify_integrity() is True


class TestParseMemory:
    def test_bytes(self):
        assert _parse_memory("1B") == 1

    def test_kilobytes(self):
        assert _parse_memory("1KB") == 1024

    def test_megabytes(self):
        assert _parse_memory("1MB") == 1024**2

    def test_gigabytes(self):
        assert _parse_memory("1GB") == 1024**3

    def test_float_gb(self):
        assert _parse_memory("1.5GB") == int(1.5 * 1024**3)

    def test_float_mb(self):
        assert _parse_memory("0.5MB") == int(0.5 * 1024**2)

    def test_case_insensitive(self):
        assert _parse_memory("1mb") == _parse_memory("1MB")
        assert _parse_memory("1gb") == _parse_memory("1GB")
        assert _parse_memory("1kb") == _parse_memory("1KB")

    def test_whitespace(self):
        assert _parse_memory("  256MB  ") == 256 * 1024**2

    def test_plain_number(self):
        assert _parse_memory("1048576") == 1048576

    def test_large_value(self):
        assert _parse_memory("4GB") == 4 * 1024**3

    def test_zero(self):
        assert _parse_memory("0") == 0

    def test_zero_bytes(self):
        assert _parse_memory("0B") == 0


class TestTrustPresets:
    def test_all_trust_levels_have_import_presets(self):
        for level in TrustLevel:
            assert level in _TRUST_IMPORT_PRESETS

    def test_all_trust_levels_have_introspection_presets(self):
        for level in TrustLevel:
            assert level in _TRUST_INTROSPECTION_PRESETS

    def test_all_trust_levels_have_resource_multipliers(self):
        for level in TrustLevel:
            assert level in _TRUST_RESOURCE_MULTIPLIERS

    def test_all_trust_levels_have_filesystem_rw(self):
        for level in TrustLevel:
            assert level in _TRUST_FILESYSTEM_RW

    def test_all_trust_levels_have_cpu_hard_limits(self):
        for level in TrustLevel:
            assert level in _TRUST_MAX_CPU_HARD_LIMITS

    def test_all_trust_levels_have_memory_hard_limits(self):
        for level in TrustLevel:
            assert level in _TRUST_MAX_MEMORY_HARD_LIMITS

    def test_all_trust_levels_have_environment_presets(self):
        for level in TrustLevel:
            assert level in _TRUST_ENVIRONMENT_PRESETS

    def test_resource_multipliers_ordering(self):
        assert _TRUST_RESOURCE_MULTIPLIERS[TrustLevel.UNTRUSTED] < _TRUST_RESOURCE_MULTIPLIERS[TrustLevel.TRUSTED_LIMITED]
        assert _TRUST_RESOURCE_MULTIPLIERS[TrustLevel.TRUSTED_LIMITED] < _TRUST_RESOURCE_MULTIPLIERS[TrustLevel.TRUSTED_FULL]

    def test_hard_limits_ordering(self):
        assert _TRUST_MAX_CPU_HARD_LIMITS[TrustLevel.UNTRUSTED] < _TRUST_MAX_CPU_HARD_LIMITS[TrustLevel.TRUSTED_LIMITED]
        assert _TRUST_MAX_CPU_HARD_LIMITS[TrustLevel.TRUSTED_LIMITED] < _TRUST_MAX_CPU_HARD_LIMITS[TrustLevel.TRUSTED_FULL]
        assert _TRUST_MAX_MEMORY_HARD_LIMITS[TrustLevel.UNTRUSTED] < _TRUST_MAX_MEMORY_HARD_LIMITS[TrustLevel.TRUSTED_LIMITED]
        assert _TRUST_MAX_MEMORY_HARD_LIMITS[TrustLevel.TRUSTED_LIMITED] < _TRUST_MAX_MEMORY_HARD_LIMITS[TrustLevel.TRUSTED_FULL]

    def test_untrusted_no_filesystem_write(self):
        assert _TRUST_FILESYSTEM_RW[TrustLevel.UNTRUSTED] is False

    def test_trusted_full_has_filesystem_write(self):
        assert _TRUST_FILESYSTEM_RW[TrustLevel.TRUSTED_FULL] is True

    def test_untrusted_environment_blocks_os_environ(self):
        ep = _TRUST_ENVIRONMENT_PRESETS[TrustLevel.UNTRUSTED]
        assert ep.block_os_environ is True
        assert ep.allowed_env_vars == set()

    def test_trusted_full_environment_allows_vars(self):
        ep = _TRUST_ENVIRONMENT_PRESETS[TrustLevel.TRUSTED_FULL]
        assert ep.block_os_environ is False
        assert "HOME" in ep.allowed_env_vars
        assert "PATH" in ep.allowed_env_vars
