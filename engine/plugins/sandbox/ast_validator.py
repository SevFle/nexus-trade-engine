"""Layer 1: AST-based import restriction validator for plugin/strategy source.

This module is the static, parse-time complement to the runtime import
enforcement in :class:`~engine.plugins.restricted_importer.RestrictedImporter`.
It parses strategy/plugin source code via the :mod:`ast` module and walks the
resulting tree to detect forbidden imports and dynamic code-execution /
dynamic-import patterns **before** the source is ever compiled or executed.

Why a separate Layer-1 module?
------------------------------
The existing :class:`~engine.plugins.restricted_importer.ImportValidator` is a
flat-list validator bundled inside the heavyweight ``restricted_importer``
module (which imports :mod:`os`, :mod:`socket`, :mod:`sys` at module top level
to install runtime hooks).  This module:

* lives under :mod:`engine.plugins.sandbox` — the natural home for sandbox
  *layers* — and is intentionally import-light (only :mod:`ast` plus the frozen
  allowlist data module), so it can be invoked from the plugin loader / CI gate
  without paying for the runtime-hook machinery;
* returns a **structured** :class:`ValidationResult` carrying rich
  :class:`Violation` records (line, column, offending module, machine-readable
  code, severity) instead of a flat ``list[str]`` — callers (plugin loader,
  structured logs, dashboards, JSON error bodies) can render and react to
  individual violations with full provenance;
* is **total** — :meth:`AstImportValidator.validate` always returns a
  :class:`ValidationResult`, capturing :class:`SyntaxError` as a violation
  rather than raising, so callers never need a separate try/except just to
  obtain a result object.

Detection surface
-----------------
1. ``import <forbidden>``  (every alias / dotted form checked by root).
2. ``from <forbidden> import ...``  (relative imports skipped).
3. Bare-name calls to code-execution builtins: ``__import__``, ``exec``,
   ``eval``, ``compile``.
4. Qualified dynamic import: ``importlib.import_module(...)`` and the
   attribute form ``importlib.__import__(...)``.

By default the forbidden set is the frozen
:data:`~engine.plugins.allowlist.DENYLIST_MODULES` (``os``, ``sys``,
``subprocess``, ``socket``, ``ctypes``, …).  Allowlisted analytics modules
(``math``, ``numpy``, ``polars``, …) are **never** flagged because they are not
members of the denylist — there are no false positives on legitimate imports.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from engine.plugins.allowlist import DENYLIST_MODULES, FROZEN_ALLOWED_MODULES

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "ALLOWED_MODULES",
    "CODE_FORBIDDEN_CALL",
    "CODE_FORBIDDEN_FROM_IMPORT",
    "CODE_FORBIDDEN_IMPORT",
    "CODE_SYNTAX_ERROR",
    "DEFAULT_FORBIDDEN_CALLS",
    "DEFAULT_FORBIDDEN_MODULES",
    "SEVERITY_ERROR",
    "SEVERITY_WARNING",
    "AstImportValidator",
    "ValidationResult",
    "Violation",
    "validate_strategy_source",
]

#: Re-exported so callers can reason about the allowlist in one place.
ALLOWED_MODULES: frozenset[str] = FROZEN_ALLOWED_MODULES

# ── Severity + violation-code vocabulary ──────────────────────────────
#
# Plain strings (rather than an Enum) so values serialise cleanly into
# structured-log payloads and JSON error bodies without a custom encoder —
# matching the convention used in :mod:`engine.legal.scoring_gate`.
SEVERITY_ERROR: str = "error"
SEVERITY_WARNING: str = "warning"

CODE_FORBIDDEN_IMPORT: str = "forbidden-import"
CODE_FORBIDDEN_FROM_IMPORT: str = "forbidden-from-import"
CODE_FORBIDDEN_CALL: str = "forbidden-call"
CODE_SYNTAX_ERROR: str = "syntax-error"

#: The default forbidden *root* module set.  Sourced from the frozen denylist
#: so this validator stays in lock-step with the runtime enforcement and the
#: test-suite escape-vector regression matrix.
DEFAULT_FORBIDDEN_MODULES: frozenset[str] = DENYLIST_MODULES

#: Bare-name builtins that execute arbitrary code or load modules.  Any direct
#: ``Name`` call of one of these is flagged.  ``exec``/``eval``/``compile``
#: are the canonical arbitrary-code-execution vectors; ``__import__`` is the
#: dynamic-import escape hatch that bypasses the static ``import`` statement
#: the runtime hook intercepts.
DEFAULT_FORBIDDEN_CALLS: frozenset[str] = frozenset(
    {"__import__", "exec", "eval", "compile"}
)

#: Attribute names that, when invoked as a qualified call on an ``importlib``
#: object, constitute a dynamic import.  ``import_module`` is the documented
#: public API; ``__import__`` is the underlying builtin reachable through the
#: module object.
_IMPORTLIB_FORBIDDEN_ATTRS: frozenset[str] = frozenset({"import_module", "__import__"})


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
        return f"[{self.code}] {prefix}" if self.code else prefix


@dataclass(frozen=True)
class ValidationResult:
    """Structured outcome of validating a source tree.

    A result is *valid* iff it contains no :data:`SEVERITY_ERROR` violations.
    Warnings (e.g. future deprecations) do not invalidate the result but are
    retained for surfacing.
    """

    violations: tuple[Violation, ...] = field(default_factory=tuple)

    @property
    def is_valid(self) -> bool:
        """``True`` when there are no error-severity violations."""
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
        """Convenience: human-readable messages for every error violation.

        Mirrors the flat ``list[str]`` shape the legacy validator returned, so
        callers migrating off the flat-list API can drop this in directly.
        """
        return [str(v) for v in self.errors()]

    def forbidden_modules(self) -> tuple[str, ...]:
        """Distinct offending *root* module names from import violations.

        De-duplicated by **root** package: ``import os`` and ``import os.path``
        both contribute the root ``os``, so a source smuggling several
        submodules of the same forbidden root collapses to a single entry.
        Useful for structured-log fields (``violations_modules=[...]``) where
        listing the same root module many times would be noisy.
        """
        seen: list[str] = []
        for v in self.violations:
            if not v.module:
                continue
            # Collapse submodules to their root (``os.path`` → ``os``) so a
            # source smuggling several submodules of the same forbidden root
            # de-duplicates to a single entry.
            root = v.module.split(".", maxsplit=1)[0]
            if root not in seen:
                seen.append(root)
        return tuple(seen)


class AstImportValidator(ast.NodeVisitor):
    """Static, parse-time validator that returns a :class:`ValidationResult`.

    Walks the AST of a strategy's Python source and records violations for:

      * ``import`` / ``from … import`` of a forbidden root module, and
      * calls to code-execution / dynamic-import builtins
        (``exec``/``eval``/``compile``/``__import__``/``importlib.import_module``).

    The validator is reusable: :meth:`validate` resets internal state on every
    call, so a single instance can validate many sources sequentially.
    """

    def __init__(
        self,
        forbidden_modules: Iterable[str] | None = None,
        *,
        forbidden_calls: Iterable[str] | None = None,
        allowed: frozenset[str] | None = None,
    ) -> None:
        #: Forbidden *root* module names.  An import is flagged when its root
        #: package matches one of these.  Defaults to the frozen denylist.
        self.forbidden_modules: frozenset[str] = (
            frozenset(forbidden_modules)
            if forbidden_modules is not None
            else DEFAULT_FORBIDDEN_MODULES
        )
        #: Bare-name builtins whose direct invocation is forbidden.  Defaults
        #: to the code-execution / dynamic-import set.
        self.forbidden_calls: frozenset[str] = (
            frozenset(forbidden_calls)
            if forbidden_calls is not None
            else DEFAULT_FORBIDDEN_CALLS
        )
        #: Optional allowlist used purely as defence-in-depth: a forbidden
        #: root can never be re-enabled by the allowlist (denylist always
        #: wins).  Retained for parity with the runtime importer's policy.
        self.allowed: frozenset[str] = allowed if allowed is not None else ALLOWED_MODULES
        #: Accumulated violations for the current :meth:`validate` call.
        #: Reset at the start of every call.
        self._violations: list[Violation] = []

    # ── Module-policy helpers ──────────────────────────────────────────

    @staticmethod
    def _root(module: str) -> str:
        """Return the root package name of a dotted module (``os.path`` → ``os``)."""
        return module.split(".", maxsplit=1)[0]

    def _is_forbidden_module(self, module: str) -> bool:
        """``True`` iff *module*'s root is in the forbidden set.

        The denylist always takes priority over the allowlist: even if a
        future too-permissive allowlist edit added ``os`` to the allowlist, a
        forbidden ``os`` entry here still wins — identical to the precedence
        in :meth:`RestrictedImporter._is_allowed`.
        """
        return self._root(module) in self.forbidden_modules

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
        """Flag ``from <forbidden> import ...`` statements.

        Relative imports (``level > 0``) with no absolute module name are
        skipped: they resolve within the strategy package and carry no
        cross-package escape vector at this layer.
        """
        if node.level and node.level > 0:
            # Pure relative import — no absolute module name to evaluate.
            self.generic_visit(node)
            return
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
        # Qualified dynamic import: ``importlib.import_module(...)`` /
        # ``importlib.__import__(...)``.  Only flagged when the attribute
        # owner is a bare ``importlib`` Name, so legitimate
        # ``some_obj.import_module(...)`` calls on unrelated objects are
        # not false-positived.
        elif (
            isinstance(func, ast.Attribute)
            and func.attr in _IMPORTLIB_FORBIDDEN_ATTRS
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
            return ValidationResult(violations=tuple(self._violations))

        self.visit(tree)
        return ValidationResult(violations=tuple(self._violations))


def validate_strategy_source(
    source: str | bytes,
    *,
    forbidden_modules: Iterable[str] | None = None,
    forbidden_calls: Iterable[str] | None = None,
) -> ValidationResult:
    """Validate *source* with default policy and return a :class:`ValidationResult`.

    Convenience wrapper around :class:`AstImportValidator` for the common case
    of a one-off validation using the frozen denylist.  Reuse an
    :class:`AstImportValidator` instance directly when validating many sources
    to avoid re-constructing the (frozen) policy sets each call.
    """
    validator = AstImportValidator(
        forbidden_modules=forbidden_modules,
        forbidden_calls=forbidden_calls,
    )
    return validator.validate(source)
