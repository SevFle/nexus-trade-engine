"""Tests for PluginRegistry — discover, load, list strategies."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from engine.plugins.registry import PluginRegistry, discover_strategies, load_strategy_class


@pytest.fixture
def strategies_dir(tmp_path: Path) -> Path:
    return tmp_path / "strategies"


def _write_strategy(directory: Path, manifest: dict, code: str | None = None) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / "manifest.yaml"
    with manifest_path.open("w") as f:
        yaml.dump(manifest, f)

    if code is not None:
        (directory / "strategy.py").write_text(code)


class TestDiscoverStrategies:
    def test_discovers_strategy_with_manifest(self, strategies_dir):
        _write_strategy(
            strategies_dir / "my_strat",
            {"name": "my_strat", "version": "1.0.0"},
            "class Strategy: pass\n",
        )

        result = discover_strategies(strategies_dir)
        assert "my_strat" in result
        assert result["my_strat"]["manifest"]["name"] == "my_strat"

    def test_skips_strategy_without_strategy_py(self, strategies_dir):
        _write_strategy(
            strategies_dir / "incomplete",
            {"name": "incomplete", "version": "1.0.0"},
        )

        result = discover_strategies(strategies_dir)
        assert "incomplete" not in result

    def test_empty_dir_returns_empty(self, strategies_dir):
        result = discover_strategies(strategies_dir)
        assert result == {}

    def test_nonexistent_dir_returns_empty(self):
        result = discover_strategies(Path("/nonexistent/path"))
        assert result == {}

    def test_discovers_multiple_strategies(self, strategies_dir):
        for name in ("strat_a", "strat_b", "strat_c"):
            _write_strategy(
                strategies_dir / name,
                {"name": name, "version": "1.0.0"},
                "class Strategy: pass\n",
            )

        result = discover_strategies(strategies_dir)
        assert len(result) == 3


class TestLoadStrategyClass:
    def test_loads_valid_strategy(self, strategies_dir):
        code = textwrap.dedent("""\
            class Strategy:
                name = "test"
                version = "1.0"
        """)
        path = strategies_dir / "valid"
        _write_strategy(path, {"name": "valid"}, code)

        cls = load_strategy_class(str(path / "strategy.py"))
        instance = cls()
        assert instance.name == "test"

    def test_raises_import_error_for_missing_file(self):
        with pytest.raises((ImportError, FileNotFoundError)):
            load_strategy_class("/nonexistent/strategy.py")

    def test_raises_attribute_error_when_no_strategy_class(self, strategies_dir):
        path = strategies_dir / "no_class"
        path.mkdir(parents=True, exist_ok=True)
        (path / "strategy.py").write_text("x = 42\n")

        with pytest.raises(AttributeError, match="Strategy"):
            load_strategy_class(str(path / "strategy.py"))


class TestPluginRegistry:
    def test_list_strategies(self, strategies_dir):
        _write_strategy(
            strategies_dir / "strat_a",
            {"name": "strat_a", "version": "1.0.0"},
            "class Strategy: pass\n",
        )
        _write_strategy(
            strategies_dir / "strat_b",
            {"name": "strat_b", "version": "1.0.0"},
            "class Strategy: pass\n",
        )

        registry = PluginRegistry(strategies_dir)
        names = registry.list_strategies()
        assert "strat_a" in names
        assert "strat_b" in names

    def test_load_strategy_returns_instance(self, strategies_dir):
        code = textwrap.dedent("""\
            class Strategy:
                name = "test_strat"
                version = "1.0.0"
        """)
        _write_strategy(
            strategies_dir / "test_strat",
            {"name": "test_strat", "version": "1.0.0"},
            code,
        )

        registry = PluginRegistry(strategies_dir)
        instance = registry.load_strategy("test_strat")
        assert instance is not None
        assert instance.name == "test_strat"

    def test_load_nonexistent_strategy_returns_none(self, strategies_dir):
        registry = PluginRegistry(strategies_dir)
        assert registry.load_strategy("nonexistent") is None

    def test_load_strategy_with_bad_code_returns_none(self, strategies_dir):
        _write_strategy(
            strategies_dir / "bad_strat",
            {"name": "bad_strat", "version": "1.0.0"},
            "class Strategy:\n    def __init__(self):\n        raise RuntimeError('cannot instantiate')\n",
        )

        registry = PluginRegistry(strategies_dir)
        assert registry.load_strategy("bad_strat") is None

    def test_load_strategy_without_strategy_class_returns_none(self, strategies_dir):
        path = strategies_dir / "no_class"
        path.mkdir(parents=True, exist_ok=True)
        (path / "manifest.yaml").write_text("name: no_class\nversion: 1.0\n")
        (path / "strategy.py").write_text("x = 42\n")

        registry = PluginRegistry(strategies_dir)
        assert registry.load_strategy("no_class") is None


class TestDiscoverRealStrategies:
    def test_discovers_mean_reversion_basic(self):
        strategies = discover_strategies()
        assert "mean_reversion_basic" in strategies

    def test_mean_reversion_basic_manifest_valid(self):
        strategies = discover_strategies()
        entry = strategies.get("mean_reversion_basic")
        assert entry is not None
        manifest = entry["manifest"]
        assert manifest["name"] == "mean_reversion_basic"
        assert "version" in manifest
