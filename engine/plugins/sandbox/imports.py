"""
Layer 1 import restrictions for the strategy plugin sandbox.

:class:`ImportRestrictor` is the scoped, context-manager-friendly import guard
used by the plugin sandbox.  It enforces a configurable **allowlist** of modules
importable by strategy plugin code, with an explicit **blocklist** layered on
top for defence-in-depth (so a future too-permissive allowlist edit cannot
silently unblock a known-dangerous module such as ``os``, ``subprocess`` or
``socket``).

Enforcement is dual-layered, mirroring
:class:`~engine.plugins.restricted_importer.RestrictedImporter`:

1. ``sys.meta_path`` finder — intercepts imports of modules that are not yet
   loaded.  Blocking is achieved by raising ``ImportError`` from
   :meth:`ImportRestrictor.find_spec`.
2. ``builtins.__import__`` override — intercepts re-imports of modules that are
   already cached in ``sys.modules`` (e.g. ``os``, ``sys``, which the host
   process imports at startup).  Without this layer a cached dangerous module
   would be reachable via a plain ``import`` statement.

The recommended usage is as a context manager, which guarantees the original
import state is restored on exit — even if the guarded block raises:

>>> with ImportRestrictor():
...     import json            # allowed by the default allowlist
...     import os              # raises ImportError

The default allowlist and blocklist are the production-vetted sets defined in
:mod:`engine.plugins.allowlist` (``FROZEN_ALLOWED_MODULES`` and
``DENYLIST_MODULES``).  Callers may pass tighter custom sets for testing or for
strategies that need a reduced capability surface.

Nesting
-------
Multiple :class:`ImportRestrictor` contexts may be nested.  Each instance
captures the ``builtins.__import__`` it finds at :meth:`install` time and
restores exactly that object on :meth:`uninstall`, so exiting an inner context
leaves the outer context's restrictions in effect until it too exits.
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

__all__ = ["ImportRestrictor"]


class ImportRestrictor(MetaPathFinder):
    """Scoped allowlist/blocklist guard for strategy plugin imports.

    A module is permitted if and only if its *root* package name is in
    :attr:`allowed` **and** not in :attr:`blocked` (blocklist wins).  Submodules
    (``json.decoder``, ``os.path``) inherit the decision of their root package.

    Parameters
    ----------
    allowed:
        Iterable of root module names that strategy code may import.  Defaults
        to :data:`~engine.plugins.allowlist.FROZEN_ALLOWED_MODULES`.  Pass
        ``set()`` to deny every absolute import.
    blocked:
        Iterable of root module names that are *always* denied, even if they
        also appear in ``allowed``.  Defaults to
        :data:`~engine.plugins.allowlist.DENYLIST_MODULES`.
    """

    #: The set of root module names permitted by this restrictor.
    allowed: frozenset[str]
    #: The set of root module names unconditionally denied (defence-in-depth).
    blocked: frozenset[str]

    def __init__(
        self,
        allowed: frozenset[str] | set[str] | None = None,
        *,
        blocked: frozenset[str] | set[str] | None = None,
    ) -> None:
        self.allowed = frozenset(allowed) if allowed is not None else FROZEN_ALLOWED_MODULES
        self.blocked = frozenset(blocked) if blocked is not None else DENYLIST_MODULES
        self._installed: bool = False
        self._original_import: Callable[..., Any] | None = None

    # ── Core decision logic ────────────────────────────────────────────

    def is_allowed(self, fullname: str) -> bool:
        """Return ``True`` iff *fullname*'s root package is importable here.

        The explicit :attr:`blocked` set takes precedence over :attr:`allowed`
        so that a known-dangerous module can never slip through a
        too-permissive allowlist edit.
        """
        root = fullname.split(".", maxsplit=1)[0]
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
        # Raising from ``find_spec`` propagates as ImportError out of the
        # import statement, which is the documented blocking technique.  For
        # allowed modules we return ``None`` so the real finders can resolve
        # them normally.
        if not self.is_allowed(fullname):
            raise ImportError(
                f"Module '{fullname}' is blocked in the plugin sandbox "
                f"(not in import allowlist)"
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
    ) -> Any:
        # Only enforce on absolute imports (``level == 0``).  Relative imports
        # are resolved against the importing module's package and are left to
        # the real import machinery.
        if level == 0 and not self.is_allowed(name):
            raise ImportError(
                f"Module '{name}' is blocked in the plugin sandbox "
                f"(not in import allowlist)"
            )
        assert self._original_import is not None
        return self._original_import(name, globals_, locals_, fromlist, level)

    # ── Lifecycle ──────────────────────────────────────────────────────

    def install(self) -> ImportRestrictor:
        """Activate the restrictions.  Idempotent — returns ``self``."""
        if self._installed:
            return self
        # Capture the *current* ``__import__`` so nested restrictors restore
        # the right shim on uninstall (see module docstring "Nesting").
        self._original_import = builtins.__import__
        builtins.__import__ = self._restricted_import  # type: ignore[assignment]
        sys.meta_path.insert(0, self)
        self._installed = True
        return self

    def uninstall(self) -> None:
        """Deactivate the restrictions.  Safe to call when not installed."""
        if not self._installed:
            return
        if self._original_import is not None:
            builtins.__import__ = self._original_import  # type: ignore[assignment]
        if self in sys.meta_path:
            sys.meta_path.remove(self)
        self._original_import = None
        self._installed = False

    # ── Context manager protocol ───────────────────────────────────────

    def __enter__(self) -> ImportRestrictor:
        return self.install()

    def __exit__(self, *exc_info: object) -> None:
        self.uninstall()
