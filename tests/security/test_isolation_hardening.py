"""Tests for hardened isolation: metadata endpoint blocking, extended path blocking, and integrity."""

from __future__ import annotations

import builtins
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

from engine.plugins.sandbox.core.context import SandboxContext
from engine.plugins.sandbox.core.policy import (
    EnvironmentPolicy,
    FilesystemPolicy,
    ImportPolicy,
    IntrospectionPolicy,
    NetworkPolicy,
    ResourcePolicy,
    SandboxPolicy,
)
from engine.plugins.sandbox.core.violation import SandboxViolationCategory
from engine.plugins.sandbox.layers.filesystem_isolation import (
    FilesystemIsolation,
    _BLOCKED_SYSTEM_PREFIXES,
)
from engine.plugins.sandbox.layers.introspection_guard import (
    _EXPLICITLY_BLOCKED_ATTRS,
    IntrospectionGuard,
)
from engine.plugins.sandbox.layers.network_guard import (
    NetworkGuard,
    _METADATA_ENDPOINTS,
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
                builtins.getattr(object(), "__builtins__")
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
            resource_policy=ResourcePolicy(max_cpu_seconds=9999),
        )
        ctx = SandboxContext(policy, metrics_collector=collector)
        ctx._event_logger = logger
        ctx.activate()
        try:
            events = logger.get_events()
            hard_limit_events = [
                e for e in events
                if "Hard limit" in e.detail
            ]
            assert len(hard_limit_events) >= 1
        finally:
            ctx.cleanup()

    def test_context_validates_integrity(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "integrity_test")
        policy.set_integrity_hash()
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True
        ctx.cleanup()

    def test_context_detects_tampered_integrity(self) -> None:
        policy = SandboxPolicy(plugin_id="tamper_test", trust_level="untrusted")
        policy.set_integrity_hash()
        policy.resource_policy.max_cpu_seconds = 9999
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
