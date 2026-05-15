"""
Adversarial tests for sandbox security gaps filled in the 5-layer model.

Tests cover:
  L1: importlib.import_module bypass, sys.modules manipulation
  L3: CPU timer tracking, thread limit enforcement
  L5: safe dir replacement, vars/globals/locals blocking,
      traceback frame access blocking, setattr restriction
  Cross-layer: combined attack vectors, import via getattr, type introspection
"""

from __future__ import annotations

import builtins
import os
import sys
from typing import Any

import pytest

from engine.plugins.manifest import StrategyManifest
from engine.plugins.sandbox import StrategySandbox
from engine.plugins.sandbox.core.context import SandboxContext
from engine.plugins.sandbox.core.policy import (
    ImportPolicy,
    IntrospectionPolicy,
    ResourcePolicy,
    SandboxPolicy,
)
from engine.plugins.sandbox.layers.import_restriction import RestrictedImporter
from engine.plugins.sandbox.layers.introspection_guard import (
    _BLOCKED_BUILTINS_DEFAULT,
    _TRACEBACK_ATTRS,
    IntrospectionGuard,
)
from engine.plugins.sandbox.layers.resource_limiter import ResourceLimiter
from engine.plugins.sandbox.monitoring.event_logger import SecurityEventLogger
from engine.plugins.sandbox.monitoring.violation_report import ViolationReport


def _make_manifest(**overrides: Any) -> StrategyManifest:
    defaults: dict[str, Any] = {
        "id": "test",
        "name": "test",
        "version": "1.0.0",
        "resources": {"max_cpu_seconds": 2},
    }
    defaults.update(overrides)
    return StrategyManifest(**defaults)


# ─── L1: importlib.import_module Bypass ──────────────────────────────


class TestImportlibBypassGuard:
    def test_restricted_importlib_import_module(self) -> None:
        importer = RestrictedImporter(blocked={"os"}, plugin_id="p1")
        importer.install()
        try:
            import importlib

            with pytest.raises(ImportError, match="blocked"):
                importlib.import_module("os")
        finally:
            importer.uninstall()

    def test_importlib_import_module_not_blocked_when_not_installed(self) -> None:
        RestrictedImporter(blocked={"os"}, plugin_id="p1")
        import importlib

        mod = importlib.import_module("json")
        assert mod is not None

    def test_importlib_import_module_logs_violation(self) -> None:
        importer = RestrictedImporter(blocked={"os"}, plugin_id="p1")
        importer.install()
        try:
            import importlib

            with pytest.raises(ImportError):
                importlib.import_module("os")
        finally:
            importer.uninstall()
        violations = importer.get_violations()
        assert any(v.module_name == "os" for v in violations)

    def test_importlib_import_module_restored_on_uninstall(self) -> None:
        importer = RestrictedImporter(blocked={"os"}, plugin_id="p1")
        original_fn = sys.modules["importlib"].import_module
        importer.install()
        assert sys.modules["importlib"].import_module is not original_fn
        importer.uninstall()
        assert sys.modules["importlib"].import_module is original_fn


# ─── L3: CPU Timer and Thread Limits ─────────────────────────────────


class TestCPUTimer:
    def test_cpu_timer_elapsed_tracking(self) -> None:
        import time

        limiter = ResourceLimiter(ResourcePolicy(max_cpu_seconds=10))
        limiter.install()
        try:
            time.sleep(0.05)
            elapsed = limiter.cpu_elapsed
            assert elapsed >= 0.04
        finally:
            limiter.uninstall()

    def test_cpu_timer_check_within_limit(self) -> None:
        limiter = ResourceLimiter(ResourcePolicy(max_cpu_seconds=30))
        limiter.install()
        try:
            limiter.check_cpu_timer()
        finally:
            limiter.uninstall()

    def test_cpu_timer_check_expired(self) -> None:
        from engine.plugins.sandbox.layers.resource_limiter import _CPUTimer

        timer = _CPUTimer(0.01, plugin_id="p1")
        timer.start()
        import time

        time.sleep(0.05)
        with pytest.raises(Exception, match="cpu_time"):
            timer.check()
        timer.stop()

    def test_thread_limit_at_zero(self) -> None:
        limiter = ResourceLimiter(ResourcePolicy(max_threads=0), plugin_id="p1")
        with pytest.raises(Exception, match="threads"):
            limiter.check_thread_limit()

    def test_install_uninstall_cycle_clean(self) -> None:
        limiter = ResourceLimiter(ResourcePolicy(max_cpu_seconds=5))
        limiter.install()
        assert limiter._installed is True
        limiter.uninstall()
        assert limiter._installed is False
        assert limiter._cpu_timer is None


# ─── L5: Blocked Builtins ────────────────────────────────────────────


class TestBlockedBuiltins:
    def test_vars_blocked(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy())
        try:
            guard.install()
            with pytest.raises(PermissionError, match="not accessible"):
                vars()
        finally:
            guard.uninstall()

    def test_globals_blocked(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy())
        try:
            guard.install()
            with pytest.raises(PermissionError, match="not accessible"):
                globals()
        finally:
            guard.uninstall()

    def test_locals_blocked(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy())
        try:
            guard.install()
            with pytest.raises(PermissionError, match="not accessible"):
                locals()
        finally:
            guard.uninstall()

    def test_vars_in_default_blocked_builtins(self) -> None:
        assert "vars" in _BLOCKED_BUILTINS_DEFAULT

    def test_globals_in_default_blocked_builtins(self) -> None:
        assert "globals" in _BLOCKED_BUILTINS_DEFAULT

    def test_locals_in_default_blocked_builtins(self) -> None:
        assert "locals" in _BLOCKED_BUILTINS_DEFAULT

    def test_blocked_builtins_restored_on_uninstall(self) -> None:
        originals = {
            "eval": builtins.eval,
            "exec": builtins.exec,
            "compile": builtins.compile,
            "vars": builtins.vars,
            "globals": builtins.globals,
            "locals": builtins.locals,
        }
        guard = IntrospectionGuard(IntrospectionPolicy())
        guard.install()
        guard.uninstall()
        for name, orig in originals.items():
            assert getattr(builtins, name) is orig


# ─── L5: Safe dir Replacement ────────────────────────────────────────


class TestSafeDirReplacement:
    def test_dir_filters_blocked_attrs(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy())
        try:
            guard.install()
            attrs = dir(str)
            assert "__subclasses__" not in attrs
            assert "__globals__" not in attrs
            assert "__bases__" not in attrs
            assert "__mro__" not in attrs
            assert "upper" in attrs
            assert "lower" in attrs
        finally:
            guard.uninstall()

    def test_dir_restored_on_uninstall(self) -> None:
        original_dir = builtins.dir
        guard = IntrospectionGuard(IntrospectionPolicy())
        guard.install()
        guard.uninstall()
        assert builtins.dir is original_dir


# ─── L5: Traceback Frame Access Blocking ─────────────────────────────


class TestTracebackFrameBlocking:
    def test_traceback_attrs_blocked(self) -> None:
        guard = IntrospectionGuard(IntrospectionPolicy())
        guard._original_getattr = builtins.getattr
        for attr in _TRACEBACK_ATTRS:
            assert guard._is_blocked_attr(attr) is True

    def test_setattr_restricted(self) -> None:
        original_setattr = builtins.setattr
        guard = IntrospectionGuard(IntrospectionPolicy(), plugin_id="p1")
        try:
            guard.install()
            assert builtins.setattr is not original_setattr
        finally:
            guard.uninstall()
        assert builtins.setattr is original_setattr

    def test_setattr_restored_on_uninstall(self) -> None:
        original_setattr = builtins.setattr
        guard = IntrospectionGuard(IntrospectionPolicy())
        guard.install()
        guard.uninstall()
        assert builtins.setattr is original_setattr


# ─── Cross-Layer: Combined Attack Vectors ────────────────────────────


class TestCrossLayerAdversarial:
    async def test_getattr_then_import_blocked(self) -> None:
        manifest = _make_manifest()

        class GetattrImportEscape:
            name = "getattr_import"
            version = "1.0"

            def on_bar(self, s, p):
                getattr(builtins, "__import__")("os")  # noqa: B009
                return []

        sandbox = StrategySandbox(GetattrImportEscape(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors >= 1
        finally:
            sandbox.cleanup()

    async def test_exception_traceback_frame_blocked(self) -> None:
        manifest = _make_manifest()

        class TracebackFrameEscape:
            name = "tb_frame"
            version = "1.0"

            def _try_escape(self):
                try:
                    raise ValueError("test")  # noqa: TRY301
                except ValueError as e:
                    tb = e.__traceback__
                    frame = getattr(tb, "tb_frame", None)
                    if frame is not None:
                        _ = frame.f_globals

            def on_bar(self, s, p):
                self._try_escape()
                return []

        sandbox = StrategySandbox(TracebackFrameEscape(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors >= 1
        finally:
            sandbox.cleanup()

    async def test_vars_for_globals_access_blocked(self) -> None:
        manifest = _make_manifest()

        class VarsGlobalsEscape:
            name = "vars_globals"
            version = "1.0"

            def on_bar(self, s, p):
                v = vars()
                return list(v)

        sandbox = StrategySandbox(VarsGlobalsEscape(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors >= 1
        finally:
            sandbox.cleanup()

    async def test_globals_access_blocked(self) -> None:
        manifest = _make_manifest()

        class GlobalsEscape:
            name = "globals_escape"
            version = "1.0"

            def on_bar(self, s, p):
                g = globals()
                return [k for k in g if "os" in k]

        sandbox = StrategySandbox(GlobalsEscape(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors >= 1
        finally:
            sandbox.cleanup()

    async def test_locals_access_blocked(self) -> None:
        manifest = _make_manifest()

        class LocalsEscape:
            name = "locals_escape"
            version = "1.0"

            def on_bar(self, s, p):
                loc = locals()
                return list(loc)

        sandbox = StrategySandbox(LocalsEscape(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors >= 1
        finally:
            sandbox.cleanup()

    async def test_dir_filters_dangerous_attrs(self) -> None:
        manifest = _make_manifest()

        class DirEscape:
            name = "dir_escape"
            version = "1.0"

            def on_bar(self, s, p):
                attrs = dir(object)
                assert "__subclasses__" not in attrs
                assert "__globals__" not in attrs
                assert "__bases__" not in attrs
                return []

        sandbox = StrategySandbox(DirEscape(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert len(signals) == 0
            assert sandbox.metrics.errors == 0
        finally:
            sandbox.cleanup()

    async def test_builtins_restored_after_all_violations(self) -> None:
        manifest = _make_manifest()
        originals = {
            "__import__": builtins.__import__,
            "open": builtins.open,
            "getattr": builtins.getattr,
            "object": builtins.object,
            "eval": builtins.eval,
            "exec": builtins.exec,
            "compile": builtins.compile,
        }

        class AllViolationStrat:
            name = "all_violations"
            version = "1.0"

            def on_bar(self, s, p):
                import contextlib

                with contextlib.suppress(ImportError, PermissionError):
                    import os  # noqa: F401
                with contextlib.suppress(PermissionError):
                    eval("1+1")  # noqa: S307
                with contextlib.suppress(PermissionError):
                    open("/etc/passwd")  # noqa: SIM115
                return []

        sandbox = StrategySandbox(AllViolationStrat(), manifest)
        try:
            await sandbox.safe_evaluate(None, None, None)
        finally:
            sandbox.cleanup()
        for name, orig in originals.items():
            assert getattr(builtins, name) is orig


# ─── ViolationReport Tests ───────────────────────────────────────────


class TestViolationReport:
    def test_from_events_empty(self) -> None:
        report = ViolationReport.from_events([])
        assert report.total_violations == 0
        assert report.by_category == {}

    def test_from_events_categorizes(self) -> None:
        from engine.plugins.sandbox.core.violation import (
            ImportViolation,
            NetworkViolation,
        )

        logger = SecurityEventLogger(plugin_id="p1")
        logger.log_violation(ImportViolation("os", plugin_id="p1"))
        logger.log_violation(ImportViolation("sys", plugin_id="p1"))
        logger.log_violation(NetworkViolation("evil.com", plugin_id="p1"))

        report = ViolationReport.from_events(logger.get_events(), plugin_id="p1")
        assert report.total_violations == 3
        assert report.by_category.get("import") == 2
        assert report.by_category.get("network") == 1

    def test_to_dict(self) -> None:
        report = ViolationReport(plugin_id="test")
        d = report.to_dict()
        assert d["plugin_id"] == "test"
        assert "total_violations" in d
        assert "by_category" in d
        assert "by_layer" in d

    def test_to_json(self) -> None:
        import json

        report = ViolationReport(plugin_id="test")
        j = report.to_json()
        parsed = json.loads(j)
        assert parsed["plugin_id"] == "test"

    def test_summary(self) -> None:
        from engine.plugins.sandbox.core.violation import ImportViolation

        logger = SecurityEventLogger()
        logger.log_violation(ImportViolation("os"))
        report = ViolationReport.from_events(logger.get_events())
        summary = report.summary()
        assert "Total violations: 1" in summary
        assert "import: 1" in summary


# ─── Plugin Signing Integration ──────────────────────────────────────


class TestPluginSigningIntegration:
    def test_verify_integrity_no_hash_passes(self) -> None:
        from engine.plugins.registry import PluginRegistry

        registry = PluginRegistry(use_sandbox=True)
        entry = {"manifest": {}, "module_path": "/nonexistent.py"}
        assert registry._verify_integrity("test", entry) is True

    def test_verify_integrity_wrong_hash_fails(self, tmp_path: Any) -> None:
        from engine.plugins.registry import PluginRegistry

        strategy_file = tmp_path / "strategy.py"
        strategy_file.write_text("# test")
        registry = PluginRegistry(use_sandbox=True)
        entry = {
            "manifest": {"content_hash": "wrong_hash"},
            "module_path": str(strategy_file),
        }
        assert registry._verify_integrity("test", entry) is False

    def test_verify_integrity_correct_hash_passes(self, tmp_path: Any) -> None:
        from engine.plugins.plugin_signing import PluginSigner
        from engine.plugins.registry import PluginRegistry

        strategy_file = tmp_path / "strategy.py"
        strategy_file.write_text("# test")
        correct_hash = PluginSigner.compute_hash(str(strategy_file))
        registry = PluginRegistry(use_sandbox=True)
        entry = {
            "manifest": {"content_hash": correct_hash},
            "module_path": str(strategy_file),
        }
        assert registry._verify_integrity("test", entry) is True


# ─── Context Thread Safety ───────────────────────────────────────────


class TestContextThreadSafety:
    def test_context_cleanup_after_activate(self) -> None:
        policy = SandboxPolicy(
            plugin_id="thread_test",
            import_policy=ImportPolicy(blocked_modules={"os"}),
        )
        ctx = SandboxContext(policy)
        work_dir = ctx.work_dir
        ctx.activate()
        assert ctx.is_active is True
        ctx.cleanup()
        assert ctx.is_active is False
        assert not os.path.isdir(work_dir)

    def test_context_double_cleanup_safe(self) -> None:
        policy = SandboxPolicy(plugin_id="double_cleanup")
        ctx = SandboxContext(policy)
        ctx.activate()
        ctx.cleanup()
        ctx.cleanup()
        assert ctx.is_active is False
