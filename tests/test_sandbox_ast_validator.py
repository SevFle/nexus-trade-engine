"""Unit tests for the Layer-1 AST-based import restriction validator.

Covers :mod:`engine.plugins.sandbox.ast_validator`:

* **clean code passes** — strategy source importing only allowlisted analytics
  modules (``math``, ``numpy``, ``polars``, ``pandas``, ``datetime`` …) yields a
  valid result with zero violations;
* **single forbidden import detected** — ``import os`` produces exactly one
  violation with the right code, line and offending module;
* **multiple violations listed** — a source smuggling several forbidden
  modules produces one violation per offending import, in source order;
* **dynamic import patterns caught** — ``__import__``, ``importlib.import_module``
  and the code-execution builtins ``exec`` / ``eval`` / ``compile`` are all
  flagged;
* **no false positives on allowed imports** — analytics modules never appear in
  the violation set because they are not members of the frozen denylist.

Plus structural / robustness cases: ``from … import`` form, dotted + aliased
imports, submodules of a forbidden root, relative imports skipped, validator
state reset across calls, syntax errors captured as violations, and custom
policy sets.
"""

from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError

import pytest

from engine.plugins.allowlist import DENYLIST_MODULES
from engine.plugins.sandbox.ast_validator import (
    ALLOWED_MODULES,
    CODE_FORBIDDEN_CALL,
    CODE_FORBIDDEN_FROM_IMPORT,
    CODE_FORBIDDEN_IMPORT,
    CODE_SYNTAX_ERROR,
    DEFAULT_FORBIDDEN_CALLS,
    DEFAULT_FORBIDDEN_MODULES,
    SEVERITY_ERROR,
    AstImportValidator,
    ValidationResult,
    Violation,
    validate_strategy_source,
)

# A representative slice of the frozen denylist — the names called out in the
# task (``os``, ``sys``, ``subprocess``, ``socket``, ``ctypes``) plus a few more
# escape-vector staples.  Asserted against the real default so the tests stay
# meaningful if the denylist grows.
_FORBIDDEN_SAMPLE = ["os", "sys", "subprocess", "socket", "ctypes", "pickle", "threading"]


class TestCleanCodePasses:
    """A strategy that imports only allowlisted modules is valid."""

    def test_allowed_analytics_imports_are_clean(self) -> None:
        source = (
            "import math\n"
            "import numpy as np\n"
            "import polars as pl\n"
            "import pandas as pd\n"
            "from datetime import datetime\n"
        )
        result = validate_strategy_source(source)

        assert result.is_valid
        assert result.violations == ()
        assert result.error_count == 0
        assert not result.has_errors

    def test_clean_result_error_messages_is_empty(self) -> None:
        result = validate_strategy_source("import math\nx = math.sqrt(16)\n")
        assert result.error_messages() == []

    def test_realistic_strategy_body_is_clean(self) -> None:
        # A small but realistic strategy body using only safe modules.
        source = (
            "import numpy as np\n"
            "import polars as pl\n"
            "from dataclasses import dataclass\n"
            "\n"
            "\n"
            "@dataclass\n"
            "class Strategy:\n"
            "    name: str = 'demo'\n"
            "    version: str = '1.0.0'\n"
            "\n"
            "    def on_bar(self, state, portfolio):\n"
            "        window = np.mean(state.closes[-20:])\n"
            "        return []\n"
        )
        result = validate_strategy_source(source)
        assert result.is_valid
        assert result.forbidden_modules() == ()


class TestNoFalsePositivesOnAllowedImports:
    """The canonical allowlisted analytics modules must never be flagged."""

    @pytest.mark.parametrize("module", ["math", "cmath", "statistics", "decimal"])
    def test_pure_math_modules(self, module: str) -> None:
        result = validate_strategy_source(f"import {module}\n")
        assert result.is_valid, f"false positive on {module!r}: {result.error_messages()}"

    @pytest.mark.parametrize("module", ["numpy", "polars", "pandas", "pydantic"])
    def test_third_party_analytics_modules(self, module: str) -> None:
        result = validate_strategy_source(f"import {module}\n")
        assert result.is_valid, f"false positive on {module!r}: {result.error_messages()}"

    def test_allowed_modules_with_aliases(self) -> None:
        result = validate_strategy_source(
            "import numpy as np\nimport polars as pl\nimport pandas as pd\n"
        )
        assert result.is_valid

    def test_from_import_of_allowed_module(self) -> None:
        result = validate_strategy_source(
            "from numpy import array, mean\nfrom polars import DataFrame\n"
        )
        assert result.is_valid

    def test_allowed_module_not_in_forbidden_set(self) -> None:
        # The guard against false positives is structural: a module is flagged
        # only if its root is in the forbidden set.  Allowlisted analytics
        # modules must not be members of the denylist.
        for module in ("math", "numpy", "polars"):
            assert module not in DEFAULT_FORBIDDEN_MODULES


class TestSingleForbiddenImportDetected:
    """Exactly one violation for a single forbidden import."""

    def test_import_os_produces_single_violation(self) -> None:
        result = validate_strategy_source("import os\n")

        assert not result.is_valid
        assert result.error_count == 1

        violation = result.errors()[0]
        assert violation.code == CODE_FORBIDDEN_IMPORT
        assert violation.module == "os"
        assert violation.line == 1
        assert violation.severity == SEVERITY_ERROR
        assert "os" in violation.message

    @pytest.mark.parametrize("module", _FORBIDDEN_SAMPLE)
    def test_each_canonical_forbidden_module_flagged(self, module: str) -> None:
        result = validate_strategy_source(f"import {module}\n")

        assert not result.is_valid
        assert result.error_count == 1
        assert result.errors()[0].module == module

    def test_violation_line_number_is_accurate(self) -> None:
        # The forbidden import is on line 3, not line 1.
        source = "import math\nimport numpy as np\nimport os\nimport polars as pl\n"
        result = validate_strategy_source(source)

        assert result.error_count == 1
        assert result.errors()[0].line == 3
        assert result.errors()[0].col >= 0

    def test_aliased_forbidden_import_detected(self) -> None:
        result = validate_strategy_source("import subprocess as sp\n")

        assert not result.is_valid
        assert result.errors()[0].module == "subprocess"

    def test_dotted_forbidden_root_detected(self) -> None:
        # ``os.path`` is a submodule of the forbidden ``os`` root.
        result = validate_strategy_source("import os.path\n")

        assert not result.is_valid
        assert result.errors()[0].module == "os.path"

    def test_from_import_form_detected(self) -> None:
        result = validate_strategy_source("from socket import socket\n")

        assert not result.is_valid
        assert result.error_count == 1
        violation = result.errors()[0]
        assert violation.code == CODE_FORBIDDEN_FROM_IMPORT
        assert violation.module == "socket"


class TestMultipleViolationsListed:
    """Several forbidden imports each yield their own violation, in order."""

    def test_three_forbidden_imports_listed_in_source_order(self) -> None:
        source = "import os\nimport subprocess\nimport socket\n"
        result = validate_strategy_source(source)

        assert not result.is_valid
        assert result.error_count == 3
        assert [v.module for v in result.errors()] == ["os", "subprocess", "socket"]
        # Source order is preserved: lines 1, 2, 3.
        assert [v.line for v in result.errors()] == [1, 2, 3]

    def test_mixed_import_and_from_import_violations(self) -> None:
        source = (
            "import os\n"
            "import math\n"  # allowed — must NOT appear
            "from subprocess import Popen\n"
            "import ctypes\n"
        )
        result = validate_strategy_source(source)

        assert result.error_count == 3
        assert [v.module for v in result.errors()] == ["os", "subprocess", "ctypes"]
        codes = {v.code for v in result.errors()}
        assert CODE_FORBIDDEN_IMPORT in codes
        assert CODE_FORBIDDEN_FROM_IMPORT in codes

    def test_forbidden_modules_de_duplicated(self) -> None:
        # Two imports of the same root produce two violations, but
        # ``forbidden_modules()`` de-duplicates the offending roots.
        source = "import os\nimport os.path\nfrom os import environ\n"
        result = validate_strategy_source(source)

        assert result.error_count == 3
        assert result.forbidden_modules() == ("os",)

    def test_mixed_allowed_and_forbidden_keeps_order(self) -> None:
        source = (
            "import numpy as np\n"
            "import os\n"
            "import polars as pl\n"
            "import sys\n"
        )
        result = validate_strategy_source(source)

        assert result.error_count == 2
        assert [v.module for v in result.errors()] == ["os", "sys"]


class TestDynamicImportPatternsCaught:
    """Dynamic import / code-execution escapes are flagged statically."""

    @pytest.mark.parametrize(
        ("call", "label"),
        [
            ("exec('1+1')", "exec"),
            ("eval('1+1')", "eval"),
            ("compile('x', '<f>', 'exec')", "compile"),
            ("__import__('os')", "__import__"),
        ],
    )
    def test_forbidden_builtin_call(self, call: str, label: str) -> None:
        result = validate_strategy_source(f"{call}\n")

        assert not result.is_valid
        assert result.error_count == 1
        violation = result.errors()[0]
        assert violation.code == CODE_FORBIDDEN_CALL
        assert label in violation.message

    def test_importlib_import_module_caught(self) -> None:
        source = "import importlib\nimportlib.import_module('os')\n"
        result = validate_strategy_source(source)

        assert not result.is_valid
        assert result.error_count == 1
        violation = result.errors()[0]
        assert violation.code == CODE_FORBIDDEN_CALL
        assert violation.module == "importlib"
        assert "import_module" in violation.message

    def test_importlib_dunder_import_caught(self) -> None:
        # ``importlib.__import__`` is the underlying builtin reachable via the
        # module object — also a dynamic-import escape vector.
        source = "import importlib\nimportlib.__import__('os')\n"
        result = validate_strategy_source(source)

        assert not result.is_valid
        assert result.error_count == 1
        assert result.errors()[0].module == "importlib"

    def test_all_dynamic_patterns_combined(self) -> None:
        source = (
            "exec('1')\n"
            "eval('2')\n"
            "compile('3', '<f>', 'exec')\n"
            "__import__('os')\n"
            "import importlib\n"
            "importlib.import_module('subprocess')\n"
        )
        result = validate_strategy_source(source)

        assert not result.is_valid
        assert result.error_count == 5
        # Each offending call is on its own line, in order.
        assert [v.line for v in result.errors()] == [1, 2, 3, 4, 6]

    def test_unrelated_import_module_call_not_flagged(self) -> None:
        # ``some_loader.import_module(...)`` on a non-``importlib`` object must
        # NOT be a false positive — only the ``importlib.`` owner is flagged.
        source = (
            "class Loader:\n"
            "    def import_module(self, name):\n"
            "        return name\n"
            "loader = Loader()\n"
            "loader.import_module('whatever')\n"
        )
        result = validate_strategy_source(source)
        assert result.is_valid, result.error_messages()


class TestRelativeImportsSkipped:
    """Relative imports resolve within the package — no escape vector here."""

    def test_relative_import_not_flagged(self) -> None:
        # ``from . import helpers`` has ``level=1`` and no absolute module.
        result = validate_strategy_source("from . import helpers\nfrom ..utils import log\n")
        assert result.is_valid

    def test_relative_import_of_forbidden_name_not_flagged(self) -> None:
        # Even a relative import whose tail name collides with a forbidden
        # module is skipped — the ``.os`` here is a sibling module, not stdlib.
        result = validate_strategy_source("from . import os\n")
        assert result.is_valid


class TestValidatorStateReset:
    """A reusable validator must not leak violations between calls."""

    def test_repeated_validate_calls_are_independent(self) -> None:
        validator = AstImportValidator()

        first = validator.validate("import os\n")
        second = validator.validate("import math\n")  # clean
        third = validator.validate("import subprocess\n")

        assert not first.is_valid
        assert second.is_valid  # no leak from the first call
        assert not third.is_valid
        assert third.error_count == 1
        assert third.errors()[0].module == "subprocess"

    def test_validator_reuse_across_many_sources(self) -> None:
        validator = AstImportValidator()
        for source in ("import os\n", "import sys\n", "import socket\n"):
            result = validator.validate(source)
            assert result.error_count == 1


class TestSyntaxErrorCapturedAsViolation:
    """``validate`` is total — syntax errors become violations, not raises."""

    def test_syntax_error_does_not_raise(self) -> None:
        result = validate_strategy_source("def broken(:\n    pass\n")
        assert not result.is_valid
        assert result.error_count == 1
        violation = result.errors()[0]
        assert violation.code == CODE_SYNTAX_ERROR
        assert violation.severity == SEVERITY_ERROR
        assert "syntax error" in violation.message.lower()

    def test_syntax_error_has_line_info_when_available(self) -> None:
        result = validate_strategy_source("x = (\n")
        assert not result.is_valid
        # ``ast.parse`` populates lineno/offset for most syntax errors.
        assert result.errors()[0].line >= 1


class TestValidationResultShape:
    """The structured result object exposes a useful, documented API."""

    def test_empty_result_is_valid(self) -> None:
        result = ValidationResult()
        assert result.is_valid
        assert result.error_count == 0
        assert result.errors() == ()
        assert result.error_messages() == []
        assert result.forbidden_modules() == ()
        assert not result.has_errors

    def test_warning_does_not_invalidate(self) -> None:
        warning = Violation(
            line=1,
            col=0,
            code="future-deprecation",
            message="heads up",
            severity="warning",
        )
        result = ValidationResult(violations=(warning,))

        assert result.is_valid  # warnings are non-fatal
        assert not result.has_errors
        assert result.has_warnings
        assert result.error_count == 0

    def test_errors_and_warnings_partitioned(self) -> None:
        error = Violation(line=1, col=0, code=CODE_FORBIDDEN_IMPORT, message="m1")
        warning = Violation(
            line=2, col=0, code="x", message="m2", severity="warning"
        )
        result = ValidationResult(violations=(error, warning))

        assert not result.is_valid
        assert result.has_errors and result.has_warnings
        assert result.error_count == 1
        assert result.errors() == (error,)

    def test_violation_is_frozen(self) -> None:
        violation = Violation(line=1, col=0, code=CODE_FORBIDDEN_IMPORT, message="m")
        with pytest.raises(FrozenInstanceError):
            violation.line = 99  # type: ignore[misc]

    def test_violation_str_includes_code_line_and_message(self) -> None:
        violation = Violation(
            line=3,
            col=0,
            code=CODE_FORBIDDEN_IMPORT,
            message="import of forbidden module 'os'",
            module="os",
        )
        rendered = str(violation)
        assert "forbidden-import" in rendered
        assert "line 3" in rendered
        assert "'os'" in rendered


class TestCustomPolicy:
    """Callers can supply their own forbidden / allowed sets."""

    def test_custom_forbidden_set(self) -> None:
        # Only ``math`` is forbidden under this custom policy.
        validator = AstImportValidator(forbidden_modules={"math"})
        forbidden = validator.validate("import math\n")
        allowed = validator.validate("import os\n")  # not in the custom set

        assert not forbidden.is_valid
        assert allowed.is_valid  # os is allowed under the custom policy

    def test_custom_forbidden_calls(self) -> None:
        validator = AstImportValidator(forbidden_calls={"print"})
        result = validator.validate("print('hi')\n")

        assert not result.is_valid
        assert result.errors()[0].code == CODE_FORBIDDEN_CALL

    def test_defaults_are_frozen_denylist(self) -> None:
        validator = AstImportValidator()
        assert validator.forbidden_modules == DEFAULT_FORBIDDEN_MODULES == DENYLIST_MODULES
        assert validator.forbidden_calls == DEFAULT_FORBIDDEN_CALLS
        assert validator.allowed == ALLOWED_MODULES


class TestAstNodeVisitorContract:
    """The validator honours the ``ast.NodeVisitor`` contract."""

    def test_validator_is_a_node_visitor(self) -> None:
        assert isinstance(AstImportValidator(), ast.NodeVisitor)

    def test_validate_accepts_bytes_source(self) -> None:
        result = validate_strategy_source(b"import os\n")
        assert not result.is_valid
        assert result.errors()[0].module == "os"

    def test_nested_function_body_is_walked(self) -> None:
        # The forbidden import is nested inside a function — the visitor must
        # descend into it (``generic_visit``) rather than stopping at the top.
        source = (
            "class Strategy:\n"
            "    def on_bar(self, state, portfolio):\n"
            "        import subprocess\n"
            "        return []\n"
        )
        result = validate_strategy_source(source)

        assert not result.is_valid
        assert result.error_count == 1
        assert result.errors()[0].line == 3
        assert result.errors()[0].module == "subprocess"
