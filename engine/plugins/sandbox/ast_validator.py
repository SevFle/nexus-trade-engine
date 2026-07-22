"""Layer 1: AST-based import / call restriction validator for plugin source.

This module is the static, *parse-time* complement to the runtime import
enforcement in :class:`~engine.plugins.restricted_importer.RestrictedImporter`.
It parses strategy / plugin source code via :mod:`ast` and walks the resulting
tree to detect forbidden imports, relative imports that escape the strategy
package, and dynamic code-execution / dynamic-import patterns **before** the
source is ever compiled or executed.

Design
------
* It lives under :mod:`engine.plugins.sandbox` — the natural home for sandbox
  *layers* — and is import-light (only :mod:`ast` plus the frozen allowlist
  data module), so it can be invoked from the plugin loader / CI gate without
  paying for the runtime-hook machinery.
* It returns a **structured** :class:`ValidationResult` carrying rich
  :class:`Violation` records (line, column, offending module, machine-readable
  code, severity) instead of a flat ``list[str]`` — callers can render and
  react to individual violations with full provenance.
* It is **total** — :meth:`ASTValidator.validate` always returns a
  :class:`ValidationResult`, capturing :class:`SyntaxError` as a violation
  rather than raising, so callers never need a surrounding try/except just to
  obtain a result object.

Policy model
------------
The validator is configured with an **allowlist** and a **denylist** of module
*root* names, plus a set of forbidden call builtins.

  * **Denylist precedence** — a module whose root is on the denylist is
    *always* blocked, even if it also appears on the allowlist.  This mirrors
    :meth:`RestrictedImporter._is_allowed` and closes the "allowlist shadows a
    blocked module" bypass.
  * **Allowlist enforcement** — when the allowlist is *non-empty*, a module
    whose root is not on the allowlist is blocked (defence-in-depth that
    catches unlisted modules at parse time, not only at import time).
  * **Empty allowlist** — when the allowlist is empty the allowlist gate is a
    no-op and only the denylist applies (permissive-unless-denied).
  * **Relative imports** — a ``from ..`` (or deeper) import reaches
    *outside* the strategy package and is flagged as a potential escape vector
    (rejected by default).  A ``from .`` import (level 1) resolves within the
    strategy package, but its module name and imported names are *still*
    checked against the denylist/allowlist — a strategy package can re-export
    or shadow a forbidden module, so ``from . import os`` is not blanket-
    permitted.

The detected call sites are the canonical code-execution / dynamic-import
escape vectors that bypass the static ``import`` statement the runtime hook
intercepts: ``__import__``, ``exec``, ``eval``, ``compile`` and
``importlib.import_module`` / ``importlib.__import__``.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from engine.plugins.allowlist import DENYLIST_MODULES

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "CODE_FORBIDDEN_CALL",
    "CODE_FORBIDDEN_FROM_IMPORT",
    "CODE_FORBIDDEN_IMPORT",
    "CODE_RELATIVE_IMPORT",
    "CODE_SYNTAX_ERROR",
    "DEFAULT_DENYLIST",
    "DEFAULT_FORBIDDEN_CALLS",
    "SEVERITY_ERROR",
    "SEVERITY_WARNING",
    "ASTValidator",
    "ValidationResult",
    "Violation",
    "validate_strategy_source",
]

# ── Severities / machine-readable reason codes ────────────────────────
SEVERITY_ERROR: str = "error"
SEVERITY_WARNING: str = "warning"

CODE_FORBIDDEN_IMPORT: str = "forbidden-import"
CODE_FORBIDDEN_FROM_IMPORT: str = "forbidden-from-import"
CODE_FORBIDDEN_CALL: str = "forbidden-call"
CODE_RELATIVE_IMPORT: str = "relative-import"
CODE_SYNTAX_ERROR: str = "syntax-error"

#: Maximum relative-import level that is permitted.  ``level == 1`` (``from .``)
#: resolves inside the strategy package; anything deeper reaches outside it.
_MAX_RELATIVE_LEVEL: int = 1

# ── Default policy sets ───────────────────────────────────────────────
#
# The authoritative denylist is the frozen
# :data:`~engine.plugins.allowlist.DENYLIST_MODULES`.  ``allowlist`` defaults to
# *empty* (permissive-unless-denied); callers that want parse-time allowlist
# enforcement pass an explicit non-empty set.
DEFAULT_DENYLIST: frozenset[str] = DENYLIST_MODULES

#: Bare-name calls that bypass the ``import`` statement and execute / load
#: arbitrary code statically.
DEFAULT_FORBIDDEN_CALLS: frozenset[str] = frozenset(
    {"__import__", "exec", "eval", "compile"}
)

#: Attribute lookups on ``importlib`` that perform a dynamic import and so
#: bypass the static ``import`` statement the runtime hook intercepts.
_IMPORTLIB_FORBIDDEN_ATTRS: frozenset[str] = frozenset({"import_module", "__import__"})


# ── Result objects ────────────────────────────────────────────────────
@dataclass(frozen=True)
class Violation:
    """A single policy violation found while walking the source AST.

    Attributes
    ----------
    line:
        1-based line number where the violation occurs (``-1`` when the
        violation is not tied to a concrete source line, e.g. a syntax error
        re-synthesised without position info).
    col:
        0-based column offset of the offending node (``-1`` when unknown).
    code:
        Machine-readable reason code (one of the ``CODE_*`` constants) so
        downstream tooling can switch on it without parsing prose.
    message:
        Human-readable description, safe to surface in logs / error bodies.
    module:
        The offending module name for import violations (``os``,
        ``subprocess``…); ``None`` for non-import violations.
    severity:
        :data:`SEVERITY_ERROR` (default) or :data:`SEVERITY_WARNING`.  Only
        ``error``-severity violations make a result invalid.
    """

    line: int
    col: int
    code: str
    message: str
    module: str | None = None
    severity: str = SEVERITY_ERROR

    def __str__(self) -> str:
        loc = f"line {self.line}" if self.line > 0 else "<unknown line>"
        prefix = f"{loc}: {self.message}"
        if self.code:
            return f"[{self.code}] {prefix}"
        return prefix


@dataclass(frozen=True)
class ValidationResult:
    """Structured outcome of validating a source tree.

    A result is *valid* iff it contains no :data:`SEVERITY_ERROR` violations.
    """

    violations: tuple[Violation, ...] = field(default_factory=tuple)

    @property
    def is_valid(self) -> bool:
        """``True`` when there are no error-severity violations (source allowed)."""
        return not self.has_errors

    @property
    def has_errors(self) -> bool:
        """``True`` when at least one error-severity violation was recorded."""
        return any(v.severity == SEVERITY_ERROR for v in self.violations)

    @property
    def has_warnings(self) -> bool:
        """``True`` when at least one warning-severity violation was recorded."""
        return any(v.severity == SEVERITY_WARNING for v in self.violations)

    @property
    def error_count(self) -> int:
        """Number of error-severity violations."""
        return sum(1 for v in self.violations if v.severity == SEVERITY_ERROR)

    def errors(self) -> tuple[Violation, ...]:
        """Return only the error-severity violations, in source order."""
        return tuple(v for v in self.violations if v.severity == SEVERITY_ERROR)

    def error_messages(self) -> list[str]:
        """Convenience: human-readable messages for every error violation."""
        return [str(v) for v in self.errors()]

    def forbidden_modules(self) -> tuple[str, ...]:
        """Distinct offending *root* module names from import violations."""
        seen: list[str] = []
        for v in self.violations:
            if not v.module:
                continue
            root = v.module.split(".", maxsplit=1)[0]
            if root not in seen:
                seen.append(root)
        return tuple(seen)


# ── The validator ─────────────────────────────────────────────────────
class ASTValidator(ast.NodeVisitor):
    """Static, parse-time validator returning a :class:`ValidationResult`.

    Walks the AST of a strategy's Python source and records violations for:

      * ``import`` / ``from … import`` of a forbidden module,
      * relative imports that escape the strategy package (``level > 1``),
      * calls to code-execution / dynamic-import builtins
        (``exec``/``eval``/``compile``/``__import__``/``importlib.import_module``).

    Parameters
    ----------
    allowlist:
        Iterable of permitted module *root* names.  When **non-empty** a module
        whose root is not on the allowlist is rejected.  When **empty** the
        allowlist gate is skipped (only the denylist applies).  Defaults to
        empty (permissive-unless-denied).
    denylist:
        Iterable of forbidden module *root* names.  The denylist always wins
        over the allowlist.  Defaults to the frozen
        :data:`~engine.plugins.allowlist.DENYLIST_MODULES`.
    forbidden_calls:
        Iterable of bare-name builtins that are rejected when called.  Defaults
        to :data:`DEFAULT_FORBIDDEN_CALLS`
        (``__import__``, ``exec``, ``eval``, ``compile``).

    The validator is reusable: :meth:`validate` resets internal state on every
    call, so a single instance can validate many sources sequentially.
    """

    def __init__(
        self,
        allowlist: Iterable[str] | None = None,
        denylist: Iterable[str] | None = None,
        forbidden_calls: Iterable[str] | None = None,
    ) -> None:
        self.allowlist: frozenset[str] = (
            frozenset(allowlist) if allowlist is not None else frozenset()
        )
        self.denylist: frozenset[str] = (
            frozenset(denylist) if denylist is not None else DEFAULT_DENYLIST
        )
        self.forbidden_calls: frozenset[str] = (
            frozenset(forbidden_calls)
            if forbidden_calls is not None
            else DEFAULT_FORBIDDEN_CALLS
        )
        self._violations: list[Violation] = []

    @staticmethod
    def _root(module: str) -> str:
        """Return the root package name of a dotted module (``os.path`` → ``os``)."""
        return module.split(".", maxsplit=1)[0]

    def _is_forbidden_module(self, module: str) -> bool:
        """``True`` iff *module* is blocked by the allowlist/denylist policy.

        Precedence:

          1. **Denylist wins** — an exact or root match in :attr:`denylist`
             always blocks, even over :attr:`allowlist`.
          2. **Allowlist gate** — when :attr:`allowlist` is non-empty, a module
             whose root is not on the allowlist is blocked.
          3. **Empty allowlist** — only the denylist applies (permissive).
        """
        root = self._root(module)
        # 1. Denylist precedence (root or exact match).
        if root in self.denylist or module in self.denylist:
            return True
        # 2/3. Allowlist gate (no-op when empty).
        if self.allowlist:
            return root not in self.allowlist and module not in self.allowlist
        return False

    # ── AST visitors ───────────────────────────────────────────────────

    def visit_Import(self, node: ast.Import) -> None:
        """Flag ``import <forbidden>`` statements (all aliases checked)."""
        for alias in node.names:
            if self._is_forbidden_module(alias.name):
                self._violations.append(
                    Violation(
                        line=node.lineno,
                        col=node.col_offset,
                        code=CODE_FORBIDDEN_IMPORT,
                        message=f"import of forbidden module {alias.name!r}",
                        module=alias.name,
                    )
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """Flag ``from <forbidden> import ...`` and escaping relative imports.

        Relative imports are **not** blanket-skipped.  A strategy package can
        re-export or shadow a forbidden module (e.g. a package-level
        ``__init__`` that performs ``import os``), so a ``from . import os``
        would hand the strategy a reference to a blocked module even though no
        static ``import os`` line was ever written.  The validator therefore
        resolves the relative module name *heuristically* and checks both it
        and every imported name against the denylist/allowlist.

        * A relative import whose level exceeds :data:`_MAX_RELATIVE_LEVEL`
          reaches *outside* the strategy package and is flagged as a potential
          escape vector regardless of the imported name — parent-package
          traversal is high-risk and rejected by default.
        * A level-1 relative import (``from .``) resolves within the strategy
          package; the module name (when present) and each imported name are
          checked against the policy.  An offending name produces a
          :data:`CODE_FORBIDDEN_FROM_IMPORT` violation.
        * An absolute from-import (level 0) is checked against the module
          policy.
        """
        # Relative import reaching beyond the current package: parent-package
        # traversal is suspicious / high-risk, so it is rejected by default.
        if node.level and node.level > _MAX_RELATIVE_LEVEL:
            self._violations.append(
                Violation(
                    line=node.lineno,
                    col=node.col_offset,
                    code=CODE_RELATIVE_IMPORT,
                    message=(
                        f"relative import level {node.level} escapes the "
                        "strategy package"
                    ),
                    module=node.module,
                )
            )
            self.generic_visit(node)
            return

        if node.level:
            # Level-1 relative import (``from .``): resolves within the
            # strategy package, but a package can re-export or shadow a
            # forbidden module.  Resolve the module name heuristically and
            # check it, plus every imported name, against the denylist /
            # allowlist so both ``from . import os`` and ``from .os import
            # path`` are caught instead of blanket-permitted.
            module = node.module or ""
            if module and self._is_forbidden_module(module):
                self._violations.append(
                    Violation(
                        line=node.lineno,
                        col=node.col_offset,
                        code=CODE_FORBIDDEN_FROM_IMPORT,
                        message=(
                            f"import from forbidden module {module!r} "
                            "(relative)"
                        ),
                        module=module,
                    )
                )
            for alias in node.names:
                name = alias.name
                # ``from . import os`` → ``name == "os"``;
                # ``from .x import y`` → ``name == "y"``.  ``*`` is a wildcard
                # and not a real identifier, so it is never a module root.
                if not name or name == "*":
                    continue
                if self._is_forbidden_module(name):
                    self._violations.append(
                        Violation(
                            line=node.lineno,
                            col=node.col_offset,
                            code=CODE_FORBIDDEN_FROM_IMPORT,
                            message=(
                                f"import of forbidden name {name!r} "
                                "(relative)"
                            ),
                            module=name,
                        )
                    )
            self.generic_visit(node)
            return

        # Absolute from-import (level 0).
        module = node.module or ""
        if module and self._is_forbidden_module(module):
            self._violations.append(
                Violation(
                    line=node.lineno,
                    col=node.col_offset,
                    code=CODE_FORBIDDEN_FROM_IMPORT,
                    message=f"import from forbidden module {module!r}",
                    module=module,
                )
            )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        """Flag code-execution / dynamic-import calls.

        Catches:

        * bare-name calls to a forbidden builtin (``exec(...)``,
          ``eval(...)``, ``compile(...)``, ``__import__(...)``);
        * the qualified dynamic import ``importlib.import_module(...)`` and
          ``importlib.__import__(...)``.

        These bypass the static ``import`` statement the runtime hook
        intercepts, so they must be rejected statically.
        """
        func = node.func
        if isinstance(func, ast.Name):
            if func.id in self.forbidden_calls:
                self._violations.append(
                    Violation(
                        line=node.lineno,
                        col=node.col_offset,
                        code=CODE_FORBIDDEN_CALL,
                        message=f"call to forbidden builtin {func.id!r}",
                        module=None,
                    )
                )
        elif isinstance(func, ast.Attribute) and (
            func.attr in _IMPORTLIB_FORBIDDEN_ATTRS
            and isinstance(func.value, ast.Name)
            and func.value.id == "importlib"
        ):
            self._violations.append(
                Violation(
                    line=node.lineno,
                    col=node.col_offset,
                    code=CODE_FORBIDDEN_CALL,
                    message=(
                        f"call to 'importlib.{func.attr}' is forbidden "
                        "(dynamic import)"
                    ),
                    module="importlib",
                )
            )
        self.generic_visit(node)

    # ── Public API ─────────────────────────────────────────────────────

    def validate(self, source: str | bytes) -> ValidationResult:
        """Parse *source* and return a structured :class:`ValidationResult`.

        ``source`` may be ``str`` or ``bytes`` (both accepted by
        :func:`ast.parse`).  A :class:`SyntaxError` is captured as a single
        :data:`SEVERITY_ERROR` violation with code :data:`CODE_SYNTAX_ERROR`
        rather than raised, so the method is **total**: callers always receive
        a result object and can branch on :attr:`ValidationResult.is_valid`
        without a surrounding try/except.
        """
        self._violations = []
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            self._violations.append(
                Violation(
                    line=exc.lineno if exc.lineno is not None else -1,
                    col=exc.offset if exc.offset is not None else -1,
                    code=CODE_SYNTAX_ERROR,
                    message=f"syntax error: {exc.msg}",
                    module=None,
                )
            )
            return ValidationResult(tuple(self._violations))
        self.visit(tree)
        return ValidationResult(tuple(self._violations))


def validate_strategy_source(
    source: str | bytes,
    allowlist: Iterable[str] | None = None,
    denylist: Iterable[str] | None = None,
    forbidden_calls: Iterable[str] | None = None,
) -> ValidationResult:
    """Validate *source* with the given policy and return a :class:`ValidationResult`.

    Convenience wrapper around :class:`ASTValidator` for a one-off validation.
    Reuse an :class:`ASTValidator` instance directly when validating many
    sources to avoid re-constructing the (frozen) policy sets each call.
    """
    return ASTValidator(
        allowlist=allowlist,
        denylist=denylist,
        forbidden_calls=forbidden_calls,
    ).validate(source)
