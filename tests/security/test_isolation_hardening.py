"""Tests for hardened isolation: metadata endpoint blocking, extended path blocking, and integrity."""

from __future__ import annotations

import builtins

import pytest

from engine.plugins.sandbox.core.context import SandboxContext
from engine.plugins.sandbox.core.policy import (
    FilesystemPolicy,
    ImportPolicy,
    IntrospectionPolicy,
    NetworkPolicy,
    ResourcePolicy,
    SandboxPolicy,
)
from engine.plugins.sandbox.core.violation import SandboxViolation
from engine.plugins.sandbox.layers.filesystem_isolation import (
    _BLOCKED_SYSTEM_PREFIXES,
    FilesystemIsolation,
)
from engine.plugins.sandbox.layers.introspection_guard import (
    _EXPLICITLY_BLOCKED_ATTRS,
    IntrospectionGuard,
)
from engine.plugins.sandbox.layers.network_guard import (
    _METADATA_ENDPOINTS,
    NetworkGuard,
)
from engine.plugins.sandbox.monitoring.event_logger import SecurityEventLogger
from engine.plugins.sandbox.monitoring.metrics import SandboxMetricsCollector
from engine.plugins.trust_levels import TrustLevel


class TestMetadataEndpointBlocking:
    def test_aws_metadata_blocked(self) -> None:
        assert "169.254.169.254" in _METADATA_ENDPOINTS

    def test_gcp_metadata_blocked(self) -> None:
        assert "metadata.google.internal" in _METADATA_ENDPOINTS

    def test_azure_metadata_blocked(self) -> None:
        assert "metadata.azure.com" in _METADATA_ENDPOINTS

    def test_metadata_blocked_by_default(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=[])
        guard = NetworkGuard(policy)
        assert guard._is_metadata_endpoint("169.254.169.254") is True
        assert guard._is_metadata_endpoint("metadata.google.internal") is True
        assert guard._is_metadata_endpoint("metadata.azure.com") is True

    def test_metadata_host_blocked_even_with_whitelist(self) -> None:
        policy = NetworkPolicy(
            allowed_endpoints=["api.example.com"],
            block_metadata_endpoints=True,
        )
        guard = NetworkGuard(policy, plugin_id="test")
        assert guard._is_host_allowed("169.254.169.254") is False
        assert guard._is_host_allowed("metadata.google.internal") is False

    def test_normal_host_not_flagged_as_metadata(self) -> None:
        guard = NetworkGuard(NetworkPolicy())
        assert guard._is_metadata_endpoint("api.example.com") is False

    def test_metadata_blocked_in_context(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "meta_test")
        ctx = SandboxContext(policy)
        assert ctx.policy.network_policy.block_metadata_endpoints is True
        ctx.cleanup()


class TestExtendedPathBlocking:
    def test_all_critical_prefixes_blocked(self) -> None:
        expected = {"/proc", "/sys", "/dev", "/etc", "/var", "/root", "/run", "/boot"}
        for prefix in expected:
            assert prefix in _BLOCKED_SYSTEM_PREFIXES, f"{prefix} not in blocked prefixes"

    def test_etc_passwd_blocked(self) -> None:
        policy = FilesystemPolicy(block_symlinks=True)
        fs = FilesystemIsolation(policy=policy, plugin_id="test")
        with pytest.raises(PermissionError, match="system_path"):
            fs._validate_path("/etc/passwd")

    def test_etc_shadow_blocked(self) -> None:
        policy = FilesystemPolicy(block_symlinks=True)
        fs = FilesystemIsolation(policy=policy, plugin_id="test")
        with pytest.raises(PermissionError, match="system_path"):
            fs._validate_path("/etc/shadow")

    def test_var_log_blocked(self) -> None:
        policy = FilesystemPolicy(block_symlinks=True)
        fs = FilesystemIsolation(policy=policy, plugin_id="test")
        with pytest.raises(PermissionError, match="system_path"):
            fs._validate_path("/var/log/syslog")

    def test_root_home_blocked(self) -> None:
        policy = FilesystemPolicy(block_symlinks=True)
        fs = FilesystemIsolation(policy=policy, plugin_id="test")
        with pytest.raises(PermissionError, match="system_path"):
            fs._validate_path("/root/.bashrc")

    def test_run_blocked(self) -> None:
        policy = FilesystemPolicy(block_symlinks=True)
        fs = FilesystemIsolation(policy=policy, plugin_id="test")
        with pytest.raises(PermissionError, match="system_path"):
            fs._validate_path("/run/docker.sock")

    def test_boot_blocked(self) -> None:
        policy = FilesystemPolicy(block_symlinks=True)
        fs = FilesystemIsolation(policy=policy, plugin_id="test")
        with pytest.raises(PermissionError, match="system_path"):
            fs._validate_path("/boot/vmlinuz")

    def test_violation_logged_on_blocked_path(self) -> None:
        policy = FilesystemPolicy(block_symlinks=True)
        fs = FilesystemIsolation(policy=policy, plugin_id="test_plugin")
        with pytest.raises(PermissionError):
            fs._validate_path("/etc/hosts")
        violations = fs.get_violations()
        assert len(violations) == 1
        assert violations[0].plugin_id == "test_plugin"


class TestIntrospectionHardening:
    def test_builtins_attr_blocked(self) -> None:
        assert "__builtins__" in _EXPLICITLY_BLOCKED_ATTRS

    def test_func_attr_blocked(self) -> None:
        assert "__func__" in _EXPLICITLY_BLOCKED_ATTRS

    def test_self_attr_blocked(self) -> None:
        assert "__self__" in _EXPLICITLY_BLOCKED_ATTRS

    def test_module_attr_blocked(self) -> None:
        assert "__module__" in _EXPLICITLY_BLOCKED_ATTRS

    def test_weakref_attr_blocked(self) -> None:
        assert "__weakref__" in _EXPLICITLY_BLOCKED_ATTRS

    def test_builtins_access_blocked_by_guard(self) -> None:
        policy = IntrospectionPolicy()
        guard = IntrospectionGuard(policy, plugin_id="test")
        guard.install()
        try:
            with pytest.raises(PermissionError, match="not accessible"):
                builtins.getattr(object(), "__builtins__")  # noqa: B009
        finally:
            guard.uninstall()

    def test_import_not_in_default_blocked_builtins(self) -> None:
        policy = IntrospectionPolicy()
        assert "__import__" not in policy.blocked_builtins


class TestContextTrustEnforcement:
    def test_context_enforces_hard_limits_on_activate(self) -> None:
        collector = SandboxMetricsCollector()
        logger = SecurityEventLogger(plugin_id="hard_test")
        policy = SandboxPolicy(
            plugin_id="hard_test",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules={f"m{i}" for i in range(15)}),
            resource_policy=ResourcePolicy(max_cpu_seconds=30, max_memory_bytes=2 * 1024**3),
        )
        ctx = SandboxContext(policy, metrics_collector=collector)
        ctx._event_logger = logger
        with pytest.raises(SandboxViolation, match="Hard limit violations"):
            ctx.activate()
        events = logger.get_events()
        hard_limit_events = [
            e for e in events
            if "Hard limit" in e.detail
        ]
        assert len(hard_limit_events) >= 1
        ctx.cleanup()

    def test_context_validates_integrity(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "integrity_test")
        policy.set_integrity_hash()
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True
        ctx.cleanup()

    def test_context_detects_tampered_integrity(self) -> None:
        # from_trust_level() auto-sets _integrity_hash. We replace
        # introspection_policy with a fresh copy and re-set the hash.
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "tamper_test")

        # Guard: replace the introspection_policy reference so mutations
        # don't leak into the shared _TRUST_INTROSPECTION_PRESETS dict.
        policy.introspection_policy = IntrospectionPolicy()
        policy.set_integrity_hash()
        assert policy.verify_integrity() is True

        # Tamper via blocked_builtins (introspection_policy sub-object).
        # Choice rationale: blocked_builtins IS serialized in
        # compute_integrity_hash() (policy.py:209) but is NOT
        # independently checked by the threshold logic in
        # validate_trust_level() (context.py:88-99). Being in a
        # different sub-object from resource_policy makes it less likely
        # to be independently validated. Detection is therefore purely
        # hash-based, proving the integrity mechanism works.
        policy.introspection_policy.blocked_builtins.discard(
            next(iter(policy.introspection_policy.blocked_builtins))
        )
        assert policy.verify_integrity() is False

        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False
        ctx.cleanup()

    def test_context_enforces_untrusted_no_threads(self) -> None:
        policy = SandboxPolicy(
            plugin_id="thread_test",
            trust_level="untrusted",
            resource_policy=ResourcePolicy(max_threads=4),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False
        ctx.cleanup()

    def test_activate_rejects_tampered_integrity(self) -> None:
        # from_trust_level() auto-sets _integrity_hash. We replace
        # introspection_policy with a fresh copy and re-set the hash.
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "tamper_activate")

        # Guard: replace the introspection_policy reference so mutations
        # don't leak into the shared _TRUST_INTROSPECTION_PRESETS dict.
        policy.introspection_policy = IntrospectionPolicy()
        policy.set_integrity_hash()
        assert policy.verify_integrity() is True

        # Tamper via blocked_builtins (introspection_policy sub-object).
        # Choice rationale: blocked_builtins IS serialized in
        # compute_integrity_hash() (policy.py:209) but is NOT subject
        # to validate_trust_level() resource thresholds (context.py:88-99).
        # Being in a different sub-object from resource_policy makes it
        # less likely to be independently validated. SandboxViolation is
        # raised because verify_integrity() detects hash mismatch.
        policy.introspection_policy.blocked_builtins.discard(
            next(iter(policy.introspection_policy.blocked_builtins))
        )
        assert policy.verify_integrity() is False

        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation, match="Trust level policy validation failed"):
            ctx.activate()
        assert ctx.is_active is False
        ctx.cleanup()

    def test_activate_rejects_insufficient_blocked_modules(self) -> None:
        policy = SandboxPolicy(
            plugin_id="weak_imports",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules={"os"}),
            resource_policy=ResourcePolicy(max_cpu_seconds=10),
        )
        policy.set_integrity_hash()
        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation, match="Trust level policy validation failed"):
            ctx.activate()
        assert ctx.is_active is False
        ctx.cleanup()

    def test_context_environment_policy_set(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "env_test")
        ctx = SandboxContext(policy)
        assert ctx.policy.environment_policy.block_os_environ is True
        assert len(ctx.policy.environment_policy.allowed_env_vars) == 0
        ctx.cleanup()

    def test_context_trusted_full_environment(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_FULL, "env_trusted")
        ctx = SandboxContext(policy)
        assert ctx.policy.environment_policy.block_os_environ is False
        assert "PATH" in ctx.policy.environment_policy.allowed_env_vars
        ctx.cleanup()

    def test_clean_hash_passes_validation(self) -> None:
        # Positive control: policy with hash set but NOT tampered
        # should pass both verify_integrity() and validate_trust_level().
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "clean_hash")
        policy.set_integrity_hash()
        assert policy.verify_integrity() is True
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True
        ctx.cleanup()

    def test_tamper_undetected_without_set_integrity_hash(self) -> None:
        # Negative control: clearing _integrity_hash makes verify_integrity()
        # return True (policy.py:219-220). Mutations are therefore invisible
        # even to fields that ARE serialized in the hash.
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "no_hash_tamper")
        policy.introspection_policy = IntrospectionPolicy()
        policy._integrity_hash = None

        # Heavy tampering that WOULD be detected if hash were set
        policy.introspection_policy.blocked_builtins = set()
        assert policy.verify_integrity() is True

        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True
        ctx.cleanup()

    def test_blocked_dunder_access_tamper_not_detected_by_hash(self) -> None:
        # blocked_dunder_access is a bool on introspection_policy but is
        # NOT serialized in compute_integrity_hash() (policy.py:195-212).
        # Toggling it does NOT change the hash, proving a coverage gap.
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "dunder_gap")
        policy.set_integrity_hash()
        original_hash = policy._integrity_hash

        policy.introspection_policy.blocked_dunder_access = not policy.introspection_policy.blocked_dunder_access
        assert policy.compute_integrity_hash() == original_hash
        assert policy.verify_integrity() is True

    def test_blocked_builtins_add_element_also_detected(self) -> None:
        # Complementary mutation: ADD an element to blocked_builtins
        # (vs. the discard used in test_context_detects_tampered_integrity).
        # Both directions should be detected by the hash.
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "add_builtin")
        policy.introspection_policy = IntrospectionPolicy()
        policy.set_integrity_hash()
        assert policy.verify_integrity() is True

        policy.introspection_policy.blocked_builtins.add("__suspicious__")
        assert policy.verify_integrity() is False

        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False
        ctx.cleanup()

    def test_from_manifest_sets_hash_automatically(self) -> None:
        # from_manifest() calls set_integrity_hash() (policy.py:309),
        # consistent with from_trust_level() and trusted_policy().
        class FakeManifest:
            id = "manifest_test"

            def requires_network(self) -> bool:
                return False

        policy = SandboxPolicy.from_manifest(FakeManifest())
        assert policy._integrity_hash is not None
        assert policy.verify_integrity() is True

        policy.introspection_policy.blocked_builtins.discard(
            next(iter(policy.introspection_policy.blocked_builtins))
        )
        assert policy.verify_integrity() is False

    def test_trusted_policy_sets_hash_automatically(self) -> None:
        # trusted_policy() also calls set_integrity_hash() (policy.py:363).
        policy = SandboxPolicy.trusted_policy("auto_hash")
        assert policy._integrity_hash is not None
        assert policy.verify_integrity() is True

    def test_tampered_hash_prevents_context_activation(self) -> None:
        # End-to-end: tampered policy cannot be activated even if
        # threshold checks (blocked_modules count, cpu, threads, paths)
        # would individually pass.
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "e2e_tamper")
        policy.introspection_policy = IntrospectionPolicy()
        policy.set_integrity_hash()

        # Threshold checks would pass before tampering
        assert policy.verify_integrity() is True

        # Minimal tamper that doesn't affect threshold logic
        policy.introspection_policy.blocked_builtins.discard("eval")
        assert policy.verify_integrity() is False

        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation):
            ctx.activate()
        assert ctx.is_active is False
        ctx.cleanup()


class TestPolicyIntegrityHash:
    def test_hash_changes_on_import_change(self) -> None:
        policy = SandboxPolicy(
            plugin_id="hash_test",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules={"os", "sys", "subprocess"}),
        )
        h1 = policy.compute_integrity_hash()
        policy.import_policy.blocked_modules = set()
        h2 = policy.compute_integrity_hash()
        assert h1 != h2

    def test_hash_changes_on_cpu_change(self) -> None:
        policy = SandboxPolicy(plugin_id="hash_test", trust_level="untrusted")
        h1 = policy.compute_integrity_hash()
        policy.resource_policy.max_cpu_seconds = 999
        h2 = policy.compute_integrity_hash()
        assert h1 != h2

    def test_hash_changes_on_endpoint_change(self) -> None:
        policy = SandboxPolicy(plugin_id="hash_test", trust_level="untrusted")
        h1 = policy.compute_integrity_hash()
        policy.network_policy.allowed_endpoints = ["evil.com"]
        h2 = policy.compute_integrity_hash()
        assert h1 != h2

    def test_hash_unchanged_for_same_config(self) -> None:
        policy1 = SandboxPolicy(
            plugin_id="same",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules={"os", "sys"}),
            resource_policy=ResourcePolicy(max_cpu_seconds=30),
        )
        policy2 = SandboxPolicy(
            plugin_id="same",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules={"os", "sys"}),
            resource_policy=ResourcePolicy(max_cpu_seconds=30),
        )
        assert policy1.compute_integrity_hash() == policy2.compute_integrity_hash()


class TestIntegrityHashTamperDetectionIsolation:
    """Tests proving integrity-hash-based tamper detection in isolation layer.

    These tests document why blocked_builtins is the chosen tampering field:
      - IS included in compute_integrity_hash() at policy.py:209
      - NOT subject to validate_trust_level() resource checks at context.py:88-94
      - In introspection_policy (different sub-object from resource_policy),
        less likely to be independently validated
      - Tamper detection is therefore purely via hash mismatch

    NOTE: from_trust_level() passes the shared _TRUST_INTROSPECTION_PRESETS
    by reference, so we replace introspection_policy with a fresh
    IntrospectionPolicy() before any mutation to avoid cross-test contamination.
    """

    def test_blocked_builtins_in_hash_so_tamper_detected(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "builtin_hash")
        policy.introspection_policy = IntrospectionPolicy()
        policy.set_integrity_hash()
        original_hash = policy._integrity_hash
        policy.introspection_policy.blocked_builtins = set()
        assert policy._integrity_hash == original_hash
        assert policy.verify_integrity() is False

    def test_blocked_dunder_access_not_in_hash_so_tamper_undetected(self) -> None:
        # blocked_dunder_access is NOT serialized into the integrity hash
        # (policy.py:195-212). Mutating it does not change the hash.
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "dunder_no_hash")
        policy.set_integrity_hash()
        policy.introspection_policy.blocked_dunder_access = False
        assert policy.verify_integrity() is True

    def test_no_hash_set_means_tamper_undetected(self) -> None:
        # Without an integrity hash, verify_integrity() always returns
        # True (policy.py:219-220). Clearing the auto-set hash proves this.
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "no_hash_set")
        policy.introspection_policy = IntrospectionPolicy()
        policy._integrity_hash = None
        policy.introspection_policy.blocked_builtins = set()
        assert policy.verify_integrity() is True
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True
        ctx.cleanup()

    def test_introspection_fields_also_detected_by_hash(self) -> None:
        # blocked_builtins IS in the hash (policy.py:209) but NOT in
        # validate_trust_level resource checks. With hash set, tampering
        # IS detected purely via hash mismatch. blocked_builtins chosen
        # because it is in introspection_policy (a different sub-object
        # from resource_policy), less likely to be independently validated.
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "builtin_hash")
        policy.introspection_policy = IntrospectionPolicy()
        policy.set_integrity_hash()
        policy.introspection_policy.blocked_builtins = set()
        assert policy.verify_integrity() is False

    def test_hash_included_fields_all_detected(self) -> None:
        # Verify ALL fields included in compute_integrity_hash() are
        # detected when mutated. This is a coverage sweep of every field
        # serialized in policy.py:195-212.
        fields_and_mutations = [
            ("plugin_id", lambda p: setattr(p, "plugin_id", "tampered")),
            ("trust_level", lambda p: setattr(p, "trust_level", "trusted_full")),
            ("blocked_modules", lambda p: p.import_policy.blocked_modules.add("NEW_MODULE")),
            ("max_cpu_seconds", lambda p: setattr(p.resource_policy, "max_cpu_seconds", 999)),
            ("max_memory_bytes", lambda p: setattr(p.resource_policy, "max_memory_bytes", 999)),
            ("max_file_descriptors", lambda p: setattr(p.resource_policy, "max_file_descriptors", 999)),
            ("max_threads", lambda p: setattr(p.resource_policy, "max_threads", 999)),
            ("wall_time_seconds", lambda p: setattr(p.resource_policy, "wall_time_seconds", 999)),
            ("allowed_endpoints", lambda p: p.network_policy.allowed_endpoints.append("evil.com")),
            ("read_only_paths", lambda p: p.filesystem_policy.read_only_paths.append("/evil")),
            ("read_write_paths", lambda p: p.filesystem_policy.read_write_paths.append("/evil")),
            ("blocked_builtins", lambda p: p.introspection_policy.blocked_builtins.add("NEW_BUILTIN")),
        ]
        for field_name, mutate in fields_and_mutations:
            policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, f"field_{field_name}")
            policy.introspection_policy = IntrospectionPolicy()
            policy.set_integrity_hash()
            assert policy.verify_integrity() is True, f"{field_name}: hash valid before tamper"
            mutate(policy)
            assert policy.verify_integrity() is False, (
                f"{field_name}: hash tamper not detected (field IS in hash)"
            )
