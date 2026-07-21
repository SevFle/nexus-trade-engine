"""Layer 1 of the strategy plugin sandbox: **import restrictions**.

This module implements the first (and most important) of the five sandbox
layers described in :mod:`engine.plugins.sandbox`:

    1. Import restrictions (**this module**) — strategy plugins may only import
       modules on a frozen allowlist; everything else (``os``, ``sys``,
       ``subprocess``, ``socket``, ``ctypes``, …) is blocked.
    2. Network whitelist.
    3. Resource limits.
    4. Filesystem isolation.
    5. Process isolation (production target).

It exposes a focused, reusable :class:`ImportGuard` facade with a clean
enable/disable (and context-manager) API, plus the typed
:class:`~engine.plugins.exceptions.SandboxSecurityError` raised on every
violation.

Why a separate facade?
----------------------
The repository already ships a full-featured
:class:`~engine.plugins.restricted_importer.RestrictedImporter`
that is deeply coupled to the runtime ``StrategySandbox`` (it also installs
network/``socket.getaddrinfo`` guards and is driven by the sandbox lifecycle).
:class:`ImportGuard` is a *standalone* Layer-1-only component: it can be used
to enforce the import policy in contexts that do not need the full sandbox
(sandboxed code runners, plugin validators, ad-hoc import linting) and it
reports violations through a dedicated, catchable exception type instead of a
plain ``ImportError``.

How it works
------------
Enforcement is **dual-layered**, mirroring the proven design of the production
finder, because each layer catches a distinct import path:

1. ``sys.meta_path`` finder (:class:`_ImportGuardFinder`) — intercepts imports
   of modules that are *not yet cached* in ``sys.modules``.
2. ``builtins.__import__`` override (:meth:`ImportGuard._restricted_import`) —
   intercepts **re-imports** of modules already cached in ``sys.modules``
   (e.g. ``os`` / ``sys``, which the host process imports at startup).

Both layers raise :class:`SandboxSecurityError` so callers get a single,
consistent, typed exception regardless of which path the violation took.

The policy reuses the vetted, frozen data from :mod:`engine.plugins.allowlist`
(:data:`~engine.plugins.allowlist.FROZEN_ALLOWED_MODULES` and
:data:`~engine.plugins.allowlist.DENYLIST_MODULES`) by default, so this layer
cannot drift from the authoritative policy.  Callers may pass a narrower
``allowed`` set for stricter contexts but the frozen denylist is **always**
unioned in (security is monotonic — a caller can only add names to the
denylist, never remove the frozen dangerous modules).
"""

from __future__ import annotations

import builtins
import sys
import threading
from contextlib import contextmanager
from importlib.abc import MetaPathFinder
from typing import TYPE_CHECKING, Any

from engine.plugins.allowlist import DENYLIST_MODULES, FROZEN_ALLOWED_MODULES
from engine.plugins.exceptions import SandboxSecurityError
from engine.plugins.restricted_importer import _INTERNAL_BYPASS_MODULES

if TYPE_CHECKING:
    from collections.abc import Iterator
    from importlib.machinery import ModuleSpec

__all__ = [
    "ALLOWED_MODULES",
    "BLOCKED_MODULES",
    "ImportGuard",
    "SandboxSecurityError",
]

#: The authoritative frozen allowlist (re-exported for convenience so callers
#: can reference ``import_guard.ALLOWED_MODULES`` without reaching into the
#: policy module).  Only the *root* package name needs to be present; submodules
#: (``json.decoder``, ``datetime.timedelta``, …) inherit the root decision.
ALLOWED_MODULES: frozenset[str] = FROZEN_ALLOWED_MODULES

#: Known-dangerous modules used as defence-in-depth on top of the allowlist
#: (re-exported for convenience).  A module is rejected if its root is here
#: *even if* a too-permissive allowlist edit also added it — the denylist always
#: wins.  See :meth:`ImportGuard.is_allowed`.
BLOCKED_MODULES: frozenset[str] = DENYLIST_MODULES


class _ImportGuardFinder(MetaPathFinder):
    """``sys.meta_path`` finder that rejects non-allowlisted modules.

    Inserted at the **front** of ``sys.meta_path`` by
    :meth:`ImportGuard.enable` so it runs ahead of the default finders.  When a
    requested module's root is not on the allowlist it raises
    :class:`SandboxSecurityError` *before* any filesystem/search-path lookup
    occurs, so the module's code is never executed.

    Allowed modules return ``None`` (i.e. "I don't handle this — let the next
    finder try"), which is the standard ``MetaPathFinder`` contract.
    """

    def __init__(self, guard: ImportGuard) -> None:
        self._guard = guard

    def find_spec(
        self,
        fullname: str,
        _path: object = None,
        _target: object = None,
    ) -> ModuleSpec | None:
        guard = self._guard
        if guard.is_blocked(fullname):
            raise SandboxSecurityError(
                fullname,
                reason=(
                    "explicitly denylisted"
                    if fullname.split(".", maxsplit=1)[0] in guard.blocked
                    and fullname.split(".", maxsplit=1)[0] not in guard.allowed
                    else "not in allowlist"
                ),
            )
        return None


class ImportGuard:
    """Standalone, reusable Layer-1 import restriction guard.

    Enforces an **allowlist** import policy for strategy plugin code: only
    modules whose root package is in :attr:`allowed` may be imported, and the
    frozen :data:`BLOCKED_MODULES` denylist is always unioned in as
    defence-in-depth so a known-dangerous module can never be re-enabled by an
    over-permissive allowlist edit.

    The guard is **off by default**.  Call :meth:`enable` to install it (or use
    the :meth:`activated` context manager for scoped enforcement) and
    :meth:`disable` to remove it.  Enable/disable are idempotent and safe to
    call repeatedly.

    Parameters
    ----------
    allowed:
        Optional iterable of root module names to permit.  Defaults to the
        frozen :data:`ALLOWED_MODULES`.  Passing a narrower set makes the guard
        stricter; it can never make it more permissive than the frozen
        denylist allows.
    blocked:
        Optional iterable of additional root module names to deny on top of
        the frozen :data:`BLOCKED_MODULES`.  The frozen denylist is **always**
        unioned in, so security is monotonic (callers can only add blocks).

    Examples
    --------
    >>> guard = ImportGuard()
    >>> with guard.activated():
    ...     import math  # allowlisted → succeeds
    ...     import os    # blocked → SandboxSecurityError
    Traceback (most recent call last):
        ...
    engine.plugins.exceptions.SandboxSecurityError: Import of module 'os' ...
    """

    #: The default allowlist, surfaced as a class attribute for discovery and
    #: documentation tooling.
    DEFAULT_ALLOWED_MODULES: frozenset[str] = FROZEN_ALLOWED_MODULES

    #: The default denylist, surfaced as a class attribute.
    DEFAULT_BLOCKED_MODULES: frozenset[str] = DENYLIST_MODULES

    def __init__(
        self,
        allowed: Any = None,
        blocked: Any = None,
    ) -> None:
        # The authoritative allowlist.  ``None`` → frozen default.
        self.allowed: frozenset[str] = (
            frozenset(allowed) if allowed is not None else FROZEN_ALLOWED_MODULES
        )
        # The effective denylist: the frozen set is ALWAYS included so a caller
        # can only *add* names (monotonic security).  ``blocked=None`` keeps the
        # frozen set verbatim (the common case for the production policy).
        if blocked is None:
            self.blocked: frozenset[str] = frozenset(DENYLIST_MODULES)
        else:
            self.blocked = frozenset(blocked) | DENYLIST_MODULES

        # ── activation state ──
        self._installed: bool = False
        # Re-entrant lock: ``sys.meta_path`` and ``builtins.__import__`` are
        # process-global mutable state; concurrent enable/disable (e.g. nested
        # context managers, or a strategy that re-enters the guard) must be
        # serialised to avoid torn state.
        self._lock: threading.RLock = threading.RLock()
        self._original_import: Any = None
        self._finder: _ImportGuardFinder | None = None
        # Capture the bound ``_restricted_import`` once: Python creates a fresh
        # bound-method object on every attribute access, so storing it lets
        # ``enable``/``disable`` compare identity via ``is`` (otherwise
        # ``disable`` could never tell it owned ``builtins.__import__``).
        self._import_hook: Any = self._restricted_import

    # ── Policy decisions ──────────────────────────────────────────────

    def is_allowed(self, name: str) -> bool:
        """Return ``True`` iff *name*'s root is permitted by the policy.

        Precedence (highest to lowest):

          1. Interpreter / test-harness infrastructure
             (:data:`_INTERNAL_BYPASS_MODULES`) — always permitted, so the
             guard cannot crash the host (or pytest) while active.
          2. The denylist (:attr:`blocked`) — a blocked root is always
             rejected, even if it also appears in :attr:`allowed`.  This is
             the defence-in-depth that prevents an allowlist edit from
             silently un-blocking a known-dangerous module.
          3. The allowlist (:attr:`allowed`).
        """
        if not name:
            return True  # defensive: empty/relative-only names cannot escape.
        root = name.split(".", maxsplit=1)[0]
        if root in _INTERNAL_BYPASS_MODULES:
            return True
        if root in self.blocked:
            return False
        return root in self.allowed

    def is_blocked(self, name: str) -> bool:
        """Return ``True`` iff importing *name* would be rejected."""
        return not self.is_allowed(name)

    def check_import(self, name: str) -> None:
        """Raise :class:`SandboxSecurityError` if *name* is not allowed.

        This is the synchronous, side-effect-free policy gate: it does **not**
        perform an import or touch ``sys.modules``.  Use it to validate a
        module name ahead of time (e.g. while parsing a plugin's declared
        dependencies) without needing to enable the guard.
        """
        if self.is_blocked(name):
            root = name.split(".", maxsplit=1)[0]
            reason = (
                "explicitly denylisted"
                if root in self.blocked and root not in self.allowed
                else "not in allowlist"
            )
            raise SandboxSecurityError(name, reason=reason)

    # ── Activation lifecycle ──────────────────────────────────────────

    @property
    def is_active(self) -> bool:
        """``True`` while the guard is installed on ``sys.meta_path``."""
        return self._installed

    def enable(self) -> None:
        """Install the guard: ``sys.meta_path`` finder + ``__import__`` override.

        Idempotent: calling ``enable()`` twice (without an intervening
        :meth:`disable`) is a no-op after the first call.
        """
        with self._lock:
            if self._installed:
                return
            # Capture the real importer BEFORE replacing it.  Replacing first
            # would make ``_original_import`` point at our own hook and every
            # delegated import would recurse infinitely.
            self._original_import = builtins.__import__
            self._finder = _ImportGuardFinder(self)
            sys.meta_path.insert(0, self._finder)
            builtins.__import__ = self._import_hook  # type: ignore[assignment]
            self._installed = True

    def disable(self) -> None:
        """Remove the guard and restore the original import machinery.

        Idempotent.  Only restores ``builtins.__import__`` and removes the
        finder when the guard still *owns* them — this prevents a blind
        overwrite from clobbering a different importer that was layered on top
        (or test scaffolding that reset the builtin), which would corrupt the
        import system during out-of-order teardown.
        """
        with self._lock:
            if not self._installed:
                return
            if (
                builtins.__import__ is self._import_hook
                and self._original_import is not None
            ):
                builtins.__import__ = self._original_import  # type: ignore[assignment]
            if self._finder is not None and self._finder in sys.meta_path:
                sys.meta_path.remove(self._finder)
            self._finder = None
            # Intentionally keep ``_original_import`` valid until the next
            # ``enable()``: an out-of-order caller may still hold a reference
            # to ``_import_hook`` and need it to keep delegating.
            self._original_import = None
            self._installed = False

    @contextmanager
    def activated(self) -> Iterator[ImportGuard]:
        """Context manager that ``enable``s the guard for the block's duration.

        Guarantees :meth:`disable` runs on exit even if the body raises — so a
        failing assertion inside the block can never leak the guard onto the
        process-global import machinery.
        """
        self.enable()
        try:
            yield self
        finally:
            self.disable()

    # ── builtins.__import__ override (catches ``sys.modules`` re-imports) ──

    def _restricted_import(
        self,
        name: str,
        globals_: object = None,
        locals_: object = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        # Relative imports (``level > 0``) resolve within the plugin package
        # and carry no cross-package escape vector at this layer, so only
        # absolute imports are gated here.
        if level == 0 and self.is_blocked(name):
            root = name.split(".", maxsplit=1)[0]
            raise SandboxSecurityError(
                name,
                reason=(
                    "explicitly denylisted"
                    if root in self.blocked and root not in self.allowed
                    else "not in allowlist"
                ),
            )
        original = self._original_import
        if original is None:
            # Unreachable in normal operation: the hook is only installed by
            # ``enable()``, which captures ``_original_import`` first.  Guard
            # defensively rather than calling ``None``.
            raise SandboxSecurityError(
                name,
                reason="guard not enabled (no original __import__ to delegate to)",
            )
        return original(name, globals_, locals_, fromlist, level)
