"""Static, AST-based import validator for strategy plugins.

This is a *pre-execution* security gate that complements the runtime
allowlist enforcement in :mod:`engine.plugins.restricted_importer`.  Rather
than waiting for a strategy's ``import os`` to surface as an opaque
``ImportError`` deep inside ``on_bar`` (or, worse, never surface at all if the
runtime hook is mis-configured), this module parses the strategy source into
an AST **before** it is ever compiled or executed and rejects any import that
targets a blocked module with a precise, source-positioned error.

Two import forms are statically recognised, including nested/dotted module
access::

    import os                  # Import node, alias.name == "os"
    import os.path as op       # Import node, alias.name == "os.path"
    from os import system      # ImportFrom node, node.module == "os"
    from os.path import join   # ImportFrom node, node.module == "os.path"

Aliases (``as``) are irrelevant to policy — it is the *module name*, not the
local binding, that determines whether an import is dangerous.

Policy model
------------
Denylist-driven and intentionally conservative:

* A module is **blocked** if its *root package* (or an exact submodule path)
  appears in :attr:`SandboxConfig.blocked_imports`.  Blocking the root
  automatically blocks the whole subtree (``os`` → ``os.path``, ``os.environ``).
* A module is **allowed** if it — or any of its ancestor packages — appears in
  :attr:`SandboxConfig.allowed_imports`.  This explicit override supports the
  rare case where a parent package is dangerous but a specific leaf submodule
  is safe (e.g. permitting ``xml.etree.ElementTree`` while blocking ``xml``).

This static pass is **defence-in-depth**, not a replacement for the runtime
importer: dynamic imports (``importlib.import_module("o" + "s")``,
``__import__("os")``) cannot be reliably resolved from the AST.  Those are
caught because ``importlib`` / ``__import__`` access themselves require
importing a blocked module — which this validator *does* flag.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from engine.plugins.allowlist import DENYLIST_MODULES

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = [
    "DEFAULT_BLOCKED_IMPORTS",
    "ImportChecker",
    "SandboxConfig",
    "SecurityViolation",
    "validate_source",
]


# Default denylist re-exported under a name that reflects this layer's
# semantics (static import blocking) rather than the runtime importer's.
DEFAULT_BLOCKED_IMPORTS: frozenset[str] = DENYLIST_MODULES


class SecurityViolation(Exception):  # noqa: N818 — name mandated by spec/SEC policy
    """Raised when a strategy statically imports a blocked module.

    Carries the offending module name and the 1-based source line / 0-based
    column of the offending statement so that error messages reported back to
    the plugin author point precisely at the line to fix.
    """

    def __init__(
        self,
        module: str,
        line: int,
        col: int,
        *,
        form: str = "import",
    ) -> None:
        self.module = module
        self.line = line
        self.col = col
        self.form = form
        super().__init__(
            f"blocked import of module {module!r} "
            f"({form} statement at line {line}, column {col})"
        )


@dataclass(frozen=True)
class SandboxConfig:
    """Configuration for the static import validator.

    Attributes
    ----------
    blocked_imports:
        Denylist of module names that must never appear in a strategy's
        static import graph.  An entry may be either a *root package*
        (``"os"``) — which also blocks every submodule (``os.path``,
        ``os.environ``) — or an exact submodule path (``"ctypes.util"``).
        Defaults to :data:`DEFAULT_BLOCKED_IMPORTS`.
    allowed_imports:
        Explicit override set.  A module — or any module nested under one of
        these entries — is permitted even if its root package is blocked.
        Defaults to empty (no overrides).
    """

    blocked_imports: frozenset[str] = field(
        default_factory=lambda: DEFAULT_BLOCKED_IMPORTS
    )
    allowed_imports: frozenset[str] = field(default_factory=frozenset)


def _module_is_allowed(module: str, allowed: frozenset[str]) -> bool:
    """True if *module* or any ancestor package is in the override set.

    ``xml.sax.saxutils`` is considered allowed when ``xml.sax`` is listed, so
    that whitelisting a parent package transitively permits its submodules.
    """
    parts = module.split(".")
    # Walk from the full dotted path up to the root, returning on the first
    # ancestor that is explicitly allowed.
    return any(".".join(parts[:depth]) in allowed for depth in range(len(parts), 0, -1))


def _module_is_blocked(module: str, config: SandboxConfig) -> bool:
    """Apply the denylist policy to a single resolved module name.

    The override allowlist takes precedence: if *module* (or an ancestor) is
    explicitly allowed, it is never blocked — even when its root appears in
    the denylist.  Otherwise a module is blocked when either itself or its
    root package is in ``config.blocked_imports``.
    """
    if not module:
        return False
    if _module_is_allowed(module, config.allowed_imports):
        return False
    if module in config.blocked_imports:
        return True
    # Root-package match: blocking "os" must also block "os.path",
    # "os.environ", etc.  ``split(".", 1)[0]`` yields the root for any
    # dotted path and the path itself for a bare name.
    root = module.split(".", 1)[0]
    return root in config.blocked_imports


class ImportChecker(ast.NodeVisitor):
    """AST visitor that flags every blocked import in parsed source.

    Usage
    -----
    Construct with an optional :class:`SandboxConfig`, then drive it with
    :func:`validate_source` (preferred) or by feeding a parsed
    :class:`ast.Module` to :meth:`visit`.  All violations encountered during a
    walk accumulate in :attr:`violations` (in source order); the convenience
    helper raises the first one.

    Both ``Import`` and ``ImportFrom`` nodes are inspected.  Relative imports
    (``from . import x``, ``from .pkg import y``) are out of scope — they
    resolve within the strategy package itself and cannot reach the blocked
    stdlib/third-party modules this layer polices.
    """

    def __init__(self, config: SandboxConfig | None = None) -> None:
        self.config = config or SandboxConfig()
        # Collected in document order so the "first" violation is also the
        # earliest in the source — the most useful one to surface first.
        self.violations: list[SecurityViolation] = []

    # ── AST visit hooks ──────────────────────────────────────────────

    def visit_Import(self, node: ast.Import) -> Any:
        """Handle ``import a`` / ``import a.b`` / ``import a as c``.

        Each alias carries its own fully-qualified dotted name in
        ``alias.name`` (``"os.path"``), so nested attribute access is covered
        for free by the dotted-string policy check.
        """
        for alias in node.names:
            if _module_is_blocked(alias.name, self.config):
                self.violations.append(
                    SecurityViolation(
                        alias.name,
                        node.lineno,
                        node.col_offset,
                        form="import",
                    )
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> Any:
        """Handle ``from a import b`` / ``from a.b import c``.

        ``node.module`` holds the dotted module being imported *from*; the
        individual imported names (``b``/``c``) are attributes/submodules of
        that module and do not change whether the *module* itself is
        blocked.  Only absolute imports (``node.level == 0``) are policed.
        """
        module = node.module or ""
        if node.level == 0 and _module_is_blocked(module, self.config):
            self.violations.append(
                SecurityViolation(
                    module,
                    node.lineno,
                    node.col_offset,
                    form="from",
                )
            )
        self.generic_visit(node)

    # ── Introspection helpers ────────────────────────────────────────

    def iter_violations(self) -> Iterator[SecurityViolation]:
        """Iterate over collected violations in source order."""
        return iter(self.violations)


def validate_source(
    source: str,
    *,
    config: SandboxConfig | None = None,
    filename: str = "<strategy>",
) -> None:
    """Parse *source* and raise on the first blocked import.

    Parameters
    ----------
    source:
        Strategy Python source to statically validate.
    config:
        Optional :class:`SandboxConfig` controlling the blocklist / override
        allowlist.  Defaults to the standard dangerous-module denylist.
    filename:
        Filename used in ``SyntaxError`` / ``SecurityViolation`` messages —
        purely cosmetic, aids error clarity for plugin authors.

    Raises
    ------
    SyntaxError:
        If *source* is not valid Python.  A malformed plugin is a programmer
        error, not a security event, so the error propagates unwrapped.
    SecurityViolation:
        On the first (earliest-in-source) blocked import encountered.  The
        full list of violations found is available on the checker if the
        caller wants exhaustive reporting instead.
    """
    tree = ast.parse(source, filename=filename)
    checker = ImportChecker(config)
    checker.visit(tree)
    if checker.violations:
        # Surface the earliest violation first — it is the one the author
        # should fix before re-running.
        first = checker.violations[0]
        raise first
