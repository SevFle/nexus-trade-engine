"""Verify that the coverage tool is correctly configured to measure the
``engine`` package — the actual source directory for ``nexus-trade-engine``.

This guards against the class of regression where the PyPI name
(``nexus-trade-engine``) is mistakenly used as the ``--cov`` target,
resulting in 0 % reported coverage even though tests run fine.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"


def _parse_toml_key(data: dict, *keys: str):
    out = data
    for k in keys:
        out = out[k]
    return out


class TestCoverageConfigAlignment:
    @pytest.fixture(autouse=True)
    def _load_pyproject(self):
        try:
            import tomllib
        except ModuleNotFoundError:
            import tomli as tomllib

        with PYPROJECT.open("rb") as fh:
            self._cfg = tomllib.load(fh)

    def test_engine_package_is_importable(self):
        mod = importlib.import_module("engine")
        assert mod is not None

    def test_pytest_addopts_cov_matches_source_dir(self):
        addopts: str = _parse_toml_key(
            self._cfg, "tool", "pytest", "ini_options", "addopts"
        )
        assert "--cov=engine" in addopts, (
            f"addopts must contain --cov=engine, got: {addopts}"
        )

    def test_coverage_run_source_matches_engine_dir(self):
        sources: list[str] = _parse_toml_key(
            self._cfg, "tool", "coverage", "run", "source"
        )
        assert "engine" in sources, (
            f"[tool.coverage.run].source must include 'engine', got: {sources}"
        )

    def test_no_nexus_trade_engine_in_cov_targets(self):
        addopts: str = _parse_toml_key(
            self._cfg, "tool", "pytest", "ini_options", "addopts"
        )
        assert "nexus_trade_engine" not in addopts, (
            "addopts must not reference 'nexus_trade_engine' (no such importable "
            "package exists; the source directory is 'engine')"
        )

    def test_engine_dir_exists_and_has_init(self):
        engine_dir = ROOT / "engine"
        assert engine_dir.is_dir(), "engine/ directory must exist"
        assert (engine_dir / "__init__.py").exists(), "engine/__init__.py must exist"

    def test_hatch_build_packages_includes_engine(self):
        packages: list[str] = _parse_toml_key(
            self._cfg, "tool", "hatch", "build", "targets", "wheel", "packages"
        )
        assert "engine" in packages, (
            f"[tool.hatch.build.targets.wheel].packages must include 'engine', got: {packages}"
        )

    def test_coverage_report_fail_under_is_set(self):
        fail_under: int = _parse_toml_key(
            self._cfg, "tool", "coverage", "report", "fail_under"
        )
        assert fail_under > 0, "fail_under must be a positive integer"

    def test_coverage_run_omits_tests_and_migrations(self):
        omits: list[str] = _parse_toml_key(
            self._cfg, "tool", "coverage", "run", "omit"
        )
        assert any("tests" in o for o in omits), "omit must exclude tests/"
        assert any("migrations" in o for o in omits), "omit must exclude migrations/"

    def test_pythonpath_includes_root(self):
        pythonpath: list[str] = _parse_toml_key(
            self._cfg, "tool", "pytest", "ini_options", "pythonpath"
        )
        assert "." in pythonpath, (
            "pythonpath must include '.' so 'engine' is importable from the project root"
        )

    def test_engine_core_modules_importable(self):
        for mod_name in [
            "engine.core.signal",
            "engine.core.portfolio",
            "engine.core.risk_engine",
            "engine.observability.metrics",
        ]:
            mod = importlib.import_module(mod_name)
            assert mod is not None, f"{mod_name} must be importable"
