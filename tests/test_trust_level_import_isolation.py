"""Comprehensive tests for TrustLevel lazy-import isolation and sandbox core modules.

Covers:
  1. engine.plugins.trust_levels — TrustLevel enum, get_trust_level, get_trust_policy
  2. engine.plugins.sandbox.core.policy — all dataclasses, from_trust_level, from_manifest,
     trusted_policy, _parse_memory, lazy import cache
  3. engine.plugins.sandbox.core.context — SandboxContext, SecurityEventLogger,
     trust_level property, validate_trust_level, lazy import cache
  4. Import isolation — trust_levels.py has zero sandbox.core imports,
     lazy caching returns same class, no circular import errors
"""

from __future__ import annotations

import ast
import inspect
import os
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

from engine.plugins.trust_levels import (
    TrustLevel,
    get_trust_level,
    get_trust_policy,
)

# ═══════════════════════════════════════════════════════════════════════════
# 1. TrustLevel enum tests
# ═══════════════════════════════════════════════════════════════════════════


class TestTrustLevelEnum:
    def test_enum_values(self) -> None:
        assert TrustLevel.TRUSTED_FULL.value == "trusted_full"
        assert TrustLevel.TRUSTED_LIMITED.value == "trusted_limited"
        assert TrustLevel.UNTRUSTED.value == "untrusted"

    def test_enum_from_string(self) -> None:
        assert TrustLevel("trusted_full") is TrustLevel.TRUSTED_FULL
        assert TrustLevel("trusted_limited") is TrustLevel.TRUSTED_LIMITED
        assert TrustLevel("untrusted") is TrustLevel.UNTRUSTED

    def test_enum_invalid_raises(self) -> None:
        with pytest.raises(ValueError):
            TrustLevel("nonexistent")

    def test_enum_members_count(self) -> None:
        assert len(TrustLevel) == 3

    def test_enum_is_hashable(self) -> None:
        mapping = {TrustLevel.TRUSTED_FULL: "full", TrustLevel.UNTRUSTED: "none"}
        assert mapping[TrustLevel.TRUSTED_FULL] == "full"


class TestGetTrustLevel:
    def test_valid_trusted_full(self) -> None:
        manifest = MagicMock(trust_level="trusted_full")
        assert get_trust_level(manifest) is TrustLevel.TRUSTED_FULL

    def test_valid_trusted_limited(self) -> None:
        manifest = MagicMock(trust_level="trusted_limited")
        assert get_trust_level(manifest) is TrustLevel.TRUSTED_LIMITED

    def test_valid_untrusted(self) -> None:
        manifest = MagicMock(trust_level="untrusted")
        assert get_trust_level(manifest) is TrustLevel.UNTRUSTED

    def test_invalid_string_falls_back(self) -> None:
        manifest = MagicMock(trust_level="garbage")
        assert get_trust_level(manifest) is TrustLevel.UNTRUSTED

    def test_none_attribute_falls_back(self) -> None:
        manifest = MagicMock(spec=[])
        assert get_trust_level(manifest) is TrustLevel.UNTRUSTED

    def test_no_trust_level_attribute(self) -> None:
        manifest = object()
        assert get_trust_level(manifest) is TrustLevel.UNTRUSTED


class TestGetTrustPolicy:
    def test_trusted_full_policy(self) -> None:
        policy = get_trust_policy(TrustLevel.TRUSTED_FULL)
        assert policy["import_restriction"] == "relaxed"
        assert policy["resource_multiplier"] == 4.0
        assert policy["filesystem"] == "workspace"
        assert policy["introspection"] == "basic"

    def test_trusted_limited_policy(self) -> None:
        policy = get_trust_policy(TrustLevel.TRUSTED_LIMITED)
        assert policy["import_restriction"] == "standard"
        assert policy["resource_multiplier"] == 2.0
        assert policy["filesystem"] == "isolated_rw"

    def test_untrusted_policy(self) -> None:
        policy = get_trust_policy(TrustLevel.UNTRUSTED)
        assert policy["import_restriction"] == "strict"
        assert policy["resource_multiplier"] == 1.0
        assert policy["filesystem"] == "isolated_ro"
        assert policy["introspection"] == "strict"

    def test_unknown_falls_back_to_untrusted(self) -> None:
        policy = get_trust_policy("nonexistent")  # type: ignore[arg-type]
        assert policy is get_trust_policy(TrustLevel.UNTRUSTED)

    def test_policy_keys_consistent(self) -> None:
        expected_keys = {
            "import_restriction",
            "network",
            "resource_multiplier",
            "filesystem",
            "introspection",
        }
        for level in TrustLevel:
            assert set(get_trust_policy(level).keys()) == expected_keys


# ═══════════════════════════════════════════════════════════════════════════
# 2. Policy dataclass tests
# ═══════════════════════════════════════════════════════════════════════════


class TestImportPolicy:
    def test_defaults_empty(self) -> None:
        from engine.plugins.sandbox.core.policy import ImportPolicy

        p = ImportPolicy()
        assert p.allowed_modules == set()
        assert p.blocked_modules == set()
        assert p.blocked_categories == {}

    def test_is_allowed_no_restrictions(self) -> None:
        from engine.plugins.sandbox.core.policy import ImportPolicy

        p = ImportPolicy()
        assert p.is_allowed("os") is True
        assert p.is_allowed("json") is True

    def test_is_allowed_blocked(self) -> None:
        from engine.plugins.sandbox.core.policy import ImportPolicy

        p = ImportPolicy(blocked_modules={"os", "subprocess"})
        assert p.is_allowed("os") is False
        assert p.is_allowed("subprocess") is False
        assert p.is_allowed("json") is True

    def test_is_allowed_with_allowlist(self) -> None:
        from engine.plugins.sandbox.core.policy import ImportPolicy

        p = ImportPolicy(allowed_modules={"json", "math"})
        assert p.is_allowed("json") is True
        assert p.is_allowed("math") is True
        assert p.is_allowed("os") is False

    def test_is_allowed_empty_allowlist_blocks_nothing(self) -> None:
        from engine.plugins.sandbox.core.policy import ImportPolicy

        p = ImportPolicy(allowed_modules=set())
        assert p.is_allowed("anything") is True

    def test_is_allowed_submodule_check(self) -> None:
        from engine.plugins.sandbox.core.policy import ImportPolicy

        p = ImportPolicy(blocked_modules={"os"})
        assert p.is_allowed("os.path") is False

    def test_is_allowed_blocked_takes_precedence(self) -> None:
        from engine.plugins.sandbox.core.policy import ImportPolicy

        p = ImportPolicy(
            allowed_modules={"os"},
            blocked_modules={"os"},
        )
        assert p.is_allowed("os") is False


class TestNetworkPolicy:
    def test_defaults(self) -> None:
        from engine.plugins.sandbox.core.policy import NetworkPolicy

        p = NetworkPolicy()
        assert p.allowed_endpoints == []
        assert p.allowed_cidrs == []
        assert p.allowed_ports == set()
        assert p.block_dns is True

    def test_is_host_allowed_empty_endpoints(self) -> None:
        from engine.plugins.sandbox.core.policy import NetworkPolicy

        p = NetworkPolicy()
        assert p.is_host_allowed("example.com") is False

    def test_is_host_allowed_exact_match(self) -> None:
        from engine.plugins.sandbox.core.policy import NetworkPolicy

        p = NetworkPolicy(allowed_endpoints=["api.example.com"])
        assert p.is_host_allowed("api.example.com") is True

    def test_is_host_allowed_subdomain(self) -> None:
        from engine.plugins.sandbox.core.policy import NetworkPolicy

        p = NetworkPolicy(allowed_endpoints=["example.com"])
        assert p.is_host_allowed("api.example.com") is True

    def test_is_host_allowed_no_partial_match(self) -> None:
        from engine.plugins.sandbox.core.policy import NetworkPolicy

        p = NetworkPolicy(allowed_endpoints=["example.com"])
        assert p.is_host_allowed("notexample.com") is False

    def test_is_host_allowed_different_suffix(self) -> None:
        from engine.plugins.sandbox.core.policy import NetworkPolicy

        p = NetworkPolicy(allowed_endpoints=["example.com"])
        assert p.is_host_allowed("evil-example.com") is False


class TestResourcePolicy:
    def test_defaults(self) -> None:
        from engine.plugins.sandbox.core.policy import ResourcePolicy

        p = ResourcePolicy()
        assert p.max_cpu_seconds == 30.0
        assert p.max_memory_bytes == 512 * 1024 * 1024
        assert p.max_file_descriptors == 64
        assert p.max_threads == 1
        assert p.wall_time_seconds == 60.0


class TestFilesystemPolicy:
    def test_defaults(self) -> None:
        from engine.plugins.sandbox.core.policy import FilesystemPolicy

        p = FilesystemPolicy()
        assert p.read_only_paths == []
        assert p.read_write_paths == []
        assert p.virtual_root is None
        assert p.block_symlinks is True
        assert p.block_absolute_paths is True


class TestIntrospectionPolicy:
    def test_defaults(self) -> None:
        from engine.plugins.sandbox.core.policy import IntrospectionPolicy

        p = IntrospectionPolicy()
        assert "eval" in p.blocked_builtins
        assert "exec" in p.blocked_builtins
        assert "__import__" in p.blocked_builtins
        assert "__subclasses__" in p.blocked_attributes
        assert "__globals__" in p.blocked_attributes
        assert p.blocked_dunder_access is True
        assert p.block_gc is True
        assert p.block_inspect is True
        assert p.block_frame_access is True

    def test_defaults_independent_per_instance(self) -> None:
        from engine.plugins.sandbox.core.policy import IntrospectionPolicy

        p1 = IntrospectionPolicy()
        p2 = IntrospectionPolicy()
        p1.blocked_builtins.add("custom_blocked")
        assert "custom_blocked" not in p2.blocked_builtins


class TestSandboxPolicyDefaults:
    def test_defaults(self) -> None:
        from engine.plugins.sandbox.core.policy import SandboxPolicy

        p = SandboxPolicy()
        assert p.plugin_id == "unknown"
        assert p.trust_level == "untrusted"
        assert isinstance(p.import_policy, type(p.import_policy))
        assert isinstance(p.network_policy, type(p.network_policy))


# ═══════════════════════════════════════════════════════════════════════════
# 3. SandboxPolicy.from_trust_level tests
# ═══════════════════════════════════════════════════════════════════════════


class TestSandboxPolicyFromTrustLevel:
    def test_from_string_trusted_full(self) -> None:
        from engine.plugins.sandbox.core.policy import SandboxPolicy

        p = SandboxPolicy.from_trust_level("trusted_full", plugin_id="p1")
        assert p.plugin_id == "p1"
        assert p.trust_level == "trusted"
        assert "subprocess" in p.import_policy.blocked_modules
        assert "ctypes" in p.import_policy.blocked_modules
        assert p.resource_policy.max_cpu_seconds == 300
        assert p.resource_policy.max_memory_bytes == 2 * 1024**3

    def test_from_string_trusted_limited(self) -> None:
        from engine.plugins.sandbox.core.policy import SandboxPolicy

        p = SandboxPolicy.from_trust_level("trusted_limited", plugin_id="p2")
        assert p.trust_level == "trusted_limited"
        assert "os" in p.import_policy.blocked_modules
        assert "subprocess" in p.import_policy.blocked_modules
        assert p.resource_policy.max_cpu_seconds == 120
        assert p.resource_policy.max_memory_bytes == 1024**3

    def test_from_string_untrusted(self) -> None:
        from engine.plugins.sandbox.core.policy import SandboxPolicy

        p = SandboxPolicy.from_trust_level("untrusted", plugin_id="p3")
        assert p.trust_level == "untrusted"
        assert "os" in p.import_policy.blocked_modules
        assert "socket" in p.import_policy.blocked_modules
        assert "gc" in p.import_policy.blocked_modules

    def test_from_enum_trusted_full(self) -> None:
        from engine.plugins.sandbox.core.policy import SandboxPolicy

        p = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_FULL)
        assert p.trust_level == "trusted"

    def test_from_enum_trusted_limited(self) -> None:
        from engine.plugins.sandbox.core.policy import SandboxPolicy

        p = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_LIMITED)
        assert p.trust_level == "trusted_limited"

    def test_from_enum_untrusted(self) -> None:
        from engine.plugins.sandbox.core.policy import SandboxPolicy

        p = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED)
        assert p.trust_level == "untrusted"

    def test_invalid_string_falls_back_to_untrusted(self) -> None:
        from engine.plugins.sandbox.core.policy import SandboxPolicy

        p = SandboxPolicy.from_trust_level("garbage")
        assert p.trust_level == "untrusted"

    def test_default_plugin_id(self) -> None:
        from engine.plugins.sandbox.core.policy import SandboxPolicy

        p = SandboxPolicy.from_trust_level("untrusted")
        assert p.plugin_id == "unknown"

    def test_trusted_full_has_fewer_blocks_than_untrusted(self) -> None:
        from engine.plugins.sandbox.core.policy import SandboxPolicy

        full = SandboxPolicy.from_trust_level("trusted_full")
        untrusted = SandboxPolicy.from_trust_level("untrusted")
        assert len(full.import_policy.blocked_modules) < len(
            untrusted.import_policy.blocked_modules
        )

    def test_trusted_limited_introspection_partial(self) -> None:
        from engine.plugins.sandbox.core.policy import SandboxPolicy

        p = SandboxPolicy.from_trust_level("trusted_limited")
        assert "eval" in p.introspection_policy.blocked_builtins
        assert "exec" in p.introspection_policy.blocked_builtins
        assert "__subclasses__" in p.introspection_policy.blocked_attributes

    def test_untrusted_introspection_full(self) -> None:
        from engine.plugins.sandbox.core.policy import IntrospectionPolicy, SandboxPolicy

        untrusted = SandboxPolicy.from_trust_level("untrusted")
        default_introp = IntrospectionPolicy()
        assert untrusted.introspection_policy.blocked_builtins == default_introp.blocked_builtins
        assert untrusted.introspection_policy.blocked_attributes == default_introp.blocked_attributes


class TestTrustedPolicy:
    def test_returns_trusted_policy(self) -> None:
        from engine.plugins.sandbox.core.policy import SandboxPolicy

        p = SandboxPolicy.trusted_policy(plugin_id="my_plugin")
        assert p.plugin_id == "my_plugin"
        assert p.trust_level == "trusted"
        assert p.resource_policy.max_cpu_seconds == 300
        assert p.resource_policy.max_memory_bytes == 2 * 1024**3

    def test_default_plugin_id(self) -> None:
        from engine.plugins.sandbox.core.policy import SandboxPolicy

        p = SandboxPolicy.trusted_policy()
        assert p.plugin_id == "trusted"


# ═══════════════════════════════════════════════════════════════════════════
# 4. SandboxPolicy.from_manifest tests
# ═══════════════════════════════════════════════════════════════════════════


class TestSandboxPolicyFromManifest:
    def test_minimal_manifest(self) -> None:
        from engine.plugins.sandbox.core.policy import SandboxPolicy

        manifest = MagicMock(spec=[])
        manifest.id = "test_plugin"
        p = SandboxPolicy.from_manifest(manifest)
        assert p.plugin_id == "test_plugin"

    def test_manifest_without_id(self) -> None:
        from engine.plugins.sandbox.core.policy import SandboxPolicy

        manifest = object()
        p = SandboxPolicy.from_manifest(manifest)
        assert p.plugin_id == "unknown"

    def test_manifest_with_resources(self) -> None:
        from engine.plugins.sandbox.core.policy import SandboxPolicy

        manifest = MagicMock(spec=["resources"])
        manifest.id = "res_plugin"
        manifest.resources = MagicMock()
        manifest.resources.max_cpu_seconds = 60
        manifest.resources.max_memory = "1GB"
        p = SandboxPolicy.from_manifest(manifest)
        assert p.resource_policy.max_cpu_seconds == 60
        assert p.resource_policy.max_memory_bytes == 1024**3

    def test_manifest_with_artifacts(self) -> None:
        from engine.plugins.sandbox.core.policy import SandboxPolicy

        manifest = MagicMock(spec=["artifacts"])
        manifest.id = "art_plugin"
        manifest.artifacts = ["/data/file1.csv", "/data/file2.csv"]
        p = SandboxPolicy.from_manifest(manifest)
        assert p.filesystem_policy.read_only_paths == ["/data/file1.csv", "/data/file2.csv"]

    def test_manifest_with_network(self) -> None:
        from engine.plugins.sandbox.core.policy import SandboxPolicy

        manifest = MagicMock(spec=["network", "requires_network"])
        manifest.id = "net_plugin"
        manifest.requires_network.return_value = True
        manifest.network.allowed_endpoints = ["api.example.com"]
        p = SandboxPolicy.from_manifest(manifest)
        assert p.network_policy.allowed_endpoints == ["api.example.com"]

    def test_manifest_network_not_required(self) -> None:
        from engine.plugins.sandbox.core.policy import SandboxPolicy

        manifest = MagicMock(spec=["network", "requires_network"])
        manifest.id = "no_net"
        manifest.requires_network.return_value = False
        manifest.network.allowed_endpoints = ["api.example.com"]
        p = SandboxPolicy.from_manifest(manifest)
        assert p.network_policy.allowed_endpoints == []

    def test_manifest_without_requires_network_method(self) -> None:
        from engine.plugins.sandbox.core.policy import SandboxPolicy

        manifest = MagicMock(spec=["network"])
        manifest.id = "no_method"
        p = SandboxPolicy.from_manifest(manifest)
        assert p.network_policy.allowed_endpoints == []


# ═══════════════════════════════════════════════════════════════════════════
# 5. _parse_memory tests
# ═══════════════════════════════════════════════════════════════════════════


class TestParseMemory:
    def test_gb(self) -> None:
        from engine.plugins.sandbox.core.policy import _parse_memory

        assert _parse_memory("2GB") == 2 * 1024**3

    def test_mb(self) -> None:
        from engine.plugins.sandbox.core.policy import _parse_memory

        assert _parse_memory("512MB") == 512 * 1024**2

    def test_kb(self) -> None:
        from engine.plugins.sandbox.core.policy import _parse_memory

        assert _parse_memory("256KB") == 256 * 1024

    def test_bytes(self) -> None:
        from engine.plugins.sandbox.core.policy import _parse_memory

        assert _parse_memory("1024B") == 1024

    def test_plain_number(self) -> None:
        from engine.plugins.sandbox.core.policy import _parse_memory

        assert _parse_memory("1048576") == 1_048_576

    def test_case_insensitive(self) -> None:
        from engine.plugins.sandbox.core.policy import _parse_memory

        assert _parse_memory("512mb") == 512 * 1024**2

    def test_whitespace(self) -> None:
        from engine.plugins.sandbox.core.policy import _parse_memory

        assert _parse_memory("  1GB  ") == 1024**3

    def test_fractional_gb(self) -> None:
        from engine.plugins.sandbox.core.policy import _parse_memory

        assert _parse_memory("1.5GB") == int(1.5 * 1024**3)

    def test_zero(self) -> None:
        from engine.plugins.sandbox.core.policy import _parse_memory

        assert _parse_memory("0MB") == 0


# ═══════════════════════════════════════════════════════════════════════════
# 6. SecurityEventLogger tests
# ═══════════════════════════════════════════════════════════════════════════


class TestSecurityEventLogger:
    def test_initial_state(self) -> None:
        from engine.plugins.sandbox.core.context import SecurityEventLogger

        logger = SecurityEventLogger(plugin_id="test")
        assert logger.event_count == 0

    def test_log_violation(self) -> None:
        from engine.plugins.sandbox.core.context import SecurityEventLogger
        from engine.plugins.sandbox.core.violation import (
            ImportViolation,
        )

        logger = SecurityEventLogger(plugin_id="test")
        violation = ImportViolation("os", plugin_id="test")
        logger.log_violation(violation)
        assert logger.event_count == 1

    def test_get_events_returns_logged(self) -> None:
        from engine.plugins.sandbox.core.context import SecurityEventLogger
        from engine.plugins.sandbox.core.violation import (
            ImportViolation,
            NetworkViolation,
        )

        logger = SecurityEventLogger(plugin_id="test")
        logger.log_violation(ImportViolation("os", plugin_id="test"))
        logger.log_violation(NetworkViolation("evil.com", plugin_id="test"))
        events = logger.get_events()
        assert len(events) == 2

    def test_get_events_filtered_by_category(self) -> None:
        from engine.plugins.sandbox.core.context import SecurityEventLogger
        from engine.plugins.sandbox.core.violation import (
            ImportViolation,
            NetworkViolation,
            SandboxViolationCategory,
        )

        logger = SecurityEventLogger(plugin_id="test")
        logger.log_violation(ImportViolation("os", plugin_id="test"))
        logger.log_violation(NetworkViolation("evil.com", plugin_id="test"))

        import_events = logger.get_events(category=SandboxViolationCategory.IMPORT)
        assert len(import_events) == 1
        assert import_events[0].category == SandboxViolationCategory.IMPORT

    def test_get_events_limit(self) -> None:
        from engine.plugins.sandbox.core.context import SecurityEventLogger
        from engine.plugins.sandbox.core.violation import ImportViolation

        logger = SecurityEventLogger(plugin_id="test")
        for i in range(10):
            logger.log_violation(ImportViolation(f"mod_{i}", plugin_id="test"))

        events = logger.get_events(limit=3)
        assert len(events) == 3

    def test_get_events_returns_most_recent(self) -> None:
        from engine.plugins.sandbox.core.context import SecurityEventLogger
        from engine.plugins.sandbox.core.violation import ImportViolation

        logger = SecurityEventLogger(plugin_id="test")
        for i in range(10):
            logger.log_violation(ImportViolation(f"mod_{i}", plugin_id="test"))

        events = logger.get_events(limit=3)
        details = [e.detail for e in events]
        assert "mod_7" in details[0] or any("mod_7" in d for d in details)

    def test_clear(self) -> None:
        from engine.plugins.sandbox.core.context import SecurityEventLogger
        from engine.plugins.sandbox.core.violation import ImportViolation

        logger = SecurityEventLogger(plugin_id="test")
        logger.log_violation(ImportViolation("os", plugin_id="test"))
        logger.clear()
        assert logger.event_count == 0

    def test_log_violation_uses_logger_plugin_id_as_fallback(self) -> None:
        from engine.plugins.sandbox.core.context import SecurityEventLogger
        from engine.plugins.sandbox.core.violation import ImportViolation

        logger = SecurityEventLogger(plugin_id="fallback_id")
        violation = ImportViolation("os", plugin_id=None)
        logger.log_violation(violation)
        events = logger.get_events()
        assert events[0].plugin_id == "fallback_id"

    def test_event_has_stack_trace(self) -> None:
        from engine.plugins.sandbox.core.context import SecurityEventLogger
        from engine.plugins.sandbox.core.violation import ImportViolation

        logger = SecurityEventLogger(plugin_id="test")
        logger.log_violation(ImportViolation("os", plugin_id="test"))
        events = logger.get_events()
        assert events[0].stack_trace is not None

    def test_event_has_timestamp(self) -> None:
        from engine.plugins.sandbox.core.context import SecurityEventLogger
        from engine.plugins.sandbox.core.violation import ImportViolation

        logger = SecurityEventLogger(plugin_id="test")
        logger.log_violation(ImportViolation("os", plugin_id="test"))
        events = logger.get_events()
        assert events[0].timestamp > 0


# ═══════════════════════════════════════════════════════════════════════════
# 7. SandboxContext tests
# ═══════════════════════════════════════════════════════════════════════════


def _make_untrusted_policy() -> Any:
    from engine.plugins.sandbox.core.policy import (
        FilesystemPolicy,
        ImportPolicy,
        ResourcePolicy,
        SandboxPolicy,
    )

    return SandboxPolicy(
        plugin_id="test",
        trust_level="untrusted",
        import_policy=ImportPolicy(
            blocked_modules={
                "os", "subprocess", "shutil", "pathlib", "io", "_io",
                "socket", "_socket", "http", "urllib", "ftplib", "smtplib",
                "ctypes", "_ctypes", "multiprocessing", "signal", "sys",
                "importlib", "threading", "_thread", "concurrent",
                "gc", "inspect", "code", "codeop", "ast", "dis",
            },
        ),
        resource_policy=ResourcePolicy(max_cpu_seconds=30),
        filesystem_policy=FilesystemPolicy(),
    )


def _make_limited_policy() -> Any:
    from engine.plugins.sandbox.core.policy import (
        ImportPolicy,
        IntrospectionPolicy,
        ResourcePolicy,
        SandboxPolicy,
    )

    return SandboxPolicy(
        plugin_id="limited_test",
        trust_level="trusted_limited",
        import_policy=ImportPolicy(
            blocked_modules={"os", "subprocess", "shutil", "ctypes", "_ctypes"},
        ),
        resource_policy=ResourcePolicy(max_cpu_seconds=120),
        introspection_policy=IntrospectionPolicy(
            blocked_builtins={"eval", "exec", "compile"},
            blocked_attributes={"__subclasses__", "__globals__"},
        ),
    )


def _make_trusted_full_policy() -> Any:
    from engine.plugins.sandbox.core.policy import (
        ImportPolicy,
        IntrospectionPolicy,
        ResourcePolicy,
        SandboxPolicy,
    )

    return SandboxPolicy(
        plugin_id="trusted_test",
        trust_level="trusted_full",
        import_policy=ImportPolicy(
            blocked_modules={"subprocess", "ctypes", "_ctypes"},
        ),
        resource_policy=ResourcePolicy(max_cpu_seconds=300),
        introspection_policy=IntrospectionPolicy(
            blocked_builtins={"exec", "compile"},
            blocked_attributes={"__subclasses__", "__globals__"},
        ),
    )


class TestSandboxContextConstruction:
    def test_policy_property(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext

        policy = _make_untrusted_policy()
        ctx = SandboxContext(policy)
        assert ctx.policy is policy

    def test_not_active_initially(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext

        policy = _make_untrusted_policy()
        ctx = SandboxContext(policy)
        assert ctx.is_active is False

    def test_event_logger_has_plugin_id(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext

        policy = _make_untrusted_policy()
        ctx = SandboxContext(policy)
        assert ctx.event_logger._plugin_id == "test"


class TestSandboxContextTrustLevel:
    def test_untrusted(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext

        ctx = SandboxContext(_make_untrusted_policy())
        assert ctx.trust_level is TrustLevel.UNTRUSTED

    def test_trusted_limited(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext

        ctx = SandboxContext(_make_limited_policy())
        assert ctx.trust_level is TrustLevel.TRUSTED_LIMITED

    def test_trusted_full(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext

        ctx = SandboxContext(_make_trusted_full_policy())
        assert ctx.trust_level is TrustLevel.TRUSTED_FULL

    def test_invalid_string_falls_back(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext
        from engine.plugins.sandbox.core.policy import SandboxPolicy

        policy = SandboxPolicy(plugin_id="bad", trust_level="garbage")
        ctx = SandboxContext(policy)
        assert ctx.trust_level is TrustLevel.UNTRUSTED


class TestSandboxContextValidateTrustLevel:
    def test_untrusted_valid(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext

        ctx = SandboxContext(_make_untrusted_policy())
        assert ctx.validate_trust_level() is True

    def test_untrusted_too_few_blocked_modules(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext
        from engine.plugins.sandbox.core.policy import ImportPolicy, SandboxPolicy

        policy = SandboxPolicy(
            plugin_id="weak",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules={"os"}),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False

    def test_untrusted_cpu_too_high(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext
        from engine.plugins.sandbox.core.policy import ImportPolicy, ResourcePolicy, SandboxPolicy

        policy = SandboxPolicy(
            plugin_id="weak_cpu",
            trust_level="untrusted",
            import_policy=ImportPolicy(
                blocked_modules={f"mod_{i}" for i in range(15)},
            ),
            resource_policy=ResourcePolicy(max_cpu_seconds=120),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False

    def test_untrusted_with_rw_paths(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext
        from engine.plugins.sandbox.core.policy import (
            FilesystemPolicy,
            ImportPolicy,
            SandboxPolicy,
        )

        policy = SandboxPolicy(
            plugin_id="rw_leak",
            trust_level="untrusted",
            import_policy=ImportPolicy(
                blocked_modules={f"mod_{i}" for i in range(15)},
            ),
            filesystem_policy=FilesystemPolicy(read_write_paths=["/tmp"]),  # noqa: S108
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False

    def test_limited_valid(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext

        ctx = SandboxContext(_make_limited_policy())
        assert ctx.validate_trust_level() is True

    def test_limited_too_few_blocked_modules(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext
        from engine.plugins.sandbox.core.policy import ImportPolicy, SandboxPolicy

        policy = SandboxPolicy(
            plugin_id="weak_limited",
            trust_level="trusted_limited",
            import_policy=ImportPolicy(blocked_modules={"os"}),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False

    def test_limited_cpu_too_high(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext
        from engine.plugins.sandbox.core.policy import ImportPolicy, ResourcePolicy, SandboxPolicy

        policy = SandboxPolicy(
            plugin_id="weak_limited_cpu",
            trust_level="trusted_limited",
            import_policy=ImportPolicy(
                blocked_modules={f"mod_{i}" for i in range(10)},
            ),
            resource_policy=ResourcePolicy(max_cpu_seconds=300),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False

    def test_trusted_full_always_valid(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext

        ctx = SandboxContext(_make_trusted_full_policy())
        assert ctx.validate_trust_level() is True

    def test_trusted_full_minimal_blocks_still_valid(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext
        from engine.plugins.sandbox.core.policy import ImportPolicy, SandboxPolicy

        policy = SandboxPolicy(
            plugin_id="full_minimal",
            trust_level="trusted_full",
            import_policy=ImportPolicy(blocked_modules=set()),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True


class TestSandboxContextLifecycle:
    def test_activate_sets_active(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext

        ctx = SandboxContext(_make_untrusted_policy())
        try:
            ctx.activate()
            assert ctx.is_active is True
        finally:
            ctx.cleanup()

    def test_deactivate_clears_active(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext

        ctx = SandboxContext(_make_untrusted_policy())
        ctx.activate()
        ctx.deactivate()
        assert ctx.is_active is False
        ctx._filesystem_layer.cleanup()

    def test_double_activate_is_noop(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext

        ctx = SandboxContext(_make_untrusted_policy())
        try:
            ctx.activate()
            ctx.activate()
            assert ctx.is_active is True
        finally:
            ctx.cleanup()

    def test_double_deactivate_is_safe(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext

        ctx = SandboxContext(_make_untrusted_policy())
        ctx.activate()
        ctx.deactivate()
        ctx.deactivate()
        assert ctx.is_active is False
        ctx._filesystem_layer.cleanup()

    def test_deactivate_without_activate_is_safe(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext

        ctx = SandboxContext(_make_untrusted_policy())
        ctx.deactivate()
        assert ctx.is_active is False
        ctx._filesystem_layer.cleanup()

    def test_context_manager(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext

        ctx = SandboxContext(_make_untrusted_policy())
        with ctx:
            assert ctx.is_active is True
        assert ctx.is_active is False
        ctx._filesystem_layer.cleanup()

    def test_cleanup_removes_work_dir(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext

        ctx = SandboxContext(_make_untrusted_policy())
        work_dir = ctx.work_dir
        assert os.path.isdir(work_dir)
        ctx.cleanup()
        assert not os.path.isdir(work_dir)


# ═══════════════════════════════════════════════════════════════════════════
# 8. Import isolation tests
# ═══════════════════════════════════════════════════════════════════════════


class TestTrustLevelsImportIsolation:
    def test_trust_levels_no_sandbox_core_imports(self) -> None:
        source = inspect.getsource(
            sys.modules["engine.plugins.trust_levels"]
        )
        assert "sandbox.core" not in source
        assert "from engine.plugins.sandbox" not in source

    def test_trust_levels_no_sandbox_core_ast(self) -> None:
        source = inspect.getsource(
            sys.modules["engine.plugins.trust_levels"]
        )
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "sandbox" not in node.module, (
                    f"trust_levels.py imports from sandbox: {node.module}"
                )


class TestLazyImportCaching:
    def test_policy_lazy_cache_returns_same_class(self) -> None:
        from engine.plugins.sandbox.core.policy import _get_trust_level_cls

        cls1 = _get_trust_level_cls()
        cls2 = _get_trust_level_cls()
        assert cls1 is cls2
        assert cls1 is TrustLevel

    def test_context_lazy_cache_returns_same_class(self) -> None:
        from engine.plugins.sandbox.core.context import _get_trust_level_cls

        cls1 = _get_trust_level_cls()
        cls2 = _get_trust_level_cls()
        assert cls1 is cls2
        assert cls1 is TrustLevel

    def test_both_modules_cache_same_class(self) -> None:
        from engine.plugins.sandbox.core.context import (
            _get_trust_level_cls as ctx_getter,
        )
        from engine.plugins.sandbox.core.policy import (
            _get_trust_level_cls as pol_getter,
        )

        assert pol_getter() is ctx_getter()

    def test_no_circular_import_on_module_load(self) -> None:
        already_loaded = "engine.plugins.sandbox.core.policy" in sys.modules
        if already_loaded:
            import importlib

            importlib.reload(sys.modules["engine.plugins.sandbox.core.policy"])
        else:
            import engine.plugins.sandbox.core.policy  # noqa: F401


class TestPolicyNoTopLevelTrustLevelImport:
    def test_policy_module_level_imports_exclude_trust_levels(self) -> None:
        source = inspect.getsource(
            sys.modules["engine.plugins.sandbox.core.policy"]
        )
        tree = ast.parse(source)
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "trust_levels" not in node.module, (
                    f"Top-level import of trust_levels in policy.py: {node.module}"
                )

    def test_context_module_level_imports_exclude_trust_levels(self) -> None:
        source = inspect.getsource(
            sys.modules["engine.plugins.sandbox.core.context"]
        )
        tree = ast.parse(source)
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "trust_levels" not in node.module, (
                    f"Top-level import of trust_levels in context.py: {node.module}"
                )


class TestLazyImportCacheReset:
    def test_policy_cache_can_be_reset_and_refilled(self) -> None:
        import engine.plugins.sandbox.core.policy as policy_mod

        original_cache = policy_mod._CACHED_TRUST_LEVEL
        policy_mod._CACHED_TRUST_LEVEL = None
        result = policy_mod._get_trust_level_cls()
        assert result is TrustLevel
        policy_mod._CACHED_TRUST_LEVEL = original_cache

    def test_context_cache_can_be_reset_and_refilled(self) -> None:
        import engine.plugins.sandbox.core.context as ctx_mod

        original_cache = ctx_mod._CACHED_TRUST_LEVEL
        ctx_mod._CACHED_TRUST_LEVEL = None
        result = ctx_mod._get_trust_level_cls()
        assert result is TrustLevel
        ctx_mod._CACHED_TRUST_LEVEL = original_cache


# ═══════════════════════════════════════════════════════════════════════════
# 9. Edge cases and boundary values
# ═══════════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    def test_import_policy_root_extraction(self) -> None:
        from engine.plugins.sandbox.core.policy import ImportPolicy

        p = ImportPolicy(blocked_modules={"os"})
        assert p.is_allowed("os") is False
        assert p.is_allowed("os.path") is False
        assert p.is_allowed("ospath") is True

    def test_network_policy_empty_string_host(self) -> None:
        from engine.plugins.sandbox.core.policy import NetworkPolicy

        p = NetworkPolicy(allowed_endpoints=[""])
        assert p.is_host_allowed("") is True
        assert p.is_host_allowed("anything") is False

    def test_parse_memory_large_value(self) -> None:
        from engine.plugins.sandbox.core.policy import _parse_memory

        result = _parse_memory("100GB")
        assert result == 100 * 1024**3

    def test_sandbox_policy_dataclass_equality(self) -> None:
        from engine.plugins.sandbox.core.policy import SandboxPolicy

        p1 = SandboxPolicy()
        p2 = SandboxPolicy()
        assert p1 == p2

    def test_sandbox_policy_dataclass_inequality(self) -> None:
        from engine.plugins.sandbox.core.policy import SandboxPolicy

        p1 = SandboxPolicy(plugin_id="a")
        p2 = SandboxPolicy(plugin_id="b")
        assert p1 != p2

    def test_validate_trust_level_boundary_untrusted_exactly_10_blocked(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext
        from engine.plugins.sandbox.core.policy import ImportPolicy, SandboxPolicy

        policy = SandboxPolicy(
            plugin_id="boundary",
            trust_level="untrusted",
            import_policy=ImportPolicy(
                blocked_modules={f"mod_{i}" for i in range(10)},
            ),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True

    def test_validate_trust_level_boundary_untrusted_9_blocked(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext
        from engine.plugins.sandbox.core.policy import ImportPolicy, SandboxPolicy

        policy = SandboxPolicy(
            plugin_id="boundary_9",
            trust_level="untrusted",
            import_policy=ImportPolicy(
                blocked_modules={f"mod_{i}" for i in range(9)},
            ),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False

    def test_validate_trust_level_boundary_limited_exactly_5_blocked(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext
        from engine.plugins.sandbox.core.policy import ImportPolicy, SandboxPolicy

        policy = SandboxPolicy(
            plugin_id="boundary_limited",
            trust_level="trusted_limited",
            import_policy=ImportPolicy(
                blocked_modules={f"mod_{i}" for i in range(5)},
            ),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True

    def test_validate_trust_level_boundary_limited_4_blocked(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext
        from engine.plugins.sandbox.core.policy import ImportPolicy, SandboxPolicy

        policy = SandboxPolicy(
            plugin_id="boundary_limited_4",
            trust_level="trusted_limited",
            import_policy=ImportPolicy(
                blocked_modules={f"mod_{i}" for i in range(4)},
            ),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False

    def test_validate_trust_level_untrusted_cpu_exactly_60(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext
        from engine.plugins.sandbox.core.policy import ImportPolicy, ResourcePolicy, SandboxPolicy

        policy = SandboxPolicy(
            plugin_id="cpu_boundary",
            trust_level="untrusted",
            import_policy=ImportPolicy(
                blocked_modules={f"mod_{i}" for i in range(15)},
            ),
            resource_policy=ResourcePolicy(max_cpu_seconds=60),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True

    def test_validate_trust_level_limited_cpu_exactly_120(self) -> None:
        from engine.plugins.sandbox.core.context import SandboxContext
        from engine.plugins.sandbox.core.policy import ImportPolicy, ResourcePolicy, SandboxPolicy

        policy = SandboxPolicy(
            plugin_id="cpu_boundary_limited",
            trust_level="trusted_limited",
            import_policy=ImportPolicy(
                blocked_modules={f"mod_{i}" for i in range(10)},
            ),
            resource_policy=ResourcePolicy(max_cpu_seconds=120),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True

    def test_from_trust_level_enum_identity(self) -> None:
        from engine.plugins.sandbox.core.policy import SandboxPolicy

        p = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_FULL)
        assert p.trust_level == "trusted"

        p2 = SandboxPolicy.from_trust_level(TrustLevel("trusted_full"))
        assert p2.trust_level == "trusted"
