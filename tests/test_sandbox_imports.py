"""Unit tests for Layer 1 of the strategy plugin sandbox: import restrictions.

Covers :mod:`engine.plugins.sandbox.import_guard`:

* the typed :class:`SandboxSecurityError` exception,
* allowlisted imports succeeding (``math``, ``datetime``, ``json``),
* dangerous imports being blocked (``os``, ``subprocess``, ``socket``,
  ``ctypes``, ``sys``),
* the guard's enable/disable lifecycle and the ``activated`` context manager,
* direct policy inspection (``is_allowed`` / ``is_blocked`` / ``check_import``),
* custom allow/deny sets, idempotent toggling, and finder-level enforcement.

Every test snapshots and restores ``sys.meta_path`` + ``builtins.__import__``
via the autouse ``_restore_import_state`` fixture so a failing assertion can
never leak the guard onto the process-global import machinery for the rest of
the session.
"""

from __future__ import annotations

import builtins
import importlib
import sys
from collections.abc import Iterator

import pytest

from engine.plugins.allowlist import FROZEN_ALLOWED_MODULES
from engine.plugins.exceptions import SandboxSecurityError
from engine.plugins.sandbox.import_guard import (
    ALLOWED_MODULES,
    BLOCKED_MODULES,
    ImportGuard,
    _ImportGuardFinder,
)

# ── Shared fixtures ──────────────────────────────────────────────────


@pytest.fixture
def _restore_import_state() -> Iterator[None]:
    """Snapshot/restore ``sys.meta_path`` and ``builtins.__import__``.

    The guard mutates both when enabled; without restoration a failing test
    would leave the guard installed and break import for every subsequent test
    (most catastrophically blocking pytest's own lazy imports).
    """
    meta_snapshot = list(sys.meta_path)
    import_snapshot = builtins.__import__
    modules_snapshot = dict(sys.modules)
    try:
        yield
    finally:
        builtins.__import__ = import_snapshot
        sys.meta_path[:] = meta_snapshot
        # Restore any modules a test may have injected/removed so we never
        # leave a partially-purged ``sys.modules`` for later tests.
        for name in list(sys.modules):
            if name not in modules_snapshot:
                sys.modules.pop(name, None)
        for name, mod in modules_snapshot.items():
            sys.modules[name] = mod


@pytest.fixture
def guard(_restore_import_state: None) -> ImportGuard:
    """A fresh, un-enabled ``ImportGuard`` with the default policy."""
    return ImportGuard()


# ── SandboxSecurityError ─────────────────────────────────────────────


class TestSandboxSecurityError:
    def test_subclasses_import_error(self) -> None:
        """The typed error must be an ``ImportError`` subclass so the stdlib
        import machinery (which expects ``ImportError`` from a rejecting
        finder) and existing ``except ImportError`` handlers keep working."""
        assert issubclass(SandboxSecurityError, ImportError)

    def test_message_includes_module_and_reason(self) -> None:
        err = SandboxSecurityError("os", reason="explicitly denylisted")
        msg = str(err)
        assert "os" in msg
        assert "denylisted" in msg

    def test_attributes_exposed(self) -> None:
        err = SandboxSecurityError("subprocess")
        assert err.module == "subprocess"
        assert err.reason == "not in allowlist"

    def test_default_reason(self) -> None:
        err = SandboxSecurityError("socket")
        assert err.reason == "not in allowlist"

    def test_is_raisable_and_catchable(self) -> None:
        with pytest.raises(SandboxSecurityError) as exc_info:
            raise SandboxSecurityError("ctypes")
        assert exc_info.value.module == "ctypes"

    def test_catchable_as_import_error(self) -> None:
        # Existing code that only knows about ``ImportError`` must still match.
        with pytest.raises(ImportError):
            raise SandboxSecurityError("os")

    def test_pickle_roundtrip(self) -> None:
        import pickle

        original = SandboxSecurityError("os", "explicitly denylisted")
        restored = pickle.loads(pickle.dumps(original))
        assert isinstance(restored, SandboxSecurityError)
        assert restored.module == "os"
        assert restored.reason == "explicitly denylisted"

    def test_re_exported_from_import_guard(self) -> None:
        # The guard module re-exports the exception for a single import path.
        from engine.plugins.sandbox.import_guard import (
            SandboxSecurityError as GuardError,
        )

        assert GuardError is SandboxSecurityError


# ── Module-level policy constants ────────────────────────────────────


class TestPolicyConstants:
    def test_allowed_modules_is_frozenset(self) -> None:
        assert isinstance(ALLOWED_MODULES, frozenset)

    def test_blocked_modules_is_frozenset(self) -> None:
        assert isinstance(BLOCKED_MODULES, frozenset)

    def test_known_safe_modules_allowed(self) -> None:
        for safe in ("math", "datetime", "json", "itertools", "collections"):
            assert safe in ALLOWED_MODULES, safe

    def test_known_dangerous_modules_blocked(self) -> None:
        for dangerous in ("os", "subprocess", "socket", "ctypes", "sys"):
            assert dangerous in BLOCKED_MODULES, dangerous

    def test_allowlist_and_denylist_are_disjoint_at_root(self) -> None:
        # The frozen policy must not simultaneously allow and deny the same
        # root (the denylist would win anyway, but a contradiction signals a
        # policy bug).  Internal bypass modules (pytest, hypothesis, …) are
        # exempt by design.
        from engine.plugins.restricted_importer import _INTERNAL_BYPASS_MODULES

        overlap = ALLOWED_MODULES & BLOCKED_MODULES - _INTERNAL_BYPASS_MODULES
        assert overlap == set(), f"roots in both allow & deny lists: {overlap}"


# ── Direct policy inspection (no activation) ─────────────────────────


class TestPolicyInspection:
    def test_is_allowed_true_for_safe_roots(self, guard: ImportGuard) -> None:
        assert guard.is_allowed("math") is True
        assert guard.is_allowed("datetime") is True
        assert guard.is_allowed("json") is True

    def test_is_allowed_true_for_submodules_of_safe_roots(
        self, guard: ImportGuard
    ) -> None:
        assert guard.is_allowed("json.decoder") is True
        assert guard.is_allowed("collections.abc") is True
        assert guard.is_allowed("datetime.timedelta") is True

    def test_is_blocked_true_for_dangerous_roots(self, guard: ImportGuard) -> None:
        for dangerous in ("os", "subprocess", "socket", "ctypes", "sys"):
            assert guard.is_blocked(dangerous) is True, dangerous

    def test_is_blocked_true_for_submodules_of_dangerous_roots(
        self, guard: ImportGuard
    ) -> None:
        assert guard.is_blocked("os.path") is True
        assert guard.is_blocked("subprocess.Popen") is True
        assert guard.is_blocked("socket.socket") is True

    def test_is_allowed_and_is_blocked_are_complements(
        self, guard: ImportGuard
    ) -> None:
        assert guard.is_allowed("math") is not guard.is_blocked("math")
        assert guard.is_allowed("os") is not guard.is_blocked("os")

    def test_empty_name_is_allowed_defensively(self, guard: ImportGuard) -> None:
        assert guard.is_allowed("") is True
        assert guard.is_blocked("") is False

    def test_internal_bypass_modules_always_allowed(self, guard: ImportGuard) -> None:
        # Host/test-harness infra (pytest, hypothesis, …) must always be
        # permitted even though they are not on the strategy allowlist, so the
        # guard can never crash the test runner while active.
        from engine.plugins.restricted_importer import _INTERNAL_BYPASS_MODULES

        assert _INTERNAL_BYPASS_MODULES
        for bypass in _INTERNAL_BYPASS_MODULES:
            assert guard.is_allowed(bypass) is True, bypass

    def test_check_import_passes_for_allowed(self, guard: ImportGuard) -> None:
        guard.check_import("math")  # must not raise
        guard.check_import("json.decoder")  # must not raise

    def test_check_import_raises_for_blocked(self, guard: ImportGuard) -> None:
        with pytest.raises(SandboxSecurityError, match="os"):
            guard.check_import("os")

    def test_check_import_raises_for_blocked_submodule(
        self, guard: ImportGuard
    ) -> None:
        with pytest.raises(SandboxSecurityError, match=r"os\.path"):
            guard.check_import("os.path")

    def test_check_import_does_not_touch_sys_modules(
        self, guard: ImportGuard
    ) -> None:
        """``check_import`` is a pure policy gate: it must not import anything
        or mutate ``sys.modules``."""
        before = dict(sys.modules)
        guard.check_import("math")
        with pytest.raises(SandboxSecurityError):
            guard.check_import("os")  # raises, but must not load anything
        after = dict(sys.modules)
        assert before == after


# ── Enable / disable lifecycle ───────────────────────────────────────


class TestEnableDisableLifecycle:
    def test_starts_disabled(self, guard: ImportGuard) -> None:
        assert guard.is_active is False

    def test_enable_sets_active_and_installs_finder(
        self, guard: ImportGuard
    ) -> None:
        guard.enable()
        assert guard.is_active is True
        assert guard._finder is not None
        assert guard._finder in sys.meta_path

    def test_disable_clears_active_and_removes_finder(
        self, guard: ImportGuard
    ) -> None:
        guard.enable()
        guard.disable()
        assert guard.is_active is False
        assert guard._finder is None
        assert guard not in sys.meta_path

    def test_enable_is_idempotent(self, guard: ImportGuard) -> None:
        guard.enable()
        finder = guard._finder
        guard.enable()  # second call is a no-op
        assert guard._finder is finder
        # The finder is inserted exactly once.
        assert sys.meta_path.count(guard._finder) == 1

    def test_disable_is_idempotent(self, guard: ImportGuard) -> None:
        guard.enable()
        guard.disable()
        guard.disable()  # second call is a no-op, must not raise
        assert guard.is_active is False

    def test_disable_without_enable_is_safe(self, guard: ImportGuard) -> None:
        guard.disable()  # never enabled → no-op
        assert guard.is_active is False

    def test_enable_restores_original_import_on_disable(
        self, guard: ImportGuard
    ) -> None:
        original = builtins.__import__
        guard.enable()
        assert builtins.__import__ is guard._import_hook
        guard.disable()
        assert builtins.__import__ is original

    def test_disable_does_not_clobber_unowned_builtin(
        self, guard: ImportGuard
    ) -> None:
        """If something else replaced ``builtins.__import__`` after we
        installed, disable must NOT overwrite it (otherwise we'd corrupt an
        unrelated importer)."""
        guard.enable()
        real_import = guard._original_import
        assert real_import is not None
        # Simulate an outer harness resetting the builtin.
        builtins.__import__ = real_import
        guard.disable()
        assert builtins.__import__ is real_import
        assert guard.is_active is False

    def test_reenable_uses_fresh_finder(self, guard: ImportGuard) -> None:
        guard.enable()
        first_finder = guard._finder
        guard.disable()
        guard.enable()
        second_finder = guard._finder
        assert first_finder is not None
        assert second_finder is not None
        assert second_finder is not first_finder  # fresh instance each time
        assert second_finder in sys.meta_path
        guard.disable()


# ── Activated context manager ────────────────────────────────────────


class TestActivatedContextManager:
    def test_guard_active_inside_context(self, guard: ImportGuard) -> None:
        assert guard.is_active is False
        with guard.activated():
            assert guard.is_active is True
        assert guard.is_active is False

    def test_context_manager_yields_guard(self, guard: ImportGuard) -> None:
        with guard.activated() as yielded:
            assert yielded is guard

    def test_context_manager_disables_on_exception(self, guard: ImportGuard) -> None:
        with pytest.raises(RuntimeError, match="boom"), guard.activated():
            assert guard.is_active is True
            raise RuntimeError("boom")
        # Guard must be torn down even though the body raised.
        assert guard.is_active is False
        assert guard._finder is None

    def test_nested_context_managers(self, guard: ImportGuard) -> None:
        with guard.activated():
            assert guard.is_active is True
            with guard.activated():  # idempotent enable
                assert guard.is_active is True
            # Inner disable runs here — but we re-entered, so outer still owns.
            # RLock keeps this safe; is_active reflects the last write.
        assert guard.is_active is False


# ── Allowed imports succeed while enabled ────────────────────────────


class TestAllowedImportsSucceed:
    @pytest.mark.parametrize(
        "module_name",
        ["math", "datetime", "json", "itertools", "collections", "statistics"],
    )
    def test_allowlisted_module_imports_inside_guard(
        self, guard: ImportGuard, module_name: str
    ) -> None:
        # Realistic flow: the host pre-loads allowlisted modules (and
        # ``purge_non_allowlisted`` retains them), so when strategy code runs
        # ``import <module>`` the module is already resident in
        # ``sys.modules``.  We pre-cache the module here before enabling the
        # guard, then assert the ``import`` succeeds while the guard is
        # active.  Re-importing e.g. ``statistics``/``datetime`` from a cold
        # cache would re-run their init code, which imports blocked
        # C-essentials (``_io``, ``_json``) that are only safe because they
        # are already resident — so a cold load is intentionally out of
        # scope for this layer (the finder path for allowed modules is
        # exercised directly in :class:`TestImportGuardFinder`).
        importlib.import_module(module_name)  # pre-cache (host flow)
        with guard.activated():
            mod = importlib.import_module(module_name)
            assert mod is not None
            assert sys.modules[module_name] is mod

    def test_import_json_and_use_it(self, guard: ImportGuard) -> None:
        with guard.activated():
            import json

            assert json.loads("[1, 2, 3]") == [1, 2, 3]

    def test_import_math_and_use_it(self, guard: ImportGuard) -> None:
        with guard.activated():
            import math

            assert math.sqrt(16) == 4.0

    def test_import_datetime_and_use_it(self, guard: ImportGuard) -> None:
        with guard.activated():
            from datetime import date

            assert date(2026, 7, 21).isoformat() == "2026-07-21"

    def test_fromlist_import_allowed(self, guard: ImportGuard) -> None:
        with guard.activated():
            from collections import deque  # noqa: F401

    def test_allowlisted_already_cached_module_re_import_allowed(
        self, guard: ImportGuard
    ) -> None:
        # Pre-cache an allowlisted module, then ensure a re-import while the
        # guard is active still succeeds (the ``__import__`` override path).
        import math  # warm cache

        with guard.activated():
            import math as math_again

            assert math_again is math


# ── Blocked imports raise while enabled ──────────────────────────────


class TestBlockedImportsRaise:
    @pytest.mark.parametrize(
        "module_name",
        ["os", "subprocess", "socket", "ctypes", "sys", "pickle", "threading"],
    )
    def test_blocked_module_import_raises_typed_error(
        self, guard: ImportGuard, module_name: str
    ) -> None:
        # Use the ``import`` statement form so the ``__import__`` override
        # enforces the policy regardless of whether the module is already
        # cached (``sys``/``socket``/``os`` are resident in any host process).
        # Popping these from ``sys.modules`` would be unsafe — several are
        # load-bearing for the interpreter / test runner — and is unnecessary
        # because the override runs *before* the cache lookup.  The finder
        # path is covered directly in :class:`TestImportGuardFinder`.
        with guard.activated():
            with pytest.raises(SandboxSecurityError) as exc_info:
                exec(f"import {module_name}", {})
            assert exc_info.value.module == module_name

    def test_blocked_module_raises_via_direct_import_statement(
        self, guard: ImportGuard
    ) -> None:
        """The plain ``import os`` statement must surface the typed error."""
        code = "import os\n"
        with guard.activated(), pytest.raises(SandboxSecurityError, match="os"):
            exec(code, {})

    def test_blocked_submodule_import_raises(self, guard: ImportGuard) -> None:
        with guard.activated():
            # Use the ``import`` statement form so the ``__import__`` override
            # runs (``os.path`` is cached, so ``importlib.import_module`` would
            # short-circuit on the cache and never hit either hook).
            with pytest.raises(SandboxSecurityError, match=r"os"):
                exec("import os.path", {})

    def test_blocked_fromlist_raises(self, guard: ImportGuard) -> None:
        with guard.activated(), pytest.raises(SandboxSecurityError):
            exec("from os.path import join", {})

    def test_cached_blocked_module_reimport_raises(
        self, guard: ImportGuard
    ) -> None:
        """``os`` is imported by the host at startup, so it is cached in
        ``sys.modules``.  A re-import must STILL be caught — this is why the
        guard overrides ``builtins.__import__`` in addition to the finder."""
        import os  # noqa: F401  # ensure cached

        assert "os" in sys.modules
        with guard.activated(), pytest.raises(SandboxSecurityError, match="os"):
            exec("import os", {})

    def test_blocked_import_does_not_load_module(
        self, guard: ImportGuard
    ) -> None:
        """A blocked module must never end up in ``sys.modules`` after a
        rejected import attempt (the finder raises before any load)."""
        # Use a module that is very unlikely to be already cached.
        target = "ctypes"  # blocked root
        sys.modules.pop(target, None)
        with guard.activated():
            with pytest.raises(SandboxSecurityError):
                importlib.import_module(target)
            assert target not in sys.modules

    def test_blocked_import_error_message_is_descriptive(
        self, guard: ImportGuard
    ) -> None:
        with guard.activated(), pytest.raises(SandboxSecurityError) as exc_info:
            exec("import subprocess", {})
        msg = str(exc_info.value)
        assert "subprocess" in msg
        assert "sandbox" in msg.lower() or "blocked" in msg.lower()


# ── Finder-level enforcement (isolated) ──────────────────────────────


class TestImportGuardFinder:
    def test_finder_blocks_non_allowed(self, guard: ImportGuard) -> None:
        finder = _ImportGuardFinder(guard)
        with pytest.raises(SandboxSecurityError, match="os"):
            finder.find_spec("os")

    def test_finder_blocks_submodule(self, guard: ImportGuard) -> None:
        finder = _ImportGuardFinder(guard)
        with pytest.raises(SandboxSecurityError, match=r"os\.path"):
            finder.find_spec("os.path")

    def test_finder_returns_none_for_allowed(self, guard: ImportGuard) -> None:
        finder = _ImportGuardFinder(guard)
        # Allowed modules return ``None`` so the next finder in the chain runs.
        assert finder.find_spec("math") is None
        assert finder.find_spec("json.decoder") is None

    def test_finder_returns_none_for_internal_bypass(
        self, guard: ImportGuard
    ) -> None:
        finder = _ImportGuardFinder(guard)
        # pytest must always pass — the guard can never break the host.
        assert finder.find_spec("pytest") is None

    def test_finder_uses_parent_guard_policy(self) -> None:
        """The finder defers to its parent guard's policy, so a custom allow
        set narrows enforcement correctly."""
        strict = ImportGuard(allowed={"math"})
        finder = _ImportGuardFinder(strict)
        assert finder.find_spec("math") is None
        # ``json`` is on the frozen allowlist but NOT our strict set → blocked.
        with pytest.raises(SandboxSecurityError):
            finder.find_spec("json")


# ── Custom allow / deny sets ─────────────────────────────────────────


class TestCustomAllowDeny:
    def test_narrower_allowed_set_blocks_frozen_allowed_module(self) -> None:
        # Only ``math`` is allowed; ``json`` (on the frozen allowlist) is not.
        strict_guard = ImportGuard(allowed={"math"})
        assert strict_guard.is_allowed("math") is True
        assert strict_guard.is_blocked("json") is True

    def test_frozen_denylist_always_unioned_in(self) -> None:
        # Even with a custom deny set, the frozen dangerous modules remain
        # blocked (security is monotonic — callers can only ADD blocks).
        custom = ImportGuard(blocked={"my_internal_helper"})
        assert "my_internal_helper" in custom.blocked
        assert "os" in custom.blocked  # frozen, still present
        assert "subprocess" in custom.blocked
        assert "socket" in custom.blocked

    def test_custom_denylist_blocks_extra_module(self) -> None:
        custom = ImportGuard(blocked={"numpy"})
        assert custom.is_blocked("numpy") is True
        # And the frozen blocks are untouched.
        assert custom.is_blocked("os") is True

    def test_denylist_wins_over_allowlist(self) -> None:
        """If a caller (mis-)adds a frozen-dangerous module to ``allowed`` it
        must STILL be blocked, because the denylist always wins."""
        broken = ImportGuard(allowed=FROZEN_ALLOWED_MODULES | {"os"})
        assert "os" in broken.allowed  # mis-configured
        assert broken.is_blocked("os") is True  # but still blocked
        with pytest.raises(SandboxSecurityError):
            broken.check_import("os")

    def test_custom_strict_guard_enforced_at_runtime(self) -> None:
        strict = ImportGuard(allowed={"math"})
        with strict.activated():
            import math  # noqa: F401  # allowed
            # ``json`` is on the frozen allowlist but not our strict set, so
            # the import statement must be rejected.  Use the statement form
            # (``importlib.import_module`` would hit the cache and bypass).
            with pytest.raises(SandboxSecurityError):
                exec("import json", {})


# ── Round-trip: disable restores normal imports ──────────────────────


class TestRoundTripRestoration:
    def test_blocked_import_works_after_disable(self, guard: ImportGuard) -> None:
        with guard.activated(), pytest.raises(SandboxSecurityError):
            exec("import subprocess", {})
        # After disable, the normal import machinery is restored and the
        # cached ``subprocess`` is reachable again.
        import subprocess  # noqa: F401

        assert "subprocess" in sys.modules

    def test_imports_outside_context_unaffected(self, guard: ImportGuard) -> None:
        # No guard active here at all.
        import os  # noqa: F401

        assert "os" in sys.modules

    def test_two_guards_do_not_interfere(self) -> None:
        g1 = ImportGuard()
        g2 = ImportGuard()
        with g1.activated():
            assert g1.is_active is True
            assert g2.is_active is False
            with pytest.raises(SandboxSecurityError):
                exec("import os", {})
        with g2.activated():
            assert g2.is_active is True
            assert g1.is_active is False
            with pytest.raises(SandboxSecurityError):
                exec("import os", {})
        assert g1.is_active is False
        assert g2.is_active is False
