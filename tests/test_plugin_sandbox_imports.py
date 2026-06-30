"""Unit tests for :class:`engine.plugins.sandbox.imports.ImportRestrictor`.

Layer 1 of the plugin sandbox: an allowlist/blocklist import guard installed
on ``sys.meta_path`` (plus a ``builtins.__import__`` shim for cached modules)
that can be activated as a context manager.

These tests deliberately keep scope tight — they exercise only the import
restriction layer (no filesystem or network sandboxing).
"""

from __future__ import annotations

import builtins
import sys

import pytest

from engine.plugins.allowlist import DENYLIST_MODULES, FROZEN_ALLOWED_MODULES
from engine.plugins.sandbox.imports import ImportRestrictor


@pytest.fixture(autouse=True)
def _restore_import_state() -> None:
    """Snapshot/restore ``sys.meta_path`` and ``builtins.__import__``.

    A defensive guard: even if a test fails before ``uninstall`` runs, the
    session-wide import machinery is restored so no other test is poisoned.
    The context manager's ``__exit__`` already restores on exception; this
    fixture is belt-and-braces.
    """
    saved_meta = list(sys.meta_path)
    saved_import = builtins.__import__
    yield
    sys.meta_path[:] = saved_meta
    builtins.__import__ = saved_import


class TestImportRestrictorDecisions:
    def test_find_spec_blocks_blocked_module(self) -> None:
        restrictor = ImportRestrictor()
        with pytest.raises(ImportError, match="blocked"):
            restrictor.find_spec("os")

    def test_find_spec_allows_allowed_module(self) -> None:
        restrictor = ImportRestrictor()
        assert restrictor.find_spec("json") is None

    @pytest.mark.parametrize("name", ["os", "subprocess", "socket"])
    def test_is_allowed_rejects_known_dangerous(self, name: str) -> None:
        assert ImportRestrictor().is_allowed(name) is False

    def test_submodule_inherits_root_decision(self) -> None:
        restrictor = ImportRestrictor()
        # Root "os" is blocked → submodule "os.path" inherits the block.
        with pytest.raises(ImportError):
            restrictor.find_spec("os.path")
        # Root "json" is allowed → submodule resolves (finder returns None).
        assert restrictor.find_spec("json.decoder") is None


class TestContextManagerEnforcement:
    def test_allowed_import_passes_while_active(self) -> None:
        with ImportRestrictor():
            mod = builtins.__import__("json")
            assert mod is sys.modules["json"]

    @pytest.mark.parametrize("name", ["os", "subprocess", "socket"])
    def test_blocked_import_raises_while_active(self, name: str) -> None:
        with ImportRestrictor(), pytest.raises(ImportError, match="blocked"):
            builtins.__import__(name)

    def test_context_manager_restores_state_on_exit(self) -> None:
        original = builtins.__import__
        restrictor = ImportRestrictor()
        with restrictor:
            assert restrictor in sys.meta_path
            with pytest.raises(ImportError):
                builtins.__import__("os")
        # Restrictions lifted: import hook gone, real __import__ restored,
        # and the previously-blocked module imports normally again.
        assert restrictor not in sys.meta_path
        assert builtins.__import__ is original
        assert builtins.__import__("os") is sys.modules["os"]

    def test_context_manager_restores_state_on_exception(self) -> None:
        original = builtins.__import__
        restrictor = ImportRestrictor()
        with pytest.raises(RuntimeError, match="boom"), restrictor:
            raise RuntimeError("boom")
        assert restrictor not in sys.meta_path
        assert builtins.__import__ is original

    def test_nested_context_managers(self) -> None:
        original = builtins.__import__
        with ImportRestrictor() as outer:
            assert outer in sys.meta_path
            with ImportRestrictor() as inner:
                assert inner in sys.meta_path
                assert outer in sys.meta_path
                with pytest.raises(ImportError):
                    builtins.__import__("os")
            # Inner exited — outer restrictions still in effect.
            assert inner not in sys.meta_path
            assert outer in sys.meta_path
            with pytest.raises(ImportError):
                builtins.__import__("os")
        # Both exited — fully restored.
        assert outer not in sys.meta_path
        assert inner not in sys.meta_path
        assert builtins.__import__ is original


class TestCustomAllowAndBlocklists:
    def test_defaults_match_production_lists(self) -> None:
        restrictor = ImportRestrictor()
        assert restrictor.allowed == FROZEN_ALLOWED_MODULES
        assert restrictor.blocked == DENYLIST_MODULES

    def test_custom_allowlist_rejects_unlisted(self) -> None:
        # Only "json" is permitted; everything else (including "math") blocked.
        with ImportRestrictor(allowed={"json"}):
            assert builtins.__import__("json") is sys.modules["json"]
            with pytest.raises(ImportError):
                builtins.__import__("math")

    def test_blocklist_overrides_allowlist(self) -> None:
        # "math" is allowed AND explicitly blocked → blocklist wins.
        with (
            ImportRestrictor(allowed={"math"}, blocked={"math"}),
            pytest.raises(ImportError, match="math"),
        ):
            builtins.__import__("math")

    def test_empty_allowlist_blocks_everything(self) -> None:
        with ImportRestrictor(allowed=set()):
            with pytest.raises(ImportError):
                builtins.__import__("json")
            with pytest.raises(ImportError):
                builtins.__import__("math")


class TestInstallLifecycle:
    def test_install_returns_self(self) -> None:
        restrictor = ImportRestrictor()
        assert restrictor.install() is restrictor
        assert restrictor._installed is True
        restrictor.uninstall()

    def test_double_install_is_idempotent(self) -> None:
        restrictor = ImportRestrictor()
        restrictor.install()
        snapshot = list(sys.meta_path)
        restrictor.install()  # no-op: must not insert a duplicate
        assert sys.meta_path == snapshot
        restrictor.uninstall()

    def test_double_uninstall_is_safe(self) -> None:
        restrictor = ImportRestrictor()
        restrictor.install()
        restrictor.uninstall()
        # Second uninstall must not raise and must leave state clean.
        restrictor.uninstall()
        assert restrictor not in sys.meta_path
        assert restrictor._installed is False
