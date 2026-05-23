"""
Tests for the recursion guard and test-infrastructure whitelist in the
restricted import system.

Covers:
  - Recursion guard prevents re-entrant _restricted_import calls
  - Infrastructure allowlist whitelists pytest/coverage/hypothesis modules
  - Thread-local isolation of the recursion guard
  - Both RestrictedImporter implementations:
      engine.plugins.restricted_importer.RestrictedImporter
      engine.plugins.sandbox.layers.import_restriction.RestrictedImporter
  - Edge cases: blocked modules still blocked, install/uninstall idempotency
"""

from __future__ import annotations

import builtins
import sys
import threading
from unittest.mock import patch

import pytest

from engine.plugins.restricted_importer import (
    _INFRA_ALLOWLIST,
    BLOCKED_MODULES,
)
from engine.plugins.restricted_importer import (
    RestrictedImporter as BaseRestrictedImporter,
)
from engine.plugins.sandbox.layers.import_restriction import (
    _INFRA_ALLOWLIST as LAYER_INFRA_ALLOWLIST,
)
from engine.plugins.sandbox.layers.import_restriction import (
    RestrictedImporter as LayerRestrictedImporter,
)
from engine.plugins.sandbox.layers.import_restriction import (
    _local as layer_local,
)


def _reset_thread_local() -> None:
    from engine.plugins.restricted_importer import _local as base_local

    for tl in (base_local, layer_local):
        if hasattr(tl, "in_hook"):
            del tl.in_hook


@pytest.fixture(autouse=True)
def _clean_state():
    _reset_thread_local()
    yield
    _reset_thread_local()


# ── Recursion guard: base RestrictedImporter ──────────────────────────


class TestBaseRecursionGuard:
    def test_is_in_hook_defaults_false(self) -> None:
        importer = BaseRestrictedImporter()
        assert importer._is_in_hook() is False

    def test_restricted_import_short_circuits_on_reentry(self) -> None:
        importer = BaseRestrictedImporter()
        importer.install()
        try:
            call_count = 0
            original = importer._original_import

            def counting_import(name, *a, **kw):
                nonlocal call_count
                call_count += 1
                return original(name, *a, **kw)

            with patch.object(importer, "_original_import", counting_import), \
                 patch.object(importer, "_is_in_hook", return_value=True):
                result = importer._restricted_import("json")
                assert result is not None
                assert call_count == 1
        finally:
            importer.uninstall()

    def test_find_spec_returns_none_on_reentry(self) -> None:
        importer = BaseRestrictedImporter()
        with patch.object(importer, "_is_in_hook", return_value=True):
            result = importer.find_spec("os")
            assert result is None

    def test_thread_local_flag_is_set_during_hook(self) -> None:
        from engine.plugins.restricted_importer import _local

        importer = BaseRestrictedImporter()
        importer.install()
        try:
            assert not getattr(_local, "in_hook", False)
            importer._restricted_import("json")
            assert not getattr(_local, "in_hook", False)
        finally:
            importer.uninstall()

    def test_thread_local_flag_cleared_on_exception(self) -> None:
        from engine.plugins.restricted_importer import _local

        importer = BaseRestrictedImporter()
        importer.install()
        try:
            with pytest.raises(ImportError, match="blocked"):
                importer._restricted_import("os")
            assert not getattr(_local, "in_hook", False)
        finally:
            importer.uninstall()

    def test_find_spec_flag_cleared_on_exception(self) -> None:
        from engine.plugins.restricted_importer import _local

        importer = BaseRestrictedImporter()
        with pytest.raises(ImportError, match="blocked"):
            importer.find_spec("os")
        assert not getattr(_local, "in_hook", False)


class TestBaseRecursionGuardThreading:
    def test_guard_is_thread_local(self) -> None:
        from engine.plugins.restricted_importer import _local

        results = {}
        barrier = threading.Barrier(2)

        def thread_fn(flag_name: str):
            barrier.wait()
            if flag_name == "set":
                _local.in_hook = True
            results[flag_name] = getattr(_local, "in_hook", False)
            barrier.wait()

        t1 = threading.Thread(target=thread_fn, args=("set",))
        t2 = threading.Thread(target=thread_fn, args=("unset",))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)
        assert results["set"] is True
        assert results["unset"] is False

    def test_concurrent_imports_no_deadlock(self) -> None:
        importer = BaseRestrictedImporter()
        importer.install()
        errors = []

        def do_import():
            try:
                __import__("json")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=do_import) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert not errors
        importer.uninstall()


# ── Infrastructure allowlist: base RestrictedImporter ─────────────────


class TestBaseInfraAllowlist:
    @pytest.mark.parametrize("module_name", sorted(_INFRA_ALLOWLIST))
    def test_find_spec_allows_infrastructure_modules(self, module_name: str) -> None:
        importer = BaseRestrictedImporter()
        result = importer.find_spec(module_name)
        assert result is None

    @pytest.mark.parametrize("module_name", sorted(_INFRA_ALLOWLIST))
    def test_restricted_import_allows_infrastructure_modules(
        self, module_name: str
    ) -> None:
        importer = BaseRestrictedImporter()
        importer.install()
        try:
            try:
                result = importer._restricted_import(module_name)
                assert result is not None or module_name in sys.modules
            except ModuleNotFoundError:
                pytest.skip(f"{module_name} not installed")
        finally:
            importer.uninstall()

    def test_pytest_submodule_allowed(self) -> None:
        importer = BaseRestrictedImporter()
        result = importer.find_spec("_pytest.config")
        assert result is None

    def test_coverage_submodule_allowed(self) -> None:
        importer = BaseRestrictedImporter()
        result = importer.find_spec("coverage.collect")
        assert result is None

    def test_blocked_module_still_blocked_despite_allowlist(self) -> None:
        importer = BaseRestrictedImporter()
        with pytest.raises(ImportError, match="blocked"):
            importer.find_spec("os")

    def test_blocked_module_not_in_allowlist(self) -> None:
        for blocked in BLOCKED_MODULES:
            assert blocked not in _INFRA_ALLOWLIST

    def test_allowlist_does_not_contain_dangerous_modules(self) -> None:
        dangerous = {"os", "subprocess", "sys", "ctypes", "pickle", "io"}
        assert _INFRA_ALLOWLIST.isdisjoint(dangerous)


# ── Recursion guard: layer RestrictedImporter ─────────────────────────


class TestLayerRecursionGuard:
    def test_is_in_hook_defaults_false(self) -> None:
        importer = LayerRestrictedImporter()
        assert importer._is_in_hook() is False

    def test_restricted_import_short_circuits_on_reentry(self) -> None:
        importer = LayerRestrictedImporter()
        call_count = 0
        original = importer._original_import

        def counting_import(name, *a, **kw):
            nonlocal call_count
            call_count += 1
            return original(name, *a, **kw)

        with patch.object(importer, "_original_import", counting_import), \
             patch.object(importer, "_is_in_hook", return_value=True):
            result = importer._restricted_import("json")
            assert result is not None
            assert call_count == 1

    def test_find_spec_returns_none_on_reentry(self) -> None:
        importer = LayerRestrictedImporter()
        with patch.object(importer, "_is_in_hook", return_value=True):
            result = importer.find_spec("os")
            assert result is None

    def test_violation_not_logged_on_reentry(self) -> None:
        importer = LayerRestrictedImporter(plugin_id="test_plugin")
        with patch.object(importer, "_is_in_hook", return_value=True):
            importer.find_spec("os")
        assert importer.get_violations() == []

    def test_thread_local_flag_cleared_after_restricted_import(self) -> None:
        importer = LayerRestrictedImporter()
        importer.install()
        try:
            importer._restricted_import("json")
            assert not getattr(layer_local, "in_hook", False)
        finally:
            importer.uninstall()

    def test_thread_local_flag_cleared_after_exception(self) -> None:
        importer = LayerRestrictedImporter()
        importer.install()
        try:
            with pytest.raises(ImportError, match="blocked"):
                importer._restricted_import("os")
            assert not getattr(layer_local, "in_hook", False)
        finally:
            importer.uninstall()

    def test_find_spec_flag_cleared_after_exception(self) -> None:
        importer = LayerRestrictedImporter()
        with pytest.raises(ImportError, match="blocked"):
            importer.find_spec("os")
        assert not getattr(layer_local, "in_hook", False)


# ── Infrastructure allowlist: layer RestrictedImporter ────────────────


class TestLayerInfraAllowlist:
    @pytest.mark.parametrize("module_name", sorted(LAYER_INFRA_ALLOWLIST))
    def test_find_spec_allows_infrastructure_modules(self, module_name: str) -> None:
        importer = LayerRestrictedImporter()
        result = importer.find_spec(module_name)
        assert result is None
        assert importer.get_violations() == []

    @pytest.mark.parametrize("module_name", sorted(LAYER_INFRA_ALLOWLIST))
    def test_restricted_import_allows_infrastructure_modules(
        self, module_name: str
    ) -> None:
        importer = LayerRestrictedImporter()
        importer.install()
        try:
            try:
                result = importer._restricted_import(module_name)
                assert result is not None or module_name in sys.modules
                assert importer.get_violations() == []
            except ModuleNotFoundError:
                pytest.skip(f"{module_name} not installed")
        finally:
            importer.uninstall()

    def test_blocked_module_still_blocked(self) -> None:
        importer = LayerRestrictedImporter(plugin_id="p1")
        with pytest.raises(ImportError, match="blocked"):
            importer.find_spec("os")
        assert len(importer.get_violations()) == 1
        assert importer.get_violations()[0].module_name == "os"
        assert importer.get_violations()[0].plugin_id == "p1"

    def test_allowed_list_still_enforced(self) -> None:
        importer = LayerRestrictedImporter(
            allowed={"json", "math"},
            plugin_id="p2",
        )
        result = importer.find_spec("json")
        assert result is None
        with pytest.raises(ImportError, match="blocked"):
            importer.find_spec("collections")
        assert len(importer.get_violations()) == 1

    def test_infra_allowlist_overrides_allowed_list(self) -> None:
        importer = LayerRestrictedImporter(
            allowed={"json"},
            plugin_id="p3",
        )
        result = importer.find_spec("pytest")
        assert result is None
        assert importer.get_violations() == []

    def test_violation_logging_works(self) -> None:
        importer = LayerRestrictedImporter(plugin_id="p4")
        with pytest.raises(ImportError):
            importer.find_spec("subprocess")
        with pytest.raises(ImportError):
            importer.find_spec("pickle")
        violations = importer.get_violations()
        assert len(violations) == 2
        assert violations[0].module_name == "subprocess"
        assert violations[1].module_name == "pickle"
        importer.clear_violations()
        assert importer.get_violations() == []


# ── Integration: install/uninstall with guard ─────────────────────────


class TestInstallUninstallWithGuard:
    def test_base_install_uninstall_cycle(self) -> None:
        original = builtins.__import__
        importer = BaseRestrictedImporter()
        importer.install()
        assert importer in sys.meta_path
        assert importer._installed is True
        importer.uninstall()
        assert importer not in sys.meta_path
        assert importer._installed is False
        assert builtins.__import__ is original

    def test_layer_install_uninstall_cycle(self) -> None:
        original = builtins.__import__
        importer = LayerRestrictedImporter()
        importer.install()
        assert importer in sys.meta_path
        assert importer._installed is True
        importer.uninstall()
        assert importer not in sys.meta_path
        assert importer._installed is False
        assert builtins.__import__ is original

    def test_base_blocked_import_raises(self) -> None:
        importer = BaseRestrictedImporter()
        importer.install()
        try:
            with pytest.raises(ImportError, match="blocked"):
                builtins.__import__("os")
        finally:
            importer.uninstall()

    def test_layer_blocked_import_raises(self) -> None:
        importer = LayerRestrictedImporter()
        importer.install()
        try:
            with pytest.raises(ImportError, match="blocked"):
                builtins.__import__("os")
        finally:
            importer.uninstall()

    def test_base_safe_import_works(self) -> None:
        importer = BaseRestrictedImporter()
        importer.install()
        try:
            mod = builtins.__import__("json")
            assert mod is not None
        finally:
            importer.uninstall()

    def test_layer_safe_import_works(self) -> None:
        importer = LayerRestrictedImporter()
        importer.install()
        try:
            mod = builtins.__import__("json")
            assert mod is not None
        finally:
            importer.uninstall()

    def test_original_import_restored_after_blocked_import(self) -> None:
        original = builtins.__import__
        importer = BaseRestrictedImporter()
        importer.install()
        try:
            with pytest.raises(ImportError):
                builtins.__import__("os")
        finally:
            importer.uninstall()
        assert builtins.__import__ is original

    def test_nested_importers_uninstall_cleanly(self) -> None:
        original = builtins.__import__
        base = BaseRestrictedImporter()
        layer = LayerRestrictedImporter()
        base.install()
        layer.install()
        layer.uninstall()
        base.uninstall()
        assert builtins.__import__ is original


# ── Edge cases ────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_relative_import_passes_through(self) -> None:
        importer = BaseRestrictedImporter()
        importer.install()
        try:
            result = importer._restricted_import(
                "json", {}, {}, (), 0
            )
            assert result is not None
        finally:
            importer.uninstall()

    def test_empty_fromlist(self) -> None:
        importer = BaseRestrictedImporter()
        importer.install()
        try:
            result = importer._restricted_import("json", None, None, ())
            assert result is not None
        finally:
            importer.uninstall()

    def test_nonempty_fromlist(self) -> None:
        importer = BaseRestrictedImporter()
        importer.install()
        try:
            result = importer._restricted_import("json", None, None, ("loads",))
            assert result is not None
        finally:
            importer.uninstall()

    def test_layer_with_allowed_none(self) -> None:
        importer = LayerRestrictedImporter(allowed=None)
        result = importer.find_spec("json")
        assert result is None

    def test_layer_with_empty_blocked(self) -> None:
        importer = LayerRestrictedImporter(blocked={"custom_only"})
        result = importer.find_spec("os")
        assert result is None
        with pytest.raises(ImportError, match="custom_only"):
            importer.find_spec("custom_only")

    def test_both_allowlists_match(self) -> None:
        assert _INFRA_ALLOWLIST == LAYER_INFRA_ALLOWLIST

    def test_blocked_submodule_blocked(self) -> None:
        importer = BaseRestrictedImporter()
        with pytest.raises(ImportError, match=r"os\.path"):
            importer.find_spec("os.path")

    def test_infra_submodule_allowed(self) -> None:
        importer = BaseRestrictedImporter()
        result = importer.find_spec("_pytest._code")
        assert result is None

    def test_guard_flag_isolation_between_calls(self) -> None:
        importer = BaseRestrictedImporter()
        importer.install()
        try:
            __import__("json")
            __import__("math")
            __import__("json")
        finally:
            importer.uninstall()

    def test_double_install_noop(self) -> None:
        importer = BaseRestrictedImporter()
        importer.install()
        idx = sys.meta_path.index(importer)
        importer.install()
        assert sys.meta_path.index(importer) == idx
        importer.uninstall()

    def test_double_uninstall_safe(self) -> None:
        importer = BaseRestrictedImporter()
        importer.install()
        importer.uninstall()
        importer.uninstall()
