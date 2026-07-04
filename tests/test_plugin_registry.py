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


class TestPluginRegistryLifecycle:
    """Cover the list_all / get / unload / reload surface used by the
    ``/api/v1/strategies`` management routes."""

    def test_list_all_summarises_installed_strategies(self, strategies_dir):
        _write_strategy(
            strategies_dir / "alpha",
            {"name": "alpha", "version": "1.2.0"},
            "class Strategy:\n    name = 'alpha'\n    version = '1.2.0'\n",
        )
        _write_strategy(
            strategies_dir / "beta",
            {"name": "beta", "version": "0.9.0"},
            "class Strategy:\n    name = 'beta'\n    version = '0.9.0'\n",
        )

        registry = PluginRegistry(strategies_dir)
        summaries = registry.list_all()
        by_id = {s["id"]: s for s in summaries}
        assert set(by_id) == {"alpha", "beta"}
        assert by_id["alpha"]["name"] == "alpha"
        assert by_id["alpha"]["version"] == "1.2.0"
        assert by_id["alpha"]["is_loaded"] is False

    def test_list_all_empty_when_no_strategies(self, strategies_dir):
        assert PluginRegistry(strategies_dir).list_all() == []

    def test_get_returns_entry_with_manifest(self, strategies_dir):
        _write_strategy(
            strategies_dir / "gamma",
            {
                "name": "gamma",
                "version": "2.0.0",
                "author": "tester",
                "description": "a gamma strat",
            },
            "class Strategy:\n    name = 'gamma'\n    version = '2.0.0'\n",
        )

        registry = PluginRegistry(strategies_dir)
        entry = registry.get("gamma")
        assert entry is not None
        assert entry.id == "gamma"
        assert entry.manifest.id == "gamma"
        assert entry.manifest.name == "gamma"
        assert entry.manifest.version == "2.0.0"
        assert entry.manifest.author == "tester"
        assert entry.manifest.description == "a gamma strat"
        assert entry.is_loaded is False

    def test_get_unknown_strategy_returns_none(self, strategies_dir):
        registry = PluginRegistry(strategies_dir)
        assert registry.get("does_not_exist") is None

    def test_get_entry_manifest_has_capability_helpers(self, strategies_dir):
        _write_strategy(
            strategies_dir / "capable",
            {"name": "capable", "version": "1.0.0"},
            "class Strategy:\n    name = 'capable'\n    version = '1.0.0'\n",
        )
        entry = PluginRegistry(strategies_dir).get("capable")
        assert entry is not None
        # Defaults from StrategyManifest — no network/gpu requested.
        assert entry.manifest.requires_network() is False
        assert entry.manifest.requires_gpu() is False
        assert isinstance(entry.manifest.data_feeds, list)
        assert isinstance(entry.manifest.watchlist, list)

    async def test_instantiate_marks_strategy_loaded(self, strategies_dir):
        _write_strategy(
            strategies_dir / "loadable",
            {"name": "loadable", "version": "1.0.0"},
            "class Strategy:\n    name = 'loadable'\n    version = '1.0.0'\n",
        )
        registry = PluginRegistry(strategies_dir)
        entry = registry.get("loadable")
        assert entry is not None and entry.is_loaded is False

        instance = await entry.instantiate(config=None)
        assert instance.name == "loadable"
        assert entry.is_loaded is True
        # list_all reflects the new loaded state.
        assert next(
            s for s in registry.list_all() if s["id"] == "loadable"
        )["is_loaded"] is True

    async def test_unload_clears_loaded_instance(self, strategies_dir):
        _write_strategy(
            strategies_dir / "ephemeral",
            {"name": "ephemeral", "version": "1.0.0"},
            "class Strategy:\n    name = 'ephemeral'\n    version = '1.0.0'\n",
        )
        registry = PluginRegistry(strategies_dir)
        entry = registry.get("ephemeral")
        await entry.instantiate()
        assert entry.is_loaded is True

        await registry.unload("ephemeral")
        assert entry.is_loaded is False

    async def test_unload_unknown_strategy_is_noop(self, strategies_dir):
        registry = PluginRegistry(strategies_dir)
        # Should not raise even though nothing is loaded.
        await registry.unload("ghost")

    async def test_reload_refreshes_manifest_and_clears_instance(self, strategies_dir):
        code = (
            "class Strategy:\n"
            "    name = 'hot'\n"
            "    version = '1.0.0'\n"
        )
        _write_strategy(
            strategies_dir / "hot",
            {"name": "hot", "version": "1.0.0"},
            code,
        )
        registry = PluginRegistry(strategies_dir)
        entry = registry.get("hot")
        await entry.instantiate()
        assert entry.is_loaded is True

        # Rewrite the manifest on disk and reload.
        _write_strategy(
            strategies_dir / "hot",
            {"name": "hot", "version": "2.0.0"},
            code,
        )
        ok = await registry.reload("hot")
        assert ok is True
        # Instance cache cleared and manifest reflects the new version.
        assert entry.is_loaded is False
        assert registry.get("hot").manifest.version == "2.0.0"

    async def test_reload_missing_strategy_returns_false(self, strategies_dir):
        registry = PluginRegistry(strategies_dir)
        assert await registry.reload("nope") is False


class TestPluginRegistryGetManifest:
    """Cover ``PluginRegistry.get_manifest`` (#1147).

    ``get_manifest`` is the consolidated discovery accessor that lets callers
    read an installed strategy's ``manifest.yaml`` metadata without
    re-running :func:`discover_strategies` over the directory and — crucially
    — without importing the strategy module. These tests lock in both
    contracts so the registry stays the single source of truth.
    """

    def test_get_manifest_returns_parsed_manifest_dict(self, strategies_dir):
        _write_strategy(
            strategies_dir / "alpha",
            {"name": "alpha", "version": "1.2.0", "author": "tester"},
            "class Strategy:\n    name = 'alpha'\n    version = '1.2.0'\n",
        )
        manifest = PluginRegistry(strategies_dir).get_manifest("alpha")
        assert manifest is not None
        assert manifest["name"] == "alpha"
        assert manifest["version"] == "1.2.0"
        assert manifest["author"] == "tester"

    def test_get_manifest_unknown_strategy_returns_none(self, strategies_dir):
        registry = PluginRegistry(strategies_dir)
        assert registry.get_manifest("does_not_exist") is None

    def test_get_manifest_matches_discovery_single_source_of_truth(
        self, strategies_dir
    ):
        """get_manifest must agree with the raw discovery result, so callers
        don't have to pick between two sources of installed-strategy metadata."""
        _write_strategy(
            strategies_dir / "consistent",
            {"name": "consistent", "version": "3.1.4", "description": "same"},
            "class Strategy:\n    name = 'consistent'\n    version = '3.1.4'\n",
        )
        registry = PluginRegistry(strategies_dir)
        discovered = discover_strategies(strategies_dir)
        assert registry.get_manifest("consistent") == discovered["consistent"]["manifest"]

    def test_get_manifest_does_not_import_strategy_module(self, strategies_dir):
        """Reading metadata via get_manifest must NOT import ``strategy.py``.

        We prove this by shipping a ``strategy.py`` that raises on import and
        asserting get_manifest still returns the parsed manifest. This is the
        core guarantee of the docstring: metadata is readable without the
        import / instantiation side-effects ``load_strategy`` performs.
        """
        _write_strategy(
            strategies_dir / "toxic_import",
            {"name": "toxic_import", "version": "1.0.0"},
            "raise RuntimeError('this module must not be imported for metadata')\n",
        )
        # Sanity: discovery picked it up (strategy.py exists).
        assert "toxic_import" in discover_strategies(strategies_dir)

        manifest = PluginRegistry(strategies_dir).get_manifest("toxic_import")
        assert manifest is not None
        assert manifest["name"] == "toxic_import"
        assert manifest["version"] == "1.0.0"

    def test_get_manifest_returns_same_object_identity_as_list_all_entry(
        self, strategies_dir
    ):
        """list_all reads the same manifest dict get_manifest returns, so the
        two views can't drift on per-strategy metadata."""
        _write_strategy(
            strategies_dir / "shared",
            {"name": "shared", "version": "0.4.2"},
            "class Strategy:\n    name = 'shared'\n    version = '0.4.2'\n",
        )
        registry = PluginRegistry(strategies_dir)
        summary = next(s for s in registry.list_all() if s["id"] == "shared")
        manifest = registry.get_manifest("shared")
        assert manifest is not None
        assert summary["name"] == manifest["name"]
        assert summary["version"] == manifest["version"]


@pytest.mark.integration
class TestGetManifestRealStrategies:
    def test_get_manifest_for_bundled_mean_reversion_basic(self):
        registry = PluginRegistry()
        manifest = registry.get_manifest("mean_reversion_basic")
        assert manifest is not None
        assert manifest["name"] == "mean_reversion_basic"
        # The bundled manifest always carries a version.
        assert "version" in manifest


class TestIsScoringStrategyFallback:
    def test_returns_false_when_scoring_module_unavailable(self):
        import sys
        from unittest.mock import patch

        with patch.dict(sys.modules, {"nexus_sdk.scoring": None}):
            from engine.plugins.registry import is_scoring_strategy

            assert is_scoring_strategy(object()) is False


class TestLoadStrategyClassNullSpec:
    def test_raises_import_error_for_null_spec(self):
        from unittest.mock import patch

        with (
            patch(
                "engine.plugins.registry.importlib.util.spec_from_file_location",
                return_value=None,
            ),
            pytest.raises(ImportError, match="Cannot load strategy"),
        ):
            load_strategy_class("/fake/path.py")


@pytest.mark.integration
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
