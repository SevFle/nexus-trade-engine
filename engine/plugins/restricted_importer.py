"""
Layer 1: Import restriction system for the strategy sandbox.

**Allowlist model** — only modules whose root name appears in
:data:`~engine.plugins.allowlist.FROZEN_ALLOWED_MODULES` may be imported by
strategy code.  Every other module is rejected with ``ImportError``.

This supersedes the former denylist (``BLOCKED_MODULES``) approach which was an
unwinnable arms-race: every new dangerous CPython or third-party module was an
escape vector until someone remembered to add it to the block list.

Enforcement is dual-layered:

1. ``sys.meta_path`` finder — catches imports of modules not yet loaded.
2. ``builtins.__import__`` override — catches re-imports of modules already
   cached in ``sys.modules`` (e.g. ``os``, ``sys``).

For backward compatibility ``BLOCKED_MODULES`` is still exported — it is a
frozenset of *known-dangerous* names used by the test-suite to parametrise
escape-vector regressions.  The actual enforcement, however, is purely
allowlist-based: a module is blocked if it is **not** in the allowlist,
regardless of whether it appears in ``BLOCKED_MODULES``.
"""

from __future__ import annotations

import builtins
import sys
from importlib.abc import MetaPathFinder
from typing import TYPE_CHECKING, Any

from engine.plugins.allowlist import DENYLIST_MODULES, FROZEN_ALLOWED_MODULES

if TYPE_CHECKING:
    from collections.abc import Callable
    from importlib.machinery import ModuleSpec

# Re-exported for backward compatibility with existing tests and callers.
# This is a *known-dangerous-modules* registry, not the enforcement mechanism.
BLOCKED_MODULES: frozenset[str] = DENYLIST_MODULES

# The authoritative allowlist (also re-exported for convenience).
ALLOWED_MODULES: frozenset[str] = FROZEN_ALLOWED_MODULES

# Modules that are always permitted at the CPython bootstrap level and must not
# be purged from ``sys.modules`` even though they are not in the allowlist.
# These are harmless C-extension support modules that the interpreter itself
# depends on and that cannot be used as escape vectors on their own.
_ESSENTIAL_CPYTHON_MODULES: frozenset[str] = frozenset(
    {
        "_abc",
        "_ast",
        "_bisect",
        "_blake2",
        "_codecs",
        "_collections",
        "_contextvars",  # C backing — harmless without the Python wrapper
        "_csv",
        "_ctypes_test",  # test-only shim, not the real ctypes
        "_datetime",
        "_decimal",
        "_functools",
        "_heapq",
        "_imp",
        "_io",
        "_json",
        "_locale",
        "_operator",
        "_random",
        "_sha",
        "_sha3",
        "_signal",
        "_sre",
        "_stat",
        "_string",
        "_struct",
        "_thread",
        "_typing",
        "_warnings",
        "_weakref",
        "_winapi",
        "atexit",
        "builtins",
        "errno",
        "gc",
        "marshal",
        "math",
        "posix",
        "pwd",
        "sys",
        "time",
        "unicodedata",
        "_socket",
        "select",
        "nt",
        "msvcrt",
        "syslog",
    }
)

# Modules that are **always** permitted to import, irrespective of the
# allowlist/denylist.  These are interpreter- and test-harness infrastructure
# packages that the *host* process (not sandboxed strategy code) pulls in at
# runtime.  If the hook ever remains active while the host is running — most
# notably during pytest collection/teardown — intercepting these would crash
# the interpreter or the test runner itself (e.g. CPython's fault dumper, or
# pytest re-importing ``_pytest.warnings`` while recording a warning).  They
# are not useful strategy escape vectors and are exempted defensively.
_INTERNAL_BYPASS_MODULES: frozenset[str] = frozenset(
    {
        # CPython runtime debug/host infra.
        "faulthandler",
        # pytest core and its import-time dependency graph.
        "pytest",
        "_pytest",
        "pluggy",
        "iniconfig",
        "packaging",
        "exceptiongroup",
        # Property-based test harness used by this repository, plus its
        # pure-Python ``sortedcontainers`` dependency, which hypothesis imports
        # lazily while emitting its terminal / observability summary — so an
        # accidentally-leaked hook must not crash the pytest teardown.
        "hypothesis",
        "sortedcontainers",
    }
)


class RestrictedImporter(MetaPathFinder):
    """
    Import hook that enforces the **allowlist** import policy.

    Dual-layer enforcement:
      1. ``sys.meta_path`` finder — catches imports of modules not yet loaded.
      2. ``builtins.__import__`` override — catches re-imports of cached modules.

    A module is allowed only if its root package name is in
    :attr:`allowed`.  An optional explicit :attr:`blocked` set provides
    defence-in-depth on top of the allowlist (so a future too-permissive
    allowlist edit does not silently unblock a known-dangerous module).
    """

    def __init__(
        self,
        blocked: set[str] | None = None,
        *,
        allowed: frozenset[str] | None = None,
    ) -> None:
        # The authoritative allowlist.  ``blocked`` is retained for explicit
        # denylist defence-in-depth and backward compatibility with callers
        # that pass a custom blocked set.
        self.allowed: frozenset[str] = allowed if allowed is not None else ALLOWED_MODULES
        self.blocked: set[str] = blocked if blocked is not None else set()
        self._installed = False
        # Stable identity for the hook.  ``self._restricted_import`` creates a
        # *new* bound-method object on every attribute access (a Python
        # quirk), so storing it once here lets ``install()`` and
        # ``uninstall()`` compare against the very same object via ``is``.
        # Without this, ``uninstall()``'s ownership check
        # (``builtins.__import__ is <hook>``) would *always* be False and the
        # original ``__import__`` would never be restored.
        self._import_hook: Callable[..., Any] = self._restricted_import
        # Capture the real ``__import__`` lazily at ``install()`` time rather
        # than at construction, so construction is side-effect free with
        # respect to the import system.  If ``_restricted_import`` is invoked
        # before ``install()`` (i.e. this is ``None``) it raises a clear
        # ``ImportError`` (see the guard below) instead of an opaque
        # ``TypeError`` from calling ``None``.  The value is intentionally NOT
        # cleared in ``uninstall()``: in out-of-order teardown another
        # importer may still hold a reference to our hook, so keeping a valid
        # delegation target keeps the import system usable rather than turning
        # every import into an error.
        self._original_import: Callable[..., Any] | None = None

    # ── Core decision logic ────────────────────────────────────────────

    def _is_allowed(self, fullname: str) -> bool:
        """Return ``True`` iff *fullname*'s root is permitted by the policy.

        Interpreter/test-harness infrastructure (see
        :data:`_INTERNAL_BYPASS_MODULES`) is always permitted so the hook
        cannot crash the host when it happens to be active during test
        collection or teardown.
        """
        root = fullname.split(".", maxsplit=1)[0]
        if root in _INTERNAL_BYPASS_MODULES:
            return True
        if root in self.blocked:
            return False
        return root in self.allowed

    # ── MetaPathFinder interface ───────────────────────────────────────

    def find_spec(
        self,
        fullname: str,
        _path: object = None,
        _target: object = None,
    ) -> ModuleSpec | None:
        if not self._is_allowed(fullname):
            raise ImportError(
                f"Module '{fullname}' is blocked in strategy sandbox (not in allowlist)"
            )
        return None

    # ── builtins.__import__ override ───────────────────────────────────

    def _restricted_import(
        self,
        name: str,
        globals_: object = None,
        locals_: object = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if level == 0 and not self._is_allowed(name):
            raise ImportError(f"Module '{name}' is blocked in strategy sandbox (not in allowlist)")
        # Delegate to the captured real ``__import__``.  Before ``install()``
        # has run there is nothing to delegate to (``_original_import`` is
        # None), so the guard below raises instead of recursing or calling
        # ``None``.
        original = self._original_import
        if original is None:
            # The importer has never been installed, so there is no captured
            # real ``__import__`` to delegate to.  Fail loudly with a clear
            # ``ImportError`` rather than an opaque ``TypeError`` from calling
            # ``None``.  ``_original_import`` is ``None`` only before
            # ``install()`` runs, and ``install()`` replaces the builtin only
            # *after* capturing it — so once this hook could ever be reached
            # via ``builtins.__import__`` this branch is unreachable, which
            # means raising here cannot cause recursion.
            raise ImportError(
                "RestrictedImporter has no original __import__ to delegate to "
                "(install() was never called)"
            )
        return original(name, globals_, locals_, fromlist, level)

    # ── Lifecycle ──────────────────────────────────────────────────────

    def install(self) -> None:
        if not self._installed:
            # Capture the real importer BEFORE replacing it.  If we replaced
            # first, ``_original_import`` would end up pointing at our own hook
            # and every delegated import would recurse infinitely.
            self._original_import = builtins.__import__
            builtins.__import__ = self._import_hook  # type: ignore[assignment]
            sys.meta_path.insert(0, self)
            self._installed = True

    def uninstall(self) -> None:
        if self._installed:
            # Only restore ``builtins.__import__`` when it still points at
            # *our* hook.  Blindly overwriting would clobber any importer that
            # was installed on top of us (or any test scaffolding that reset
            # the builtin), corrupting the import system during out-of-order /
            # overlapping teardown.  Note we deliberately leave
            # ``_original_import`` intact: another importer may still hold a
            # reference to ``_restricted_import`` and need it to keep working.
            if (
                builtins.__import__ is self._import_hook
                and self._original_import is not None
            ):
                builtins.__import__ = self._original_import  # type: ignore[assignment]
            if self in sys.meta_path:
                sys.meta_path.remove(self)
            self._installed = False

    # ── sys.modules hardening ──────────────────────────────────────────

    def purge_non_allowlisted(self) -> None:
        """
        Remove every non-allowlisted, non-essential entry from ``sys.modules``.

        Called at sandbox startup so that previously-imported dangerous modules
        (e.g. ``os`` imported by the host process) are not reachable via
        ``sys.modules`` lookups by sandboxed code.

        Only modules that are genuinely safe to evict are removed; CPython
        bootstrap essentials listed in :data:`_ESSENTIAL_CPYTHON_MODULES` are
        retained because removing them can crash the interpreter.
        """
        to_remove = [
            name
            for name in list(sys.modules)
            if not self._is_allowed(name)
            and name.split(".", maxsplit=1)[0] not in _ESSENTIAL_CPYTHON_MODULES
        ]
        for name in to_remove:
            sys.modules.pop(name, None)


__all__ = [
    "ALLOWED_MODULES",
    "BLOCKED_MODULES",
    "RestrictedImporter",
]
