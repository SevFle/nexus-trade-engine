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
        # from_trust_level() auto-calls set_integrity_hash() internally (policy.py:347),
        # but we call it explicitly here to make the intent clear and to establish a
        # known-good baseline before tampering.
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "tamper_test")
        policy.set_integrity_hash()

        # Mutate a field that IS included in compute_integrity_hash() (blocked_modules
        # is sorted into the hash at policy.py:199). This must cause verify_integrity()
        # to return False, which in turn makes validate_trust_level() return False.
        original_blocked = set(policy.import_policy.blocked_modules)
        policy.import_policy.blocked_modules = set()

        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False

        # Restore to prove the original policy would have passed.
        policy.import_policy.blocked_modules = original_blocked
        assert ctx.validate_trust_level() is True
        ctx.cleanup()

    def test_context_detects_tampered_introspection_blocked_builtins(self) -> None:
        # SECURITY GAP DOCUMENTATION: introspection_policy fields (blocked_builtins,
        # blocked_attributes, blocked_dunder_access, etc.) are NOT included in
        # compute_integrity_hash(). Mutating them after set_integrity_hash() will NOT
        # be caught by verify_integrity(). This test demonstrates that gap so that
        # a future fix can turn this into a proper detection test.
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "introspect_tamper")
        policy.set_integrity_hash()

        # Flip blocked_builtins — a security-critical introspection field.
        # Note: "block_dunder_builtins" referenced in requirements maps to
        # IntrospectionPolicy.blocked_builtins (the set of blocked builtin names).
        policy.introspection_policy.blocked_builtins = set()

        # verify_integrity() still returns True because introspection fields
        # are not part of the hash. This is a known limitation.
        assert policy.verify_integrity() is True

        # validate_trust_level() returns True despite the introspection tamper
        # because the integrity hash still matches.
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True
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
        # from_trust_level() auto-calls set_integrity_hash() (policy.py:347).
        # We call it again explicitly to document that the hash is the baseline
        # against which any subsequent mutation should be detected.
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "tamper_activate")
        policy.set_integrity_hash()

        # Mutate blocked_modules (a hashed field) so verify_integrity() returns False,
        # which causes activate() to raise SandboxViolation.
        policy.import_policy.blocked_modules = set()

        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation, match="Trust level policy validation failed"):
            ctx.activate()
        assert ctx.is_active is False
        ctx.cleanup()

    def test_activate_rejects_tampered_integrity_resource_mutation(self) -> None:
        # Additional variant: mutate max_memory_bytes (also in the hash).
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "mem_tamper")
        policy.set_integrity_hash()
        policy.resource_policy.max_memory_bytes = 99 * 1024**3

        ctx = SandboxContext(policy)
        with pytest.raises(SandboxViolation, match="Trust level policy validation failed"):
            ctx.activate()
        assert ctx.is_active is False
        ctx.cleanup()

    def test_activate_rejects_tampered_integrity_network_mutation(self) -> None:
        # Mutate allowed_endpoints (included in the integrity hash).
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "net_tamper")
        policy.set_integrity_hash()
        policy.network_policy.allowed_endpoints = ["evil.internal"]

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

    def test_from_trust_level_auto_sets_integrity_hash(self) -> None:
        # from_trust_level() calls set_integrity_hash() at policy.py:347,
        # so the internal _integrity_hash is already set after construction.
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "auto_hash")
        assert policy._integrity_hash is not None
        assert policy.verify_integrity() is True

    def test_explicit_set_integrity_hash_overwrites_auto(self) -> None:
        # Calling set_integrity_hash() after from_trust_level() is idempotent
        # if no mutations have occurred — same hash value is recomputed.
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "idempotent")
        auto_hash = policy._integrity_hash
        policy.set_integrity_hash()
        assert policy._integrity_hash == auto_hash

    def test_hash_changes_on_memory_change(self) -> None:
        policy = SandboxPolicy(plugin_id="hash_test", trust_level="untrusted")
        h1 = policy.compute_integrity_hash()
        policy.resource_policy.max_memory_bytes = 999
        h2 = policy.compute_integrity_hash()
        assert h1 != h2

    def test_hash_changes_on_fd_change(self) -> None:
        policy = SandboxPolicy(plugin_id="hash_test", trust_level="untrusted")
        h1 = policy.compute_integrity_hash()
        policy.resource_policy.max_file_descriptors = 9999
        h2 = policy.compute_integrity_hash()
        assert h1 != h2

    def test_hash_changes_on_thread_change(self) -> None:
        policy = SandboxPolicy(plugin_id="hash_test", trust_level="untrusted")
        h1 = policy.compute_integrity_hash()
        policy.resource_policy.max_threads = 16
        h2 = policy.compute_integrity_hash()
        assert h1 != h2

    def test_hash_changes_on_wall_time_change(self) -> None:
        policy = SandboxPolicy(plugin_id="hash_test", trust_level="untrusted")
        h1 = policy.compute_integrity_hash()
        policy.resource_policy.wall_time_seconds = 9999
        h2 = policy.compute_integrity_hash()
        assert h1 != h2

    def test_hash_changes_on_rw_paths_change(self) -> None:
        policy = SandboxPolicy(plugin_id="hash_test", trust_level="untrusted")
        h1 = policy.compute_integrity_hash()
        policy.filesystem_policy.read_write_paths = ["/tmp/evil"]  # noqa: S108
        h2 = policy.compute_integrity_hash()
        assert h1 != h2

    def test_hash_changes_on_ro_paths_change(self) -> None:
        policy = SandboxPolicy(plugin_id="hash_test", trust_level="untrusted")
        h1 = policy.compute_integrity_hash()
        policy.filesystem_policy.read_only_paths = ["/etc/passwd"]
        h2 = policy.compute_integrity_hash()
        assert h1 != h2

    def test_introspection_fields_not_in_hash(self) -> None:
        # SECURITY GAP: IntrospectionPolicy fields are not included in the
        # integrity hash. Mutating blocked_builtins, blocked_attributes,
        # blocked_dunder_access, etc. does not change the hash.
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "gap_test")
        h_before = policy.compute_integrity_hash()
        policy.introspection_policy.blocked_builtins = set()
        policy.introspection_policy.blocked_attributes = set()
        policy.introspection_policy.blocked_dunder_access = False
        h_after = policy.compute_integrity_hash()
        assert h_before == h_after

    def test_environment_fields_not_in_hash(self) -> None:
        # EnvironmentPolicy fields are also not in the integrity hash.
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "env_gap")
        h_before = policy.compute_integrity_hash()
        policy.environment_policy.allowed_env_vars = {"SECRET_KEY"}
        policy.environment_policy.block_os_environ = False
        h_after = policy.compute_integrity_hash()
        assert h_before == h_after

    def test_verify_integrity_none_hash_returns_true(self) -> None:
        # When _integrity_hash is None, verify_integrity() returns True
        # (no baseline to compare against). This means a policy with no
        # integrity hash set will always "pass" verification.
        policy = SandboxPolicy(plugin_id="no_hash", trust_level="untrusted")
        assert policy._integrity_hash is None
        assert policy.verify_integrity() is True

    def test_tampered_plugin_id_detected(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "original_id")
        policy.set_integrity_hash()
        policy.plugin_id = "tampered_id"
        assert policy.verify_integrity() is False

    def test_tampered_trust_level_detected(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "trust_tamper")
        policy.set_integrity_hash()
        policy.trust_level = "trusted_full"
        assert policy.verify_integrity() is False
