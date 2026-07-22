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
5. **Wildcard from-imports** — ``from … import *`` is always rejected because
   its bound names cannot be enumerated statically, and relative from-imports
   are checked at the *module root* only (imported names are not, since they
   may be local submodules or functions rather than modules).
"""

from __future__ import annotations

import pytest

from engine.plugins.sandbox.ast_validator import (
    CODE_FORBIDDEN_CALL,
    CODE_FORBIDDEN_FROM_IMPORT,
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


# ── 5. Wildcard + relative from-import handling ───────────────────────


def test_wildcard_relative_import_rejected() -> None:
    """``from . import *`` cannot be enumerated statically → blocked outright.

    The validator has no way to know which names a wildcard binds, so it must
    reject the form outright rather than risk pulling in a forbidden name.
    """
    validator = ASTValidator(allowlist=frozenset(), denylist=frozenset())

    result = validator.validate("from . import *")
    assert not result.is_valid
    assert result.has_errors
    assert any(
        v.code == CODE_FORBIDDEN_FROM_IMPORT for v in result.errors()
    ), result.error_messages()


def test_wildcard_relative_import_from_submodule_rejected() -> None:
    """``from .helpers import *`` is likewise rejected (relative wildcard)."""
    validator = ASTValidator(allowlist=frozenset(), denylist=frozenset())

    result = validator.validate("from .helpers import *")
    assert not result.is_valid
    assert any(v.code == CODE_FORBIDDEN_FROM_IMPORT for v in result.errors())


def test_wildcard_absolute_import_rejected() -> None:
    """The wildcard rule applies to absolute from-imports too.

    Even ``from math import *`` of an *allowed* module is rejected: the
    wildcard is the offence, not the module.
    """
    validator = ASTValidator(allowlist={"math"}, denylist=frozenset())

    result = validator.validate("from math import *")
    assert not result.is_valid
    assert any(v.code == CODE_FORBIDDEN_FROM_IMPORT for v in result.errors())


def test_relative_import_local_submodule_name_allowed() -> None:
    """``from . import config`` imports a local submodule, not a module → allowed.

    The imported *name* (``config``) is intentionally not checked against the
    policy: it denotes a local submodule (or function) rather than a top-level
    module, so it must never trip the denylist/allowlist gate.
    """
    validator = ASTValidator(allowlist=frozenset(), denylist=frozenset())

    assert validator.validate("from . import config").is_valid


def test_relative_from_submodule_function_name_allowed() -> None:
    """``from .utils import helper_func`` checks the module root, not the name.

    Only the *module* (``utils``) is policy-checked; the imported *name*
    (``helper_func``) is a local function and is left untouched.
    """
    validator = ASTValidator(allowlist=frozenset(), denylist=frozenset())

    assert validator.validate("from .utils import helper_func").is_valid


def test_relative_import_does_not_check_imported_names() -> None:
    """Imported *names* in a relative import are never policy-checked.

    Even a name that collides with a denied module root (``os``) is fine when
    it refers to a local submodule/function rather than the stdlib module.
    """
    # ``os`` is denied, but as an imported *name* (not a module) it is local.
    validator = ASTValidator(allowlist=frozenset(), denylist={"os"})

    assert validator.validate("from . import os").is_valid
    assert validator.validate("from .pkg import os").is_valid


def test_relative_from_import_module_root_checked_consistently() -> None:
    """The module root of a relative from-import is checked like an absolute one.

    This is the *consistency* guarantee: a relative ``from .<root> import …``
    is policy-checked on ``<root>`` exactly as an absolute ``from <root>``
    would be, eliminating the previous asymmetry where level-1 imports were
    skipped entirely.
    """
    validator = ASTValidator(allowlist=frozenset(), denylist={"subprocess"})

    # Relative form.
    rel = validator.validate("from .subprocess import run")
    assert not rel.is_valid
    assert any(
        v.code == CODE_FORBIDDEN_FROM_IMPORT and v.module == "subprocess"
        for v in rel.errors()
    )
    # Absolute form — same outcome.
    abs_result = validator.validate("from subprocess import run")
    assert not abs_result.is_valid
    assert any(
        v.code == CODE_FORBIDDEN_FROM_IMPORT and v.module == "subprocess"
        for v in abs_result.errors()
    )
