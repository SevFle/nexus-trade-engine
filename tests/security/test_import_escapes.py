from __future__ import annotations

import sys

import pytest

from engine.plugins.restricted_importer import BLOCKED_MODULES, RestrictedImporter
from engine.plugins.sandbox.layers.import_restriction import (
    RestrictedImporter as SandboxRestrictedImporter,
)


class TestDirectImportBlocking:
    def test_all_blocked_modules_via_find_spec(self) -> None:
        importer = RestrictedImporter()
        for mod in BLOCKED_MODULES:
            with pytest.raises(ImportError, match="blocked"):
                importer.find_spec(mod)

    def test_submodule_of_blocked_parent(self) -> None:
        importer = RestrictedImporter()
        with pytest.raises(ImportError, match=r"os\.path"):
            importer.find_spec("os.path")

    def test_submodule_of_blocked_http(self) -> None:
        importer = RestrictedImporter()
        with pytest.raises(ImportError, match=r"http\.client"):
            importer.find_spec("http.client")

    def test_submodule_of_blocked_urllib(self) -> None:
        importer = RestrictedImporter()
        with pytest.raises(ImportError, match=r"urllib\.request"):
            importer.find_spec("urllib.request")

    def test_safe_module_passes(self) -> None:
        importer = RestrictedImporter()
        assert importer.find_spec("json") is None
        assert importer.find_spec("math") is None
        assert importer.find_spec("datetime") is None

    def test_custom_blocked_set(self) -> None:
        importer = RestrictedImporter(blocked={"my_dangerous_mod"})
        with pytest.raises(ImportError, match="my_dangerous_mod"):
            importer.find_spec("my_dangerous_mod")
        assert importer.find_spec("json") is None


class TestImportBuiltinOverride:
    def test_builtin_import_overridden(self) -> None:
        import builtins

        importer = RestrictedImporter()
        original = builtins.__import__
        importer.install()
        try:
            assert builtins.__import__ is not original
        finally:
            importer.uninstall()
            assert builtins.__import__ is original

    def test_builtin_import_blocks_os(self) -> None:
        import builtins

        importer = RestrictedImporter()
        importer.install()
        try:
            with pytest.raises(ImportError, match="blocked"):
                builtins.__import__("os")
        finally:
            importer.uninstall()

    def test_builtin_import_blocks_fromlist(self) -> None:
        import builtins

        importer = RestrictedImporter()
        importer.install()
        try:
            with pytest.raises(ImportError, match="blocked"):
                builtins.__import__("os.path", fromlist=("join",))
        finally:
            importer.uninstall()

    def test_builtin_import_allows_safe(self) -> None:
        import builtins

        importer = RestrictedImporter()
        importer.install()
        try:
            mod = builtins.__import__("json")
            assert hasattr(mod, "dumps")
        finally:
            importer.uninstall()


class TestAllowedWhitelist:
    def test_non_allowed_module_blocked_when_whitelist_set(self) -> None:
        importer = SandboxRestrictedImporter(
            blocked=set(),
            allowed={"json", "math"},
        )
        with pytest.raises(ImportError, match="blocked"):
            importer.find_spec("collections")

    def test_allowed_module_passes(self) -> None:
        importer = SandboxRestrictedImporter(
            blocked=set(),
            allowed={"json", "math"},
        )
        assert importer.find_spec("json") is None
        assert importer.find_spec("math") is None


class TestImportViaSysModules:
    def test_sys_modules_not_available_in_sandbox(self) -> None:
        importer = RestrictedImporter()
        with pytest.raises(ImportError, match="sys"):
            importer.find_spec("sys")


class TestImportInstallationLifecycle:
    def test_install_adds_to_meta_path(self) -> None:
        importer = RestrictedImporter()
        importer.install()
        assert importer in sys.meta_path
        importer.uninstall()

    def test_uninstall_removes_from_meta_path(self) -> None:
        importer = RestrictedImporter()
        importer.install()
        importer.uninstall()
        assert importer not in sys.meta_path

    def test_double_install_idempotent(self) -> None:
        importer = RestrictedImporter()
        importer.install()
        count = sys.meta_path.count(importer)
        importer.install()
        assert sys.meta_path.count(importer) == count
        importer.uninstall()

    def test_double_uninstall_safe(self) -> None:
        importer = RestrictedImporter()
        importer.install()
        importer.uninstall()
        importer.uninstall()

    def test_violations_recorded(self) -> None:
        importer = SandboxRestrictedImporter(blocked={"os"}, plugin_id="test_plugin")
        with pytest.raises(ImportError):
            importer.find_spec("os")
        violations = importer.get_violations()
        assert len(violations) == 1
        assert violations[0].module_name == "os"
        assert violations[0].plugin_id == "test_plugin"
        importer.clear_violations()
        assert len(importer.get_violations()) == 0


class TestImportBypassAttempts:
    def test_importlib_import_module_blocked_as_module(self) -> None:
        importer = RestrictedImporter()
        with pytest.raises(ImportError, match="importlib"):
            importer.find_spec("importlib")

    def test_pkgutil_blocked(self) -> None:
        importer = RestrictedImporter()
        with pytest.raises(ImportError, match="pkgutil"):
            importer.find_spec("pkgutil")

    def test_runpy_blocked(self) -> None:
        importer = RestrictedImporter()
        with pytest.raises(ImportError, match="runpy"):
            importer.find_spec("runpy")

    def test_zipimport_blocked(self) -> None:
        importer = RestrictedImporter()
        with pytest.raises(ImportError, match="zipimport"):
            importer.find_spec("zipimport")

    def test_pickle_blocked(self) -> None:
        importer = RestrictedImporter()
        with pytest.raises(ImportError, match="pickle"):
            importer.find_spec("pickle")

    def test_marshal_blocked(self) -> None:
        importer = RestrictedImporter()
        with pytest.raises(ImportError, match="marshal"):
            importer.find_spec("marshal")

    def test_shelve_blocked(self) -> None:
        importer = RestrictedImporter()
        with pytest.raises(ImportError, match="shelve"):
            importer.find_spec("shelve")

    def test_ctypes_blocked(self) -> None:
        importer = RestrictedImporter()
        with pytest.raises(ImportError, match="ctypes"):
            importer.find_spec("ctypes")

    def test_underscore_ctypes_blocked(self) -> None:
        importer = RestrictedImporter()
        with pytest.raises(ImportError, match="_ctypes"):
            importer.find_spec("_ctypes")

    def test_multiprocessing_blocked(self) -> None:
        importer = RestrictedImporter()
        with pytest.raises(ImportError, match="multiprocessing"):
            importer.find_spec("multiprocessing")

    def test_concurrent_futures_blocked(self) -> None:
        importer = RestrictedImporter()
        with pytest.raises(ImportError, match="concurrent"):
            importer.find_spec("concurrent.futures")

    def test_inspect_blocked(self) -> None:
        importer = RestrictedImporter()
        with pytest.raises(ImportError, match="inspect"):
            importer.find_spec("inspect")

    def test_gc_blocked(self) -> None:
        importer = RestrictedImporter()
        with pytest.raises(ImportError, match="gc"):
            importer.find_spec("gc")

    def test_code_blocked(self) -> None:
        importer = RestrictedImporter()
        with pytest.raises(ImportError, match="code"):
            importer.find_spec("code")

    def test_ast_blocked(self) -> None:
        importer = RestrictedImporter()
        with pytest.raises(ImportError, match="ast"):
            importer.find_spec("ast")
