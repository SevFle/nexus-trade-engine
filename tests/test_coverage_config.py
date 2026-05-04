from __future__ import annotations

import importlib
import os
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_pyproject() -> dict:
    with (ROOT / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)


def test_coverage_sources_are_importable():
    pyproject = _load_pyproject()
    source_packages = pyproject["tool"]["coverage"]["run"]["source"]
    for pkg_name in source_packages:
        mod = importlib.import_module(pkg_name)
        assert mod.__file__ is not None, f"package {pkg_name!r} has no __file__"


def test_nexus_trade_engine_is_not_a_package():
    try:
        importlib.import_module("nexus_trade_engine")
    except ModuleNotFoundError:
        pass
    else:
        msg = (
            "nexus_trade_engine is not a valid package name. "
            "The importable packages are 'engine' and 'nexus_sdk'. "
            "Use --cov=engine --cov=nexus_sdk instead of --cov=nexus_trade_engine."
        )
        raise AssertionError(msg)


def test_pytest_addopts_cov_matches_coverage_source():
    pyproject = _load_pyproject()
    addopts = pyproject["tool"]["pytest"]["ini_options"]["addopts"]
    coverage_source = pyproject["tool"]["coverage"]["run"]["source"]
    for pkg in coverage_source:
        assert f"--cov={pkg}" in addopts, (
            f"Package {pkg!r} in coverage source but missing from pytest addopts"
        )


def test_ruff_known_first_party_matches_coverage_source():
    pyproject = _load_pyproject()
    known_first_party = pyproject["tool"]["ruff"]["lint"]["isort"]["known-first-party"]
    coverage_source = pyproject["tool"]["coverage"]["run"]["source"]
    assert set(known_first_party) == set(coverage_source), (
        f"known-first-party {known_first_party} != coverage source {coverage_source}"
    )


def test_makefile_test_target_matches_fail_under():
    makefile = (ROOT / "Makefile").read_text()
    pyproject = _load_pyproject()
    fail_under = pyproject["tool"]["coverage"]["report"]["fail_under"]
    assert f"--cov-fail-under={fail_under}" not in makefile, (
        "Makefile should not override --cov-fail-under; "
        f"let pyproject.toml's fail_under={fail_under} take effect"
    )


def test_pythonpath_includes_sdk_dir():
    pyproject = _load_pyproject()
    pythonpath = pyproject["tool"]["pytest"]["ini_options"]["pythonpath"]
    assert "sdk" in pythonpath, (
        "pythonpath must include 'sdk' so that 'nexus_sdk' is importable"
    )
