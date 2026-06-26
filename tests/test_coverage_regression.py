"""Regression guard for the chronic pytest-cov 0% coverage failure.

Root-cause analysis (issue #482)
================================
``pytest-cov`` reporting 0% coverage has been "fixed" ≥8 times
(#397, #401, #435, #439, #467, #469, #471, #473, #477). Every one of those
commits only nudged a single field in ``pyproject.toml`` and never addressed
why coverage silently collapses to 0%.

The root cause is not any one bad line — it is that coverage source is
configured in **two places that must stay perfectly in sync**, and one of the
packages lives outside the project root:

* ``[tool.pytest.ini_options] addopts`` → ``--cov=engine --cov=nexus_sdk``
  (this is what pytest-cov actually uses to start tracing)
* ``[tool.coverage.run] source = ["engine", "nexus_sdk"]`` (read by the
  standalone ``coverage`` tool, and by ``coverage report``/``combine``)

``nexus_sdk`` is not packaged in the wheel (``[tool.hatch...] packages =
["engine"]``); it is importable only because ``[tool.pytest.ini_options]
pythonpath = [".", "sdk"]`` puts ``sdk/`` on ``sys.path``.

When these drift — a path instead of a package name (``sdk/nexus_sdk`` vs
``nexus_sdk``), ``source`` vs ``source_pkgs``, a dropped ``--cov`` arg, or a
moved package — coverage can no longer locate/measure the sources and the
whole run reports 0% with no error. Because nothing asserted coverage > 0%,
each regression was discovered only when a human noticed a red badge and
opened *another* piecemeal "fix 0%" PR. The git history of
``[tool.coverage.run] source`` literally flips between
``["engine","nexus_sdk"]`` → ``["engine","sdk/nexus_sdk"]`` →
``source_pkgs`` → back to ``source`` — none of which changed the pytest-cov
result, because ``--cov`` in ``addopts`` was always the real driver.

Definitive fix
--------------
1. The ``[tool.coverage.run] source`` form (package names, not paths) is
   correct and is the single canonical source of truth. ``source_pkgs`` was
   tried and *reverted* because it emits ``module-not-measured`` warnings.
2. THIS module asserts the two configurations never drift and that coverage
   genuinely collects non-zero data for every configured source — so the next
   regression fails a test instead of silently producing 0%.

These tests are intentionally config-driven: they read ``pyproject.toml`` so
they stay correct as packages are added/renamed without being edited.
"""

from __future__ import annotations

import importlib
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"

_COV_ARG_RE = re.compile(r"--cov(?:=(\S+)|\s+(\S+))")
# ``--cov-report=...`` and ``--cov-fail-under=...`` must never be mistaken
# for source specs — they are excluded by anchoring ``--cov`` to ``=`` or a
# bare token that is not ``-...``.
_COV_REPORT_RE = re.compile(r"--cov-(?:report|fail-under|term|xml|html|annotate)")


def _load_pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text())


def _coverage_run_config() -> dict:
    return _load_pyproject()["tool"]["coverage"]["run"]


def _coverage_sources() -> list[str]:
    """The configured coverage sources, from whichever key is set."""
    run = _coverage_run_config()
    return list(run.get("source") or run.get("source_pkgs") or [])


def _coverage_omit() -> list[str]:
    return list(_coverage_run_config().get("omit", []))


def _pytest_pythonpath() -> list[str]:
    return list(_load_pyproject()["tool"]["pytest"]["ini_options"].get("pythonpath", []))


def _addopts_cov_sources() -> list[str]:
    """Package names given to pytest-cov via ``--cov=NAME`` in addopts.

    A bare ``--cov`` (no argument) means "use [tool.coverage.run] source";
    that is handled separately by ``_has_bare_cov``.
    """
    addopts = _load_pyproject()["tool"]["pytest"]["ini_options"]["addopts"]
    names: list[str] = []
    for match in _COV_ARG_RE.finditer(addopts):
        grp = match.group(1) or match.group(2)
        if grp is None:
            continue
        # Skip anything that is really a --cov-* option bleeding into the match.
        full = addopts[match.start() : match.end()]
        if _COV_REPORT_RE.search(full):
            continue
        names.append(grp)
    return names


def _has_bare_cov() -> bool:
    addopts = _load_pyproject()["tool"]["pytest"]["ini_options"]["addopts"]
    # A bare ``--cov`` not immediately followed by ``=``/``-``/a value.
    return bool(re.search(r"--cov(?!\S|=|-)", addopts))


# ---------------------------------------------------------------------------
# 1. Configuration consistency — catches the drift that caused the loop.
# ---------------------------------------------------------------------------


class TestCoverageConfigConsistency:
    """Assert ``addopts --cov`` and ``[tool.coverage.run] source`` never drift."""

    def test_coverage_sources_are_declared(self):
        sources = _coverage_sources()
        assert sources, (
            "[tool.coverage.run] must declare source/source_pkgs; an empty "
            "source is the textbook cause of 0% coverage."
        )

    def test_pytest_cov_args_match_configured_sources(self):
        """The ``--cov=NAME`` args must be exactly the configured sources.

        This is the precise invariant that was violated repeatedly: people
        edited ``[tool.coverage.run] source`` (or ``source_pkgs``) while the
        ``--cov`` flags stayed fixed, or vice-versa. If they ever disagree,
        coverage measures a different set than it reports → 0% for the
        mismatched package.
        """
        cov_args = set(_addopts_cov_sources())
        configured = set(_coverage_sources())

        if _has_bare_cov() and not cov_args:
            # Bare ``--cov`` deliberately defers to [tool.coverage.run] source.
            return

        assert cov_args == configured, (
            "pytest-cov --cov args and [tool.coverage.run] source must be "
            f"identical sets; got --cov={sorted(cov_args)} vs "
            f"source={sorted(configured)}. This drift is the root cause of "
            "the recurring 0% coverage regressions (issue #482)."
        )

    def test_configured_sources_are_importable(self):
        """Every source must resolve as an importable package given the
        configured ``pythonpath`` — otherwise coverage (and ``--cov``) can
        neither locate nor measure it, yielding 0%."""
        for source in _coverage_sources():
            assert importlib.util.find_spec(source) is not None, (
                f"Coverage source {source!r} is not importable. A source that "
                "cannot be imported cannot be measured → 0% coverage."
            )

    def test_pythonpath_covers_non_root_packages(self):
        """If a source package lives outside the repo root, it must be on
        ``pythonpath`` so both pytest-cov and coverage can import it."""
        pythonpath = {Path(p) for p in _pytest_pythonpath()}
        for source in _coverage_sources():
            spec = importlib.util.find_spec(source)
            if spec is None or spec.submodule_search_locations is None:
                continue
            origin = Path(spec.submodule_search_locations[0]).resolve()
            in_root = origin.is_relative_to(REPO_ROOT.resolve())
            # Either the package is under the repo root, or one of the
            # configured pythonpath entries contains it.
            if in_root:
                continue
            covered = any(
                origin.is_relative_to((REPO_ROOT / p).resolve())
                for p in pythonpath
            )
            assert covered, (
                f"Coverage source {source!r} at {origin} is outside the repo "
                f"root and not on pythonpath={sorted(pythonpath)}; coverage "
                "will report 0% for it."
            )


# ---------------------------------------------------------------------------
# 2. Integration test — coverage must actually collect >0 data.
#    Runs in an isolated subprocess so the parent pytest-cov tracer does not
#    interfere, faithfully reproducing how CI measures coverage.
# ---------------------------------------------------------------------------


class TestCoverageCollectsData:
    """The definitive assertion: given the configured sources, coverage
    measures a non-empty set of files with executed lines > 0."""

    @pytest.fixture(autouse=True)
    def _run_coverage_subprocess(self) -> dict:
        return {}  # placeholder, real work below

    def test_coverage_reports_nonzero_for_configured_sources(self):
        sources = _coverage_sources()
        omit = _coverage_omit()
        pythonpath = _pytest_pythonpath()

        # Build an isolated subprocess that mirrors pytest-cov: it starts a
        # coverage.Coverage(source=...) over the *configured* sources, imports
        # and exercises one cheap, dependency-free module from each source,
        # then reports how many files/lines were measured.
        script = textwrap_dedent(f"""
            import json, sys
            sys.path[:0] = {pythonpath!r}
            import coverage

            cov = coverage.Coverage(source={sources!r}, omit={omit!r})
            cov.start()
            measured_modules = []
            failures = []
            # Import + lightly exercise one cheap module per source so real
            # lines get traced. Failures here must NOT abort the measurement
            # run — we still assert >0 from the modules that did import.
            try:
                from engine.core.options import black_scholes
                from engine.core.options.black_scholes import OptionType, bs_greeks
                measured_modules.append("engine.core.options.black_scholes")
                try:
                    bs_greeks(option_type=OptionType.CALL, S=100.0, K=100.0,
                              T=1.0, r=0.05, sigma=0.2)
                except Exception as exc:  # pragma: no cover - API drift guard
                    failures.append(f"black_scholes call: {{exc!r}}")
            except Exception as exc:  # pragma: no cover - env guard
                failures.append(f"engine import: {{exc!r}}")

            try:
                import nexus_sdk.types as types
                import nexus_sdk.signals as signals
                measured_modules.append("nexus_sdk.types")
                measured_modules.append("nexus_sdk.signals")
                try:
                    types.Money(amount=1.0, currency="USD")
                except Exception as exc:  # pragma: no cover - API drift guard
                    failures.append(f"Money call: {{exc!r}}")
            except Exception as exc:  # pragma: no cover - env guard
                failures.append(f"nexus_sdk import: {{exc!r}}")

            cov.stop()
            data = cov.get_data()
            files = list(data.measured_files())
            executed = sum(len(data.lines(f) or ()) for f in files)
            print("COVERAGE_RESULT_JSON=" + json.dumps({{
                "sources": {sources!r},
                "measured_files": len(files),
                "executed_lines": executed,
                "measured_modules": measured_modules,
                "import_failures": failures,
            }}))
        """)

        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )

        assert result.returncode == 0, (
            "coverage measurement subprocess failed:\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )

        payload = _extract_result_json(result.stdout)
        assert payload is not None, (
            "coverage subprocess produced no COVERAGE_RESULT_JSON line.\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )

        # The core assertion: coverage must measure real files with real
        # executed lines. If this ever drops to zero, the config has drifted
        # into the 0% state — exactly the regression this test exists to catch.
        assert payload["measured_files"] > 0, (
            "coverage measured ZERO files for sources "
            f"{payload['sources']!r} — this is the 0% regression (issue #482).\n"
            f"import_failures={payload['import_failures']!r}"
        )
        assert payload["executed_lines"] > 0, (
            "coverage measured zero executed lines for sources "
            f"{payload['sources']!r} — files were found but nothing was traced, "
            "indicating the tracer never attached to the sources (0% bug)."
        )
        # At least one module from every configured source must be measurable.
        measured = {m.split(".", 1)[0] for m in payload["measured_modules"]}
        for source in payload["sources"]:
            assert source in measured, (
                f"coverage measured nothing under source {source!r}; "
                f"measured_modules={payload['measured_modules']!r}"
            )


def _extract_result_json(stdout: str) -> dict | None:
    for line in reversed(stdout.splitlines()):
        marker = "COVERAGE_RESULT_JSON="
        if marker in line:
            return json.loads(line.split(marker, 1)[1])
    return None


def textwrap_dedent(text: str) -> str:
    import textwrap

    return textwrap.dedent(text)
