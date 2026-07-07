"""Focused unit tests for the static AST import validator.

These tests pin the *pre-execution* security behaviour of
:mod:`engine.sandbox.import_validator`:

  * blocked direct imports of dangerous modules (os, subprocess, socket, ctypes),
  * safe imports of analytics modules (math, numpy, polars) passing unchanged,
  * ``from X import Y`` forms,
  * dotted / nested attribute access (``import os.path``, ``from os.path import``),
  * whitelisted submodules overriding a blocked root,
  * a configurable blocklist (SandboxConfig),
  * error-message clarity (module name + line + column + form).

They are intentionally pure-Python and dependency-free: the validator only
needs ``ast``, so no strategy objects, sandboxes, or network fixtures are
required.
"""

from __future__ import annotations

import textwrap

import pytest

from engine.sandbox.import_validator import (
    DEFAULT_BLOCKED_IMPORTS,
    ImportChecker,
    SandboxConfig,
    SecurityViolation,
    validate_source,
)

# ── Helpers ─────────────────────────────────────────────────────────


def _src(body: str) -> str:
    """Dedent a source snippet so tests can be written flush-left.

    The validator reports 1-based line numbers; :func:`textwrap.dedent`
    preserves the leading line structure (including the opening blank line
    that follows the triple-quote) so ``line`` assertions stay stable and
    map directly onto the readable multi-line layout below.
    """
    return textwrap.dedent(body)


# ── Blocked direct imports ──────────────────────────────────────────


class TestBlockedDirectImports:
    """The headline dangerous modules must be statically rejected."""

    @pytest.mark.parametrize(
        "module",
        ["os", "subprocess", "socket", "ctypes", "sys", "pickle", "threading"],
    )
    def test_import_blocked(self, module: str) -> None:
        with pytest.raises(SecurityViolation) as exc_info:
            validate_source(f"import {module}\n")
        assert exc_info.value.module == module
        assert exc_info.value.form == "import"
        assert exc_info.value.line == 1

    def test_import_alias_still_blocked(self) -> None:
        # Aliasing must not escape the policy — it is the module name that
        # matters, not the local binding.
        with pytest.raises(SecurityViolation, match=r"'os'") as exc:
            validate_source("import os as _safe_name\n")
        assert exc.value.module == "os"

    def test_import_blocked_among_other_statements(self) -> None:
        # The violation must point at the *offending* line, not line 1.
        src = _src(
            """
            import math
            import json

            import subprocess   # ← this is the offender
            """
        )
        with pytest.raises(SecurityViolation) as exc_info:
            validate_source(src)
        assert exc_info.value.module == "subprocess"
        assert exc_info.value.line == 5

    def test_all_default_blocked_modules_rejected(self) -> None:
        # Every module in the default denylist must be statically rejected —
        # guards against a regression that loosens the default config.
        for module in ("os", "subprocess", "socket", "ctypes"):
            assert module in DEFAULT_BLOCKED_IMPORTS
            with pytest.raises(SecurityViolation):
                validate_source(f"import {module}\n")


# ── Nested attribute access (dotted imports) ───────────────────────


class TestNestedAttributeAccess:
    """``import os.path`` and ``from os.path import x`` must be blocked via
    root-package matching."""

    def test_dotted_import_blocked(self) -> None:
        with pytest.raises(SecurityViolation) as exc:
            validate_source("import os.path\n")
        assert exc.value.module == "os.path"
        assert exc.value.form == "import"

    def test_dotted_import_alias_blocked(self) -> None:
        with pytest.raises(SecurityViolation) as exc:
            validate_source("import os.path as pathy\n")
        assert exc.value.module == "os.path"

    def test_deeply_nested_dotted_blocked(self) -> None:
        # Three-level dotted path still resolves to the blocked root "os".
        with pytest.raises(SecurityViolation, match=r"os\.path\.basename"):
            validate_source("import os.path.basename\n")

    def test_ctypes_util_blocked(self) -> None:
        with pytest.raises(SecurityViolation) as exc:
            validate_source("import ctypes.util\n")
        assert exc.value.module == "ctypes.util"


# ── From-import forms ──────────────────────────────────────────────


class TestFromImportForms:
    """``from X import Y`` — the module imported *from* is what is policed."""

    @pytest.mark.parametrize(
        ("module", "name"),
        [
            ("os", "system"),
            ("subprocess", "Popen"),
            ("socket", "socket"),
            ("ctypes", "CDLL"),
            ("shutil", "rmtree"),
        ],
    )
    def test_from_blocked_module(self, module: str, name: str) -> None:
        with pytest.raises(SecurityViolation) as exc:
            validate_source(f"from {module} import {name}\n")
        assert exc.value.module == module
        assert exc.value.form == "from"

    def test_from_dotted_blocked_module(self) -> None:
        with pytest.raises(SecurityViolation) as exc:
            validate_source("from os.path import join\n")
        assert exc.value.module == "os.path"
        assert exc.value.form == "from"

    def test_from_import_alias_blocked(self) -> None:
        with pytest.raises(SecurityViolation, match=r"'subprocess'"):
            validate_source("from subprocess import Popen as run\n")

    def test_from_multiple_names_blocked_once(self) -> None:
        # ``from os import system, getcwd`` is a single ImportFrom node → a
        # single violation is reported (the module, not each imported name).
        checker = ImportChecker()
        checker.visit(__import__("ast").parse("from os import system, getcwd\n"))
        assert len(checker.violations) == 1
        assert checker.violations[0].module == "os"

    def test_relative_import_not_flagged(self) -> None:
        # Relative imports resolve within the strategy package and must not
        # trip the denylist (they cannot reach blocked stdlib modules).
        validate_source("from . import sibling\n")
        validate_source("from .pkg import helper\n")
        validate_source("from ..pkg import helper\n")


# ── Allowed safe imports ───────────────────────────────────────────


class TestAllowedSafeImports:
    """Analytics / safe-stdlib imports must pass the validator untouched."""

    @pytest.mark.parametrize(
        "module",
        ["math", "cmath", "statistics", "decimal", "fractions", "itertools"],
    )
    def test_safe_stdlib_import(self, module: str) -> None:
        # Must not raise — returns None.
        assert validate_source(f"import {module}\n") is None

    @pytest.mark.parametrize("module", ["numpy", "polars", "pandas", "pydantic"])
    def test_safe_third_party_import(self, module: str) -> None:
        assert validate_source(f"import {module}\n") is None

    def test_from_safe_module(self) -> None:
        assert validate_source("from math import sqrt, pi\n") is None

    def test_dotted_safe_submodule_allowed(self) -> None:
        # A submodule of an allowlisted root is not blocked.
        assert validate_source("from collections.abc import Iterable\n") is None
        assert validate_source("import json.decoder\n") is None

    def test_full_safe_strategy_source(self) -> None:
        # A realistic, benign strategy body must validate cleanly.
        src = _src(
            """
            import math
            from dataclasses import dataclass
            import numpy as np
            import polars as pl

            @dataclass
            class Window:
                period: int = 20

            def signal(prices):
                if len(prices) < Window().period:
                    return 0
                return math.sqrt(float(prices[-1]))
            """
        )
        assert validate_source(src) is None


# ── Whitelisted submodules (override allowlist) ────────────────────


class TestWhitelistedSubmodules:
    """An override allowlist entry permits a submodule of a blocked root."""

    def test_whitelisted_submodule_of_blocked_root_allowed(self) -> None:
        # ``xml`` is blocked by default; whitelisting ``xml.etree.ElementTree``
        # must permit both that submodule and its descendants.
        config = SandboxConfig(
            blocked_imports=frozenset({"xml", "os"}),
            allowed_imports=frozenset({"xml.etree.ElementTree"}),
        )
        assert validate_source(
            "import xml.etree.ElementTree\n", config=config
        ) is None
        assert validate_source(
            "from xml.etree.ElementTree import parse\n", config=config
        ) is None
        # A deeper descendant of a whitelisted parent is also allowed.
        assert validate_source(
            "import xml.etree.ElementTree.ElementPath\n", config=config
        ) is None

    def test_whitelisted_parent_unblocks_descendants(self) -> None:
        # Whitelisting a parent package permits nested submodules.
        config = SandboxConfig(
            blocked_imports=frozenset({"xml"}),
            allowed_imports=frozenset({"xml.etree"}),
        )
        assert validate_source(
            "import xml.etree.ElementTree\n", config=config
        ) is None

    def test_whitelisted_submodule_does_not_unblock_sibling(self) -> None:
        config = SandboxConfig(
            blocked_imports=frozenset({"xml"}),
            allowed_imports=frozenset({"xml.etree.ElementTree"}),
        )
        # ``xml.dom`` shares the blocked root but is NOT in the override set.
        with pytest.raises(SecurityViolation) as exc:
            validate_source("import xml.dom\n", config=config)
        assert exc.value.module == "xml.dom"

    def test_whitelisted_exact_submodule_still_blocks_root(self) -> None:
        config = SandboxConfig(
            blocked_imports=frozenset({"os"}),
            allowed_imports=frozenset({"os.path"}),
        )
        # The override lifts only os.path, not the os root itself.
        assert validate_source("import os.path\n", config=config) is None
        with pytest.raises(SecurityViolation) as exc:
            validate_source("import os\n", config=config)
        assert exc.value.module == "os"


# ── Configurable blocklist ─────────────────────────────────────────


class TestConfigurableBlocklist:
    """The blocklist is driven entirely by SandboxConfig."""

    def test_custom_blocklist_rejects_custom_module(self) -> None:
        # A module absent from the default denylist is blocked once added.
        config = SandboxConfig(blocked_imports=frozenset({"custom_danger"}))
        with pytest.raises(SecurityViolation) as exc:
            validate_source("import custom_danger\n", config=config)
        assert exc.value.module == "custom_danger"

    def test_empty_blocklist_allows_everything(self) -> None:
        # An empty denylist disables static blocking — the validator becomes
        # a pure passthrough (runtime layer still applies).
        config = SandboxConfig(blocked_imports=frozenset())
        assert validate_source("import os\nimport subprocess\n", config=config) is None

    def test_default_config_blocks_os(self) -> None:
        # Omitting config yields the standard dangerous-module policy.
        with pytest.raises(SecurityViolation, match=r"'os'"):
            validate_source("import os\n")

    def test_config_is_immutable_frozenset(self) -> None:
        # ``SandboxConfig`` is frozen — defensive against accidental mutation
        # of a shared policy object between strategy validations.
        config = SandboxConfig()
        with pytest.raises(Exception):  # noqa: B017 — dataclass FrozenInstanceError
            config.blocked_imports = frozenset()  # type: ignore[misc]


# ── Multiple violations & checker API ──────────────────────────────


class TestCheckerAccumulation:
    """The checker collects *all* violations; the helper raises the first."""

    def test_multiple_violations_collected_in_order(self) -> None:
        src = _src(
            """
            import os
            import math
            import subprocess
            """
        )
        checker = ImportChecker()
        checker.visit(__import__("ast").parse(src))
        modules = [v.module for v in checker.iter_violations()]
        # ``math`` is not blocked → exactly the two dangerous ones remain,
        # in source order.
        assert modules == ["os", "subprocess"]

    def test_validate_source_raises_earliest(self) -> None:
        src = _src(
            """
            import os
            import subprocess
            import socket
            """
        )
        with pytest.raises(SecurityViolation) as exc:
            validate_source(src)
        # The first line (os) is surfaced, not the last.
        assert exc.value.module == "os"
        assert exc.value.line == 2


# ── Error message clarity ──────────────────────────────────────────


class TestErrorMessageClarity:
    """The exception must name the module, the form, and locate the line."""

    def test_message_contains_module_name(self) -> None:
        with pytest.raises(SecurityViolation) as exc:
            validate_source("import subprocess\n")
        msg = str(exc.value)
        assert "subprocess" in msg

    def test_message_states_form(self) -> None:
        with pytest.raises(SecurityViolation) as exc:
            validate_source("from os import system\n")
        msg = str(exc.value)
        assert "from" in msg  # form: "from"

    def test_message_reports_line_and_column(self) -> None:
        with pytest.raises(SecurityViolation) as exc:
            validate_source("\n\nfrom socket import socket\n")
        v = exc.value
        assert v.line == 3
        assert v.col == 0
        msg = str(v.value) if False else str(v)
        assert "line 3" in msg

    def test_attributes_exposed(self) -> None:
        # Programmatic consumers (e.g. the plugin loader surfacing a
        # structured error) rely on these attributes.
        with pytest.raises(SecurityViolation) as exc:
            validate_source("import ctypes\n")
        v = exc.value
        assert v.module == "ctypes"
        assert v.form == "import"
        assert isinstance(v.line, int)
        assert isinstance(v.col, int)

    def test_security_violation_is_exception(self) -> None:
        # Must be catchable as a plain Exception by callers that don't
        # import the specific type.
        with pytest.raises(Exception):  # noqa: B017
            validate_source("import os\n")


# ── Syntax handling ────────────────────────────────────────────────


class TestSyntaxHandling:
    def test_invalid_syntax_propagates_syntax_error(self) -> None:
        # A malformed plugin is a programmer error, not a security event:
        # the SyntaxError surfaces unchanged (not wrapped in SecurityViolation).
        with pytest.raises(SyntaxError):
            validate_source("import (broken\n")

    def test_empty_source_is_clean(self) -> None:
        assert validate_source("") is None

    def test_comment_only_source_is_clean(self) -> None:
        # ``# import os`` in a comment must NOT be flagged — AST parsing
        # naturally ignores comments, but pin it as a regression guard.
        assert validate_source("# import os\n# from socket import socket\n") is None

    def test_string_literal_not_flagged(self) -> None:
        # An import statement embedded in a string literal is not real code.
        assert validate_source('msg = "import os"\n') is None
