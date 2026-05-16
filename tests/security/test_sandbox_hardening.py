"""Tests for sandbox hardening: SSRF, wall-time, path validation, trust enforcement."""

from __future__ import annotations

import builtins
from typing import Any

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
from engine.plugins.sandbox.core.violation import (
    ResourceExhausted,
)
from engine.plugins.sandbox.layers.filesystem_isolation import FilesystemIsolation
from engine.plugins.sandbox.layers.introspection_guard import (
    _EXPLICITLY_BLOCKED_ATTRS,
    IntrospectionGuard,
)
from engine.plugins.sandbox.layers.network_guard import (
    NetworkGuard,
    _is_private_ip,
)
from engine.plugins.sandbox.layers.resource_limiter import ResourceLimiter
from engine.plugins.sandbox.monitoring.violation_report import ViolationReport
from engine.plugins.trust_levels import TrustLevel


class TestSSRFProtection:
    def test_loopback_ipv4_blocked(self) -> None:
        assert _is_private_ip("127.0.0.1") is True

    def test_loopback_range_blocked(self) -> None:
        assert _is_private_ip("127.255.255.255") is True

    def test_private_10_range_blocked(self) -> None:
        assert _is_private_ip("10.0.0.1") is True

    def test_private_172_range_blocked(self) -> None:
        assert _is_private_ip("172.16.0.1") is True

    def test_private_192_range_blocked(self) -> None:
        assert _is_private_ip("192.168.1.1") is True

    def test_link_local_blocked(self) -> None:
        assert _is_private_ip("169.254.1.1") is True

    def test_zero_network_blocked(self) -> None:
        assert _is_private_ip("0.0.0.0") is True  # noqa: S104

    def test_ipv6_loopback_blocked(self) -> None:
        assert _is_private_ip("::1") is True

    def test_ipv6_unique_local_blocked(self) -> None:
        assert _is_private_ip("fc00::1") is True

    def test_ipv6_link_local_blocked(self) -> None:
        assert _is_private_ip("fe80::1") is True

    def test_public_ip_not_blocked(self) -> None:
        assert _is_private_ip("8.8.8.8") is False

    def test_hostname_not_blocked(self) -> None:
        assert _is_private_ip("example.com") is False

    def test_private_ip_blocked_by_guard(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=[])
        guard = NetworkGuard(policy, plugin_id="test")
        assert guard._is_host_allowed("127.0.0.1") is False

    def test_private_ip_allowed_if_in_cidr(self) -> None:
        policy = NetworkPolicy(
            allowed_endpoints=[],
            allowed_cidrs=["10.0.0.0/8"],
        )
        guard = NetworkGuard(policy, plugin_id="test")
        assert guard._is_host_allowed("10.0.1.50") is True

    def test_public_ip_blocked_without_whitelist(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=[])
        guard = NetworkGuard(policy, plugin_id="test")
        assert guard._is_host_allowed("8.8.8.8") is False

    def test_private_ip_blocked_even_with_whitelist(self) -> None:
        policy = NetworkPolicy(allowed_endpoints=["safe.example.com"])
        guard = NetworkGuard(policy, plugin_id="test")
        assert guard._is_host_allowed("127.0.0.1") is False


class TestWallTimeEnforcement:
    def test_wall_time_check_raises_on_exceed(self) -> None:
        policy = ResourcePolicy(wall_time_seconds=0.001, max_cpu_seconds=999)
        limiter = ResourceLimiter(policy, plugin_id="test")
        limiter.install()
        try:
            import time

            time.sleep(0.01)
            with pytest.raises(ResourceExhausted, match="wall_time"):
                limiter.check_wall_timer()
        finally:
            limiter.uninstall()

    def test_wall_time_within_limit_no_raise(self) -> None:
        policy = ResourcePolicy(wall_time_seconds=60.0, max_cpu_seconds=60.0)
        limiter = ResourceLimiter(policy, plugin_id="test")
        limiter.install()
        try:
            limiter.check_wall_timer()
        finally:
            limiter.uninstall()

    def test_wall_time_resource_exhausted_fields(self) -> None:
        policy = ResourcePolicy(wall_time_seconds=0.001, max_cpu_seconds=999)
        limiter = ResourceLimiter(policy, plugin_id="test_plugin")
        limiter.install()
        try:
            import time

            time.sleep(0.01)
            with pytest.raises(ResourceExhausted) as exc_info:
                limiter.check_wall_timer()
            assert exc_info.value.resource_type == "wall_time"
            assert exc_info.value.plugin_id == "test_plugin"
        finally:
            limiter.uninstall()


class TestPathValidationHardening:
    def test_path_traversal_dotdot_blocked(self) -> None:
        policy = FilesystemPolicy(block_symlinks=True)
        fs = FilesystemIsolation(policy=policy, plugin_id="test")
        with pytest.raises(PermissionError, match="path_traversal"):
            fs._validate_path("/safe/dir/../../../etc/passwd")

    def test_path_traversal_component_blocked(self) -> None:
        policy = FilesystemPolicy(block_symlinks=True)
        fs = FilesystemIsolation(policy=policy, plugin_id="test")
        with pytest.raises(PermissionError, match="path_traversal"):
            fs._validate_path("../../../etc/shadow")

    def test_proc_path_blocked(self) -> None:
        policy = FilesystemPolicy(block_symlinks=True)
        fs = FilesystemIsolation(policy=policy, plugin_id="test")
        with pytest.raises(PermissionError, match="system_path"):
            fs._validate_path("/proc/self/environ")

    def test_sys_path_blocked(self) -> None:
        policy = FilesystemPolicy(block_symlinks=True)
        fs = FilesystemIsolation(policy=policy, plugin_id="test")
        with pytest.raises(PermissionError, match="system_path"):
            fs._validate_path("/sys/kernel/notes")

    def test_dev_path_blocked(self) -> None:
        policy = FilesystemPolicy(block_symlinks=True)
        fs = FilesystemIsolation(policy=policy, plugin_id="test")
        with pytest.raises(PermissionError, match="system_path"):
            fs._validate_path("/dev/null")

    def test_symlink_path_blocked(self, tmp_path: Any) -> None:
        target = tmp_path / "target.txt"
        target.write_text("secret")
        link = tmp_path / "link.txt"
        link.symlink_to(target)
        policy = FilesystemPolicy(block_symlinks=True)
        fs = FilesystemIsolation(policy=policy, plugin_id="test")
        with pytest.raises(PermissionError, match="symlink"):
            fs._validate_path(str(link))

    def test_normal_path_passes(self, tmp_path: Any) -> None:
        safe_dir = tmp_path / "safe"
        safe_dir.mkdir()
        policy = FilesystemPolicy(block_symlinks=True)
        fs = FilesystemIsolation(policy=policy, plugin_id="test")
        result = fs._validate_path(str(safe_dir / "file.txt"))
        assert isinstance(result, str)

    def test_violation_logged_on_path_traversal(self) -> None:
        policy = FilesystemPolicy(block_symlinks=True)
        fs = FilesystemIsolation(policy=policy, plugin_id="test_plugin")
        with pytest.raises(PermissionError):
            fs._validate_path("../../../etc/passwd")
        violations = fs.get_violations()
        assert len(violations) == 1
        assert violations[0].operation == "path_traversal"
        assert violations[0].plugin_id == "test_plugin"

    def test_violation_logged_on_system_path(self) -> None:
        policy = FilesystemPolicy(block_symlinks=True)
        fs = FilesystemIsolation(policy=policy, plugin_id="test_plugin")
        with pytest.raises(PermissionError):
            fs._validate_path("/proc/self/environ")
        violations = fs.get_violations()
        assert len(violations) == 1
        assert violations[0].operation == "system_path"


class TestPickleEscapeVectorsBlocked:
    @pytest.mark.parametrize(
        "attr",
        ["__reduce__", "__reduce_ex__", "__getstate__", "__setstate__"],
    )
    def test_pickle_attrs_in_blocked_set(self, attr: str) -> None:
        assert attr in _EXPLICITLY_BLOCKED_ATTRS

    @pytest.mark.parametrize(
        "attr",
        ["__reduce__", "__reduce_ex__", "__getstate__", "__setstate__"],
    )
    def test_pickle_attrs_blocked_by_guard(self, attr: str) -> None:
        policy = IntrospectionPolicy()
        guard = IntrospectionGuard(policy, plugin_id="test")
        guard.install()
        try:
            with pytest.raises(PermissionError, match="not accessible"):
                builtins.getattr(object(), attr)
        finally:
            guard.uninstall()

    def test_reduce_violation_logged(self) -> None:
        policy = IntrospectionPolicy()
        guard = IntrospectionGuard(policy, plugin_id="test_plugin")
        guard.install()
        try:
            with pytest.raises(PermissionError, match="not accessible"):
                builtins.getattr(object(), "__reduce__")  # noqa: B009
        finally:
            guard.uninstall()
        violations = guard.get_violations()
        assert len(violations) >= 1
        assert violations[0].attribute == "__reduce__"


class TestTrustLevelEnforcement:
    def test_context_resolves_untrusted(self) -> None:
        policy = SandboxPolicy(plugin_id="test", trust_level="untrusted")
        ctx = SandboxContext(policy)
        assert ctx.trust_level is TrustLevel.UNTRUSTED

    def test_context_resolves_trusted_full(self) -> None:
        policy = SandboxPolicy(plugin_id="test", trust_level="trusted_full")
        ctx = SandboxContext(policy)
        assert ctx.trust_level is TrustLevel.TRUSTED_FULL

    def test_context_resolves_trusted_limited(self) -> None:
        policy = SandboxPolicy(plugin_id="test", trust_level="trusted_limited")
        ctx = SandboxContext(policy)
        assert ctx.trust_level is TrustLevel.TRUSTED_LIMITED

    def test_context_defaults_invalid_to_untrusted(self) -> None:
        policy = SandboxPolicy(plugin_id="test", trust_level="invalid_level")
        ctx = SandboxContext(policy)
        assert ctx.trust_level is TrustLevel.UNTRUSTED

    def test_validate_untrusted_passes_with_strict_policy(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "test")
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True

    def test_validate_untrusted_fails_with_lax_imports(self) -> None:
        policy = SandboxPolicy(
            plugin_id="test",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules=set()),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False

    def test_validate_untrusted_fails_with_high_cpu(self) -> None:
        policy = SandboxPolicy(
            plugin_id="test",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules=set(range(20))),
            resource_policy=ResourcePolicy(max_cpu_seconds=120),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False

    def test_validate_untrusted_fails_with_rw_paths(self) -> None:
        policy = SandboxPolicy(
            plugin_id="test",
            trust_level="untrusted",
            import_policy=ImportPolicy(blocked_modules=set(range(20))),
            filesystem_policy=FilesystemPolicy(read_write_paths=["/tmp"]),  # noqa: S108
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False

    def test_validate_trusted_limited_passes(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_LIMITED, "test")
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True

    def test_validate_trusted_limited_fails_with_high_cpu(self) -> None:
        policy = SandboxPolicy(
            plugin_id="test",
            trust_level="trusted_limited",
            import_policy=ImportPolicy(blocked_modules=set(range(20))),
            resource_policy=ResourcePolicy(max_cpu_seconds=300),
        )
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is False

    def test_validate_trusted_full_passes(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_FULL, "test")
        ctx = SandboxContext(policy)
        assert ctx.validate_trust_level() is True


class TestViolationReportTrustLevel:
    def test_report_includes_trust_level(self) -> None:
        report = ViolationReport(plugin_id="test", trust_level="untrusted")
        d = report.to_dict()
        assert d["trust_level"] == "untrusted"

    def test_report_defaults_trust_level_none(self) -> None:
        report = ViolationReport(plugin_id="test")
        d = report.to_dict()
        assert d["trust_level"] is None

    def test_report_from_events_with_trust_level(self) -> None:
        report = ViolationReport.from_events(
            events=[],
            plugin_id="test",
            trust_level="trusted_limited",
        )
        assert report.trust_level == "trusted_limited"

    def test_report_summary_includes_trust_level(self) -> None:
        report = ViolationReport(plugin_id="test", trust_level="untrusted")
        summary = report.summary()
        assert "untrusted" in summary

    def test_report_summary_unknown_when_none(self) -> None:
        report = ViolationReport(plugin_id="test")
        summary = report.summary()
        assert "unknown" in summary


class TestSandboxPolicyFromTrustLevelIntegration:
    def test_untrusted_has_strict_imports(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "test")
        assert "os" in policy.import_policy.blocked_modules
        assert "subprocess" in policy.import_policy.blocked_modules
        assert "sys" in policy.import_policy.blocked_modules

    def test_untrusted_no_rw_paths(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "test")
        assert policy.filesystem_policy.read_write_paths == []

    def test_untrusted_has_strict_introspection(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "test")
        assert "eval" in policy.introspection_policy.blocked_builtins
        assert "exec" in policy.introspection_policy.blocked_builtins
        assert "__subclasses__" in policy.introspection_policy.blocked_attributes

    def test_trusted_full_has_relaxed_imports(self) -> None:
        policy = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_FULL, "test")
        assert "os" not in policy.import_policy.blocked_modules
        assert "subprocess" in policy.import_policy.blocked_modules

    def test_trusted_full_has_higher_cpu(self) -> None:
        untrusted = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "test")
        trusted = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_FULL, "test")
        assert trusted.resource_policy.max_cpu_seconds > untrusted.resource_policy.max_cpu_seconds

    def test_trusted_limited_between_extremes(self) -> None:
        untrusted = SandboxPolicy.from_trust_level(TrustLevel.UNTRUSTED, "test")
        limited = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_LIMITED, "test")
        trusted = SandboxPolicy.from_trust_level(TrustLevel.TRUSTED_FULL, "test")
        assert (
            untrusted.resource_policy.max_cpu_seconds
            <= limited.resource_policy.max_cpu_seconds
            <= trusted.resource_policy.max_cpu_seconds
        )


class TestSDKStrategyConfigTrustLevel:
    def test_default_trust_level(self) -> None:
        from nexus_sdk.strategy import StrategyConfig

        config = StrategyConfig(strategy_id="test")
        assert config.trust_level == "untrusted"

    def test_explicit_trust_level(self) -> None:
        from nexus_sdk.strategy import StrategyConfig

        config = StrategyConfig(strategy_id="test", trust_level="trusted_full")
        assert config.trust_level == "trusted_full"
