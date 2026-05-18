"""
Comprehensive tests for policy.py fixes.

Covers:
  1. frozenset handling in _serialize_policy_value
  2. Deterministic hash fallback for mixed-type collections
  3. _parse_memory negative value protection
  4. Trust level validation flow and SandboxContext activation
  5. Integrity hash determinism
  6. Hard limits enforcement per trust level
  7. from_trust_level factory method for all trust levels
  8. from_manifest factory method
  9. Edge cases and boundary values
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

import pytest

from engine.plugins.sandbox.core.context import (
    _MAX_CPU_SECONDS_LIMITED,
    _MAX_CPU_SECONDS_UNTRUSTED,
    _MIN_BLOCKED_MODULES_LIMITED,
    _MIN_BLOCKED_MODULES_UNTRUSTED,
    SandboxContext,
)
from engine.plugins.sandbox.core.policy import (
    FilesystemPolicy,
    ImportPolicy,
    NetworkPolicy,
    ResourcePolicy,
    SandboxPolicy,
    _parse_memory,
)
from engine.plugins.sandbox.core.violation import SandboxViolation
from engine.plugins.trust_levels import TrustLevel

# ─── _serialize_policy_value: frozenset handling ────────────────────────


class TestSerializePolicyValueFrozenset:
    def test_frozenset_of_strings(self) -> None:
        result = SandboxPolicy._serialize_policy_value(frozenset({"b", "a", "c"}))
        assert result == ["a", "b", "c"]

    def test_frozenset_of_ints(self) -> None:
        result = SandboxPolicy._serialize_policy_value(frozenset({3, 1, 2}))
        assert result == [1, 2, 3]

    def test_empty_frozenset(self) -> None:
        result = SandboxPolicy._serialize_policy_value(frozenset())
        assert result == []

    def test_frozenset_nested_in_dataclass(self) -> None:
        @dataclass
        class FakeWithFrozenset:
            items: frozenset[str] = field(default_factory=frozenset)

        obj = FakeWithFrozenset(items=frozenset({"z", "a"}))
        result = SandboxPolicy._serialize_policy_value(obj)
        assert result == {"items": ["a", "z"]}


# ─── _serialize_policy_value: deterministic fallback ────────────────────


class TestSerializePolicyValueDeterministicFallback:
    def test_mixed_types_in_set_sorted_by_repr(self) -> None:
        val = {2, "a", True}
        result = SandboxPolicy._serialize_policy_value(val)
        assert isinstance(result, list)
        assert len(result) == len(val)
        r1 = SandboxPolicy._serialize_policy_value(val)
        r2 = SandboxPolicy._serialize_policy_value(val)
        assert r1 == r2

    def test_mixed_types_in_list_deterministic(self) -> None:
        val = [42, "hello", 3.14, None]
        r1 = SandboxPolicy._serialize_policy_value(val)
        r2 = SandboxPolicy._serialize_policy_value(val)
        assert r1 == r2

    def test_set_with_unorderable_types(self) -> None:
        val = {(1, 2), (3, 4)}
        r1 = SandboxPolicy._serialize_policy_value(val)
        r2 = SandboxPolicy._serialize_policy_value(val)
        assert r1 == r2

    def test_enum_serialization(self) -> None:
        class Color(enum.Enum):
            RED = "red"
            BLUE = "blue"

        result = SandboxPolicy._serialize_policy_value(Color.RED)
        assert result == "red"

    def test_enum_in_set(self) -> None:
        class Color(enum.Enum):
            RED = "red"
            BLUE = "blue"

        val = {Color.RED, Color.BLUE}
        result = SandboxPolicy._serialize_policy_value(val)
        assert isinstance(result, list)
        assert "red" in result
        assert "blue" in result

    def test_dict_serialization_sorted_keys(self) -> None:
        val = {"z_key": 1, "a_key": 2, "m_key": 3}
        result = SandboxPolicy._serialize_policy_value(val)
        assert list(result.keys()) == ["a_key", "m_key", "z_key"]

    def test_nested_dict_with_sets(self) -> None:
        val = {"outer": {"inner": frozenset({3, 1, 2})}}
        result = SandboxPolicy._serialize_policy_value(val)
        assert result == {"outer": {"inner": [1, 2, 3]}}

    def test_tuple_serialization(self) -> None:
        result = SandboxPolicy._serialize_policy_value((3, 1, 2))
        assert result == [1, 2, 3]

    def test_plain_value_passthrough(self) -> None:
        assert SandboxPolicy._serialize_policy_value(42) == 42
        assert SandboxPolicy._serialize_policy_value("hello") == "hello"
        assert SandboxPolicy._serialize_policy_value(3.14) == 3.14
        assert SandboxPolicy._serialize_policy_value(None) is None

    def test_bool_passthrough(self) -> None:
        assert SandboxPolicy._serialize_policy_value(True) is True
        assert SandboxPolicy._serialize_policy_value(False) is False


# ─── Integrity hash determinism ─────────────────────────────────────────


class TestIntegrityHashDeterminism:
    def test_same_policy_same_hash(self) -> None:
        p1 = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "test")
        p2 = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "test")
        assert p1.compute_integrity_hash() == p2.compute_integrity_hash()

    def test_hash_stable_across_multiple_calls(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_LIMITED, "stable")
        hashes = [policy.compute_integrity_hash() for _ in range(10)]
        assert len(set(hashes)) == 1

    def test_different_policies_different_hashes(self) -> None:
        p1 = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "a")
        p2 = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_FULL, "b")
        assert p1.compute_integrity_hash() != p2.compute_integrity_hash()

    def test_hash_changes_on_mutation(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "mutable")
        original_hash = policy.compute_integrity_hash()
        policy.resource_policy.max_cpu_seconds = 999
        mutated_hash = policy.compute_integrity_hash()
        assert original_hash != mutated_hash
        assert not policy.verify_integrity()

    def test_verify_integrity_true_when_set(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "verify_ok")
        assert policy.verify_integrity()

    def test_verify_integrity_true_when_none(self) -> None:
        policy = SandboxPolicy(plugin_id="no_hash")
        assert policy._integrity_hash is None
        assert policy.verify_integrity()

    def test_hash_format_is_v2_sha256(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "fmt")
        h = policy.compute_integrity_hash()
        assert h.startswith("v2:")
        assert len(h) == 2 + 1 + 64

    def test_frozenset_in_policy_doesnt_break_hash(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "frozen")
        policy.import_policy.blocked_modules = frozenset({"os", "sys", "subprocess"})  # type: ignore[assignment]
        h = policy.compute_integrity_hash()
        assert h.startswith("v2:")


# ─── _parse_memory negative value protection ────────────────────────────


class TestParseMemoryNegativeProtection:
    def test_negative_one_returns_zero(self) -> None:
        assert _parse_memory("-1") == 0

    def test_negative_with_unit(self) -> None:
        assert _parse_memory("-512MB") == 0

    def test_negative_float_gb(self) -> None:
        assert _parse_memory("-1.5GB") == 0

    def test_zero_returns_zero(self) -> None:
        assert _parse_memory("0") == 0

    def test_zero_with_unit(self) -> None:
        assert _parse_memory("0GB") == 0

    def test_positive_value_unaffected(self) -> None:
        assert _parse_memory("512MB") == 512 * 1024**2

    def test_positive_one_gb(self) -> None:
        assert _parse_memory("1GB") == 1024**3

    def test_positive_float(self) -> None:
        assert _parse_memory("1.5GB") == int(1.5 * 1024**3)

    def test_whitespace_handling(self) -> None:
        assert _parse_memory("  256MB  ") == 256 * 1024**2

    def test_case_insensitive(self) -> None:
        assert _parse_memory("1gb") == _parse_memory("1GB")

    def test_kb_unit(self) -> None:
        assert _parse_memory("1KB") == 1024

    def test_b_unit(self) -> None:
        assert _parse_memory("1B") == 1

    def test_plain_number(self) -> None:
        assert _parse_memory("1048576") == 1048576

    def test_negative_whitespace(self) -> None:
        assert _parse_memory("  -10  ") == 0

    def test_negative_kb(self) -> None:
        assert _parse_memory("-100KB") == 0


# ─── Trust level validation flow ────────────────────────────────────────


class TestTrustLevelValidationFlow:
    def test_bare_untrusted_policy_fails_validation(self) -> None:
        policy = SandboxPolicy(plugin_id="bare")
        ctx = SandboxContext(policy)
        assert not ctx.validate_trust_level()

    def test_from_trust_level_untrusted_passes_validation(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "ok_untrusted")
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level()

    def test_from_trust_level_limited_passes_validation(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_LIMITED, "ok_limited")
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level()

    def test_from_trust_level_full_passes_validation(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_FULL, "ok_full")
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level()

    def test_untrusted_with_too_few_blocked_modules_fails(self) -> None:
        blocked = {"os", "sys", "subprocess"}
        assert len(blocked) < _MIN_BLOCKED_MODULES_UNTRUSTED
        policy = SandboxPolicy(
            plugin_id="few_modules",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules=blocked),
        )
        ctx = SandboxContext(policy)
        assert not ctx.validate_trust_level()

    def test_untrusted_with_excessive_cpu_fails(self) -> None:
        policy = SandboxPolicy(
            plugin_id="high_cpu",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules=_get_enough_blocked_modules()),
            resource_policy=ResourcePolicy(max_cpu_seconds=_MAX_CPU_SECONDS_UNTRUSTED + 1),
        )
        ctx = SandboxContext(policy)
        assert not ctx.validate_trust_level()

    def test_untrusted_with_write_paths_fails(self) -> None:
        policy = SandboxPolicy(
            plugin_id="rw_paths",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules=_get_enough_blocked_modules()),
            filesystem_policy=FilesystemPolicy(read_write_paths=["/tmp/write"]),  # noqa: S108
        )
        ctx = SandboxContext(policy)
        assert not ctx.validate_trust_level()

    def test_untrusted_with_threads_fails(self) -> None:
        policy = SandboxPolicy(
            plugin_id="threads",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules=_get_enough_blocked_modules()),
            resource_policy=ResourcePolicy(max_threads=4),
        )
        ctx = SandboxContext(policy)
        assert not ctx.validate_trust_level()

    def test_limited_with_too_few_blocked_modules_fails(self) -> None:
        blocked = {"os"}
        assert len(blocked) < _MIN_BLOCKED_MODULES_LIMITED
        policy = SandboxPolicy(
            plugin_id="limited_few",
            trust_level="trusted_limited",
            import_policy=ImportPolicy(blocked_modules=blocked),
        )
        ctx = SandboxContext(policy)
        assert not ctx.validate_trust_level()

    def test_limited_with_excessive_cpu_fails(self) -> None:
        policy = SandboxPolicy(
            plugin_id="limited_cpu",
            trust_level="trusted_limited",
            import_policy=ImportPolicy(blocked_modules=_get_enough_blocked_modules()),
            resource_policy=ResourcePolicy(max_cpu_seconds=_MAX_CPU_SECONDS_LIMITED + 1),
        )
        ctx = SandboxContext(policy)
        assert not ctx.validate_trust_level()


def _get_enough_blocked_modules() -> set[str]:
    return {
        "os", "subprocess", "shutil", "pathlib", "io", "_io",
        "socket", "_socket", "http", "urllib", "ftplib",
    }


# ─── SandboxContext activation ──────────────────────────────────────────


class TestSandboxContextActivation:
    def test_activate_untrusted_succeeds(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "act_untrusted")
        ctx = SandboxContext(policy)
        try:
            ctx.activate()
            assert ctx.is_active is True
        finally:
            ctx.deactivate()
            ctx.cleanup()

    def test_activate_limited_succeeds(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_LIMITED, "act_limited")
        ctx = SandboxContext(policy)
        try:
            ctx.activate()
            assert ctx.is_active is True
        finally:
            ctx.deactivate()
            ctx.cleanup()

    def test_activate_full_succeeds(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_FULL, "act_full")
        ctx = SandboxContext(policy)
        try:
            ctx.activate()
            assert ctx.is_active is True
        finally:
            ctx.deactivate()
            ctx.cleanup()

    def test_activate_bare_policy_raises(self) -> None:
        policy = SandboxPolicy(plugin_id="bare")
        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation, match="Trust level policy validation failed"):
            ctx.activate()

    def test_context_manager_with_valid_policy(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "cm")
        ctx = SandboxContext(policy)
        with ctx:
            assert ctx.is_active is True
        assert ctx.is_active is False
        ctx.cleanup()

    def test_context_manager_with_invalid_policy_raises(self) -> None:
        policy = SandboxPolicy(plugin_id="bad_cm")
        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation), ctx:
            pass

    def test_deactivate_idempotent(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "deact")
        ctx = SandboxContext(policy)
        ctx.deactivate()
        ctx.deactivate()
        assert ctx.is_active is False

    def test_activate_idempotent(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "idem")
        ctx = SandboxContext(policy)
        try:
            ctx.activate()
            ctx.activate()
            assert ctx.is_active is True
        finally:
            ctx.deactivate()
            ctx.cleanup()

    def test_trust_level_property(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "prop_test")
        ctx = SandboxContext(policy)
        assert ctx.trust_level == TrustLevel.UNTRUSTED

    def test_event_logger_has_plugin_id(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "log_test")
        ctx = SandboxContext(policy)
        assert ctx.event_logger._plugin_id == "log_test"


# ─── Hard limits enforcement ────────────────────────────────────────────


class TestEnforceHardLimits:
    def test_untrusted_exceeds_cpu_hard_limit(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "cpu_hard")
        policy.resource_policy.max_cpu_seconds = 999
        violations = policy.enforce_hard_limits(TrustLevel.UNTRUSTED)
        assert any("max_cpu_seconds" in v for v in violations)

    def test_untrusted_exceeds_memory_hard_limit(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "mem_hard")
        policy.resource_policy.max_memory_bytes = 10 * 1024**3
        violations = policy.enforce_hard_limits(TrustLevel.UNTRUSTED)
        assert any("max_memory_bytes" in v for v in violations)

    def test_untrusted_with_write_paths_violation(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "rw_hard")
        policy.filesystem_policy.read_write_paths = ["/tmp/write"]  # noqa: S108
        violations = policy.enforce_hard_limits(TrustLevel.UNTRUSTED)
        assert any("write paths" in v for v in violations)

    def test_untrusted_with_threads_violation(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "thr_hard")
        policy.resource_policy.max_threads = 4
        violations = policy.enforce_hard_limits(TrustLevel.UNTRUSTED)
        assert any("threads" in v for v in violations)

    def test_untrusted_metadata_endpoints_not_blocked(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "meta_hard")
        policy.network_policy.block_metadata_endpoints = False
        violations = policy.enforce_hard_limits(TrustLevel.UNTRUSTED)
        assert any("metadata" in v for v in violations)

    def test_trusted_full_no_untrusted_violations(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_FULL, "full_ok")
        violations = policy.enforce_hard_limits(TrustLevel.TRUSTED_FULL)
        assert len(violations) == 0

    def test_trusted_limited_no_untrusted_violations(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_LIMITED, "limited_ok")
        violations = policy.enforce_hard_limits(TrustLevel.TRUSTED_LIMITED)
        assert len(violations) == 0


# ─── from_trust_level factory method ────────────────────────────────────


class TestFromTrustLevelFactory:
    def test_untrusted_has_many_blocked_modules(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "blk")
        assert len(policy.import_policy.blocked_modules) >= _MIN_BLOCKED_MODULES_UNTRUSTED

    def test_limited_has_many_blocked_modules(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_LIMITED, "blk")
        assert len(policy.import_policy.blocked_modules) >= _MIN_BLOCKED_MODULES_LIMITED

    def test_full_has_few_blocked_modules(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_FULL, "blk")
        assert len(policy.import_policy.blocked_modules) == 3

    def test_untrusted_resource_multiplier_is_1(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "mult")
        assert policy.resource_policy.max_cpu_seconds == 30.0

    def test_limited_resource_multiplier_is_2(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_LIMITED, "mult")
        assert policy.resource_policy.max_cpu_seconds == 60.0

    def test_full_resource_multiplier_is_4_capped(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_FULL, "mult")
        assert policy.resource_policy.max_cpu_seconds == 120.0

    def test_untrusted_no_write_paths(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "rw")
        assert policy.filesystem_policy.read_write_paths == []

    def test_untrusted_environment_is_restricted(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "env")
        assert policy.environment_policy.block_os_environ is True
        assert len(policy.environment_policy.allowed_env_vars) == 0

    def test_full_environment_is_permissive(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_FULL, "env")
        assert policy.environment_policy.block_os_environ is False
        assert "HOME" in policy.environment_policy.allowed_env_vars

    def test_integrity_hash_set_on_creation(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "hash")
        assert policy._integrity_hash is not None
        assert policy._integrity_hash.startswith("v2:")

    def test_custom_cpu_seconds_capped_by_hard_limit(self) -> None:
        policy = SandboxPolicy.from_trust_level(
            TrustLevel.UNTRUSTED, "cap", max_cpu_seconds=999.0
        )
        assert policy.resource_policy.max_cpu_seconds <= 120.0

    def test_custom_memory_capped_by_hard_limit(self) -> None:
        policy = SandboxPolicy.from_trust_level(
            TrustLevel.UNTRUSTED, "cap", max_memory_bytes=10 * 1024**3
        )
        assert policy.resource_policy.max_memory_bytes <= 1024**3

    def test_custom_network_endpoints(self) -> None:
        policy = SandboxPolicy.from_trust_level(
            TrustLevel.UNTRUSTED, "net",
            network_endpoints=["api.example.com"],
        )
        assert policy.network_policy.allowed_endpoints == ["api.example.com"]

    def test_custom_read_only_paths(self) -> None:
        policy = SandboxPolicy.from_trust_level(
            TrustLevel.UNTRUSTED, "ro",
            read_only_paths=["/data"],
        )
        assert policy.filesystem_policy.read_only_paths == ["/data"]

    def test_trust_level_string_matches_enum(self) -> None:
        for level in TrustLevel:
            policy = SandboxPolicy.from_trust_level(level, "check")
            assert policy.trust_level == level.value


# ─── from_manifest factory method ───────────────────────────────────────


class TestFromManifestFactory:
    def test_minimal_manifest(self) -> None:
        manifest = type("M", (), {"id": "test_plugin", "trust_level": "untrusted"})()
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy.plugin_id == "test_plugin"
        assert policy.trust_level == "untrusted"

    def test_manifest_with_resources(self) -> None:
        manifest = type("M", (), {
            "id": "res_plugin",
            "trust_level": "trusted_limited",
            "resources": type("R", (), {"max_cpu_seconds": 60, "max_memory": "1GB"})(),
        })()
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy.resource_policy.max_cpu_seconds > 0
        assert policy.resource_policy.max_memory_bytes > 0

    def test_manifest_with_network(self) -> None:
        network = type("N", (), {"allowed_endpoints": ["api.example.com"]})()
        manifest = type("M", (), {
            "id": "net_plugin",
            "trust_level": "trusted_limited",
            "network": network,
            "requires_network": lambda self: True,
        })()
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy.network_policy.allowed_endpoints == ["api.example.com"]

    def test_manifest_no_network(self) -> None:
        manifest = type("M", (), {"id": "no_net", "trust_level": "untrusted"})()
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy.network_policy.allowed_endpoints == []

    def test_manifest_with_artifacts(self) -> None:
        manifest = type("M", (), {
            "id": "art_plugin",
            "trust_level": "trusted_full",
            "artifacts": ["/data/file1.csv"],
            "permissions": {"filesystem_write"},
            "has_permission": lambda self, p: p == "filesystem_write",
        })()
        policy = SandboxPolicy.from_manifest(manifest)
        assert "/data/file1.csv" in policy.filesystem_policy.read_write_paths

    def test_manifest_untrusted_no_write_paths(self) -> None:
        manifest = type("M", (), {
            "id": "untrust_art",
            "trust_level": "untrusted",
            "artifacts": ["/data/file1.csv"],
            "permissions": {"filesystem_write"},
            "has_permission": lambda self, p: p == "filesystem_write",
        })()
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy.filesystem_policy.read_write_paths == []

    def test_manifest_integrity_hash_set(self) -> None:
        manifest = type("M", (), {"id": "hash_test", "trust_level": "untrusted"})()
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy._integrity_hash is not None

    def test_manifest_invalid_trust_defaults_untrusted(self) -> None:
        manifest = type("M", (), {"id": "bad_trust", "trust_level": "invalid"})()
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy.trust_level == "untrusted"

    def test_manifest_missing_id_defaults_unknown(self) -> None:
        manifest = type("M", (), {"trust_level": "untrusted"})()
        policy = SandboxPolicy.from_manifest(manifest)
        assert policy.plugin_id == "unknown"


# ─── trusted_policy factory ─────────────────────────────────────────────


class TestTrustedPolicyFactory:
    def test_creates_trusted_full_policy(self) -> None:
        policy = SandboxPolicy.trusted_policy()
        assert policy.trust_level == "trusted_full"

    def test_custom_plugin_id(self) -> None:
        policy = SandboxPolicy.trusted_policy(plugin_id="custom")
        assert policy.plugin_id == "custom"

    def test_has_minimal_blocked_modules(self) -> None:
        policy = SandboxPolicy.trusted_policy()
        assert "subprocess" in policy.import_policy.blocked_modules

    def test_integrity_hash_set(self) -> None:
        policy = SandboxPolicy.trusted_policy()
        assert policy._integrity_hash is not None


# ─── ImportPolicy.is_allowed ────────────────────────────────────────────


class TestImportPolicyIsAllowed:
    def test_blocked_root_module(self) -> None:
        policy = ImportPolicy(blocked_modules={"os"})
        assert not policy.is_allowed("os")

    def test_blocked_submodule(self) -> None:
        policy = ImportPolicy(blocked_modules={"os"})
        assert not policy.is_allowed("os.path")

    def test_allowed_when_no_blocklist(self) -> None:
        policy = ImportPolicy(blocked_modules=set())
        assert policy.is_allowed("os")

    def test_allowed_when_in_allowlist(self) -> None:
        policy = ImportPolicy(allowed_modules={"numpy"})
        assert policy.is_allowed("numpy")
        assert not policy.is_allowed("os")

    def test_blocked_overrides_allowed(self) -> None:
        policy = ImportPolicy(allowed_modules={"os"}, blocked_modules={"os"})
        assert not policy.is_allowed("os")

    def test_empty_allowlist_allows_all(self) -> None:
        policy = ImportPolicy(allowed_modules=set(), blocked_modules=set())
        assert policy.is_allowed("anything")

    def test_submodule_checked_by_root(self) -> None:
        policy = ImportPolicy(allowed_modules={"numpy"})
        assert policy.is_allowed("numpy.linalg")
        assert not policy.is_allowed("pandas.io")


# ─── NetworkPolicy.is_host_allowed ──────────────────────────────────────


class TestNetworkPolicyIsHostAllowed:
    def test_empty_endpoints_blocks_all(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=[])
        assert not policy.is_host_allowed("any.host.com")

    def test_exact_match(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["api.example.com"])
        assert policy.is_host_allowed("api.example.com")

    def test_subdomain_match(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["example.com"])
        assert policy.is_host_allowed("api.example.com")

    def test_no_partial_match(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["example.com"])
        assert not policy.is_host_allowed("notexample.com")

    def test_unrelated_host_blocked(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["api.example.com"])
        assert not policy.is_host_allowed("other.com")
