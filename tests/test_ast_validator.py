"""Tests for :mod:`engine.plugins.sandbox.ast_validator`.

Covers the four contract areas of :class:`ASTValidator`:

1. **Allowlist enforcement** — a non-empty allowlist rejects unlisted modules;
   an empty allowlist permits anything not on the denylist.
2. **Denylist precedence** — the denylist always wins over the allowlist.
3. **Forbidden-call detection** — ``exec`` / ``eval`` / ``compile`` /
   ``__import__`` (plus the ``importlib.import_module`` dynamic-import form)
   are flagged.
4. **Relative-import handling** — a level-1 ``from .`` import is allowed, while
   a level >= 2 ``from ..`` import (which escapes the strategy package) is
   flagged.
"""

from __future__ import annotations

import pytest

from engine.plugins.sandbox.ast_validator import (
    CODE_FORBIDDEN_CALL,
    CODE_FORBIDDEN_IMPORT,
    CODE_RELATIVE_IMPORT,
    ASTValidator,
)

# ── 1. Allowlist enforcement ──────────────────────────────────────────


def test_allowlist_blocks_unlisted_module() -> None:
    """A non-empty allowlist rejects a module that is not listed."""
    validator = ASTValidator(allowlist={"math", "json"}, denylist=frozenset())

    # ``re`` is neither denied nor allow-listed → blocked by the allowlist gate.
    result = validator.validate("import re")
    assert not result.is_valid
    assert result.has_errors
    assert any(v.code == CODE_FORBIDDEN_IMPORT and v.module == "re" for v in result.errors())


def test_empty_allowlist_permits_non_denylisted() -> None:
    """An empty allowlist is permissive: only the denylist can block."""
    validator = ASTValidator(allowlist=frozenset(), denylist={"os"})

    # ``json`` is not denied and the allowlist gate is a no-op → permitted.
    assert validator.validate("import json").is_valid

    # ``os`` is on the denylist → still blocked.
    os_result = validator.validate("import os")
    assert not os_result.is_valid
    assert os_result.error_count == 1


# ── 2. Denylist precedence ────────────────────────────────────────────


def test_denylist_overrides_allowlist() -> None:
    """A module present on *both* lists is blocked: the denylist wins."""
    validator = ASTValidator(allowlist={"os"}, denylist={"os"})

    result = validator.validate("import os")
    assert not result.is_valid
    assert result.has_errors
    assert any(v.module == "os" for v in result.errors())


# ── 3. Forbidden-call detection ───────────────────────────────────────


@pytest.mark.parametrize(
    "call_src",
    [
        'exec("1 + 1")',
        'eval("1 + 1")',
        'compile("1 + 1", "<sandbox>", "exec")',
        '__import__("os")',
    ],
    ids=["exec", "eval", "compile", "__import__"],
)
def test_forbidden_calls_detected(call_src: str) -> None:
    """Each code-execution / dynamic-import builtin call is flagged."""
    # Empty denylist + empty allowlist so the *only* reason to block is the call.
    validator = ASTValidator(allowlist=frozenset(), denylist=frozenset())

    result = validator.validate(call_src)
    assert not result.is_valid
    assert result.has_errors
    assert any(v.code == CODE_FORBIDDEN_CALL for v in result.errors())


def test_importlib_dynamic_import_flagged() -> None:
    """``importlib.import_module`` bypasses the static import and is flagged."""
    validator = ASTValidator(allowlist=frozenset(), denylist=frozenset())

    result = validator.validate('importlib.import_module("os")')
    assert not result.is_valid
    assert any(v.code == CODE_FORBIDDEN_CALL for v in result.errors())


# ── 4. Relative-import handling ───────────────────────────────────────


def test_relative_import_level_1_allowed() -> None:
    """A level-1 relative import (``from .``) stays within the package."""
    validator = ASTValidator(allowlist=frozenset(), denylist=frozenset())

    assert validator.validate("from . import helpers").is_valid
    assert validator.validate("from .helpers import thing").is_valid


def test_relative_import_level_above_1_flagged() -> None:
    """A level >= 2 relative import escapes the package and is flagged."""
    validator = ASTValidator(allowlist=frozenset(), denylist=frozenset())

    result = validator.validate("from .. import sibling")
    assert not result.is_valid
    assert any(v.code == CODE_RELATIVE_IMPORT for v in result.errors())

    # Deeper levels are flagged too.
    deeper = validator.validate("from ...pkg import mod")
    assert not deeper.is_valid


# ── Baseline: normal imports ──────────────────────────────────────────


def test_normal_import_allowed() -> None:
    """A normal ``import`` of an allow-listed module is permitted."""
    validator = ASTValidator(allowlist={"math", "json"}, denylist=frozenset())

    assert validator.validate("import math").is_valid
    assert validator.validate("import json").is_valid
