"""Tests for PluginRegistry — discover, load, list strategies."""

from __future__ import annotations

import builtins
import textwrap
from pathlib import Path

import pytest
import yaml

from engine.plugins.registry import (
    PluginError,
    PluginRegistry,
    _is_strategy_class,
    discover_strategies,
    load_strategy_class,
)
from nexus_sdk.strategy import IStrategy, MarketState, StrategyConfig


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

    def test_get_manifest_returns_parsed_manifest(self, strategies_dir):
        _write_strategy(
            strategies_dir / "strat_a",
            {
                "name": "strat_a",
                "version": "2.3.1",
                "description": "A test strategy",
                "parameters": {"window": 14},
            },
            "class Strategy: pass\n",
        )

        registry = PluginRegistry(strategies_dir)
        manifest = registry.get_manifest("strat_a")
        assert manifest is not None
        assert manifest["name"] == "strat_a"
        assert manifest["version"] == "2.3.1"
        assert manifest["parameters"] == {"window": 14}

    def test_get_manifest_returns_none_for_unknown_strategy(self, strategies_dir):
        registry = PluginRegistry(strategies_dir)
        assert registry.get_manifest("does_not_exist") is None

    def test_get_module_path_returns_strategy_py_path(self, strategies_dir):
        _write_strategy(
            strategies_dir / "strat_a",
            {"name": "strat_a", "version": "1.0.0"},
            "class Strategy: pass\n",
        )

        registry = PluginRegistry(strategies_dir)
        module_path = registry.get_module_path("strat_a")
        assert module_path is not None
        assert module_path.endswith("strat_a/strategy.py")
        # Same path that discover_strategies records for the entry.
        assert module_path == discover_strategies(strategies_dir)["strat_a"]["module_path"]

    def test_get_module_path_returns_none_for_unknown_strategy(self, strategies_dir):
        registry = PluginRegistry(strategies_dir)
        assert registry.get_module_path("does_not_exist") is None


class TestLoadStrategyClassValidatedBytesAreExecuted:
    """The exact bytes that pass static validation must be the ones executed.

    Guards the time-of-check/time-of-use fix in :func:`load_strategy_class`:
    the file must be read **once**, validated, compiled and exec'd without a
    second disk read, so swapping the file between validation and execution
    cannot smuggle un-validated code into the module namespace.
    """

    def test_executed_code_object_matches_validated_bytes(self, strategies_dir, monkeypatch):
        code = textwrap.dedent(
            """\
            class Strategy:
                name = "spy"
                version = "1.0"
        """
        )
        path = strategies_dir / "spy"
        _write_strategy(path, {"name": "spy"}, code)
        module_file = path / "strategy.py"

        # Capture the source handed to the validator.
        from engine.plugins.restricted_importer import ImportValidator

        validated: dict[str, object] = {}
        real_validate = ImportValidator.validate

        def validate_spy(self, source):
            validated["source"] = source
            return real_validate(self, source)

        monkeypatch.setattr(
            "engine.plugins.restricted_importer.ImportValidator.validate",
            validate_spy,
        )

        # Capture the code object handed to exec (patching the module-global
        # ``exec`` shadows the builtin from inside registry.py).
        captured: dict[str, object] = {}
        real_exec = exec

        def exec_spy(code_obj, namespace):
            captured["code"] = code_obj
            real_exec(code_obj, namespace)

        monkeypatch.setattr("engine.plugins.registry.exec", exec_spy, raising=False)

        cls = load_strategy_class(str(module_file))
        assert cls().name == "spy"

        # The validator received bytes...
        assert "source" in validated
        # ...and those exact bytes are what got compiled into the executed
        # code object.  ``compile`` is deterministic, so equal code objects
        # imply identical source bytes.
        expected_code = compile(validated["source"], str(module_file), "exec")
        assert "code" in captured
        assert captured["code"] == expected_code

    def test_file_is_read_only_once(self, strategies_dir, monkeypatch):
        """No second disk read between validation and execution."""
        path = strategies_dir / "once"
        _write_strategy(
            path,
            {"name": "once"},
            'class Strategy:\n    name = "once"\n',
        )
        module_file = path / "strategy.py"

        read_paths: list[str] = []
        real_open = builtins.open

        def open_spy(file, mode="r", *args, **kwargs):
            try:
                import os as _os

                read_paths.append(_os.fspath(file))
            except TypeError:
                pass
            return real_open(file, mode, *args, **kwargs)

        monkeypatch.setattr("engine.plugins.registry.open", open_spy, raising=False)

        load_strategy_class(str(module_file))

        strategy_reads = [p for p in read_paths if p.endswith("strategy.py")]
        assert len(strategy_reads) == 1, (
            f"strategy.py should be read exactly once (validate+exec share "
            f"the bytes), got {len(strategy_reads)} reads"
        )


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


# ---------------------------------------------------------------------------
# Concrete / partial strategy fixtures for ``register`` / ``_is_strategy_class``
# ---------------------------------------------------------------------------


class _ConcreteStrategy(IStrategy):
    """A fully-implemented IStrategy: no abstract methods remain."""

    @property
    def id(self) -> str:
        return "concrete"

    @property
    def name(self) -> str:
        return "Concrete"

    @property
    def version(self) -> str:
        return "0.1.0"

    async def initialize(self, config: StrategyConfig) -> None:
        pass

    async def dispose(self) -> None:
        pass

    async def evaluate(self, portfolio, market: MarketState, costs) -> list:
        return []

    def get_config_schema(self) -> dict:
        return {}


class _PartialStrategy(IStrategy):
    """Implements *some* abstract methods but leaves others abstract.

    ``__abstractmethods__`` is therefore still populated, so the class is
    abstract even though it is a genuine ``IStrategy`` subclass.
    """

    @property
    def id(self) -> str:
        return "partial"

    async def initialize(self, config: StrategyConfig) -> None:
        pass


class TestIsStrategyClass:
    """Unit tests for the ``_is_strategy_class`` predicate."""

    def test_concrete_subclass_is_accepted(self):
        assert _is_strategy_class(_ConcreteStrategy) is True

    def test_abstract_base_is_rejected(self):
        # IStrategy passes issubclass but still carries abstract methods.
        assert _is_strategy_class(IStrategy) is False

    def test_partial_abstract_subclass_is_rejected(self):
        assert _is_strategy_class(_PartialStrategy) is False

    def test_non_strategy_class_is_rejected(self):
        class NotAStrategy:
            pass

        assert _is_strategy_class(NotAStrategy) is False

    def test_builtin_types_are_rejected(self):
        assert _is_strategy_class(int) is False
        assert _is_strategy_class(dict) is False

    def test_instance_is_rejected(self):
        # Must be a *class*, not an instance.
        assert _is_strategy_class(_ConcreteStrategy()) is False

    def test_arbitrary_object_is_rejected(self):
        assert _is_strategy_class("not a class") is False
        assert _is_strategy_class(42) is False
        assert _is_strategy_class(None) is False

    def test_concrete_strategy_has_no_abstractmethods(self):
        # Sanity check: a concrete subclass really does clear the set, so the
        # guard in _is_strategy_class is what distinguishes it from the base.
        assert getattr(_ConcreteStrategy, "__abstractmethods__", set()) == set()

    def test_abstract_base_carries_abstractmethods(self):
        assert bool(getattr(IStrategy, "__abstractmethods__", set())) is True


class TestRegisterStrategy:
    """Tests for ``PluginRegistry.register`` and its abstract-class guard."""

    def test_abstract_base_rejected(self, strategies_dir):
        """IStrategy itself must not be registerable — it is abstract."""
        registry = PluginRegistry(strategies_dir)
        with pytest.raises(PluginError) as excinfo:
            registry.register(IStrategy)
        # Error message should mention the offending class and the reason.
        message = str(excinfo.value)
        assert "IStrategy" in message
        assert "abstract" in message.lower()

    def test_partial_abstract_subclass_rejected(self, strategies_dir):
        registry = PluginRegistry(strategies_dir)
        with pytest.raises(PluginError) as excinfo:
            registry.register(_PartialStrategy)
        # Should name the leftover abstract methods so the author can fix them.
        message = str(excinfo.value)
        assert "_PartialStrategy" in message
        assert "abstract" in message.lower()

    def test_non_strategy_class_rejected(self, strategies_dir):
        registry = PluginRegistry(strategies_dir)

        class NotAStrategy:
            pass

        with pytest.raises(PluginError):
            registry.register(NotAStrategy)

    def test_instance_rejected(self, strategies_dir):
        registry = PluginRegistry(strategies_dir)
        with pytest.raises(PluginError):
            registry.register(_ConcreteStrategy())

    def test_builtin_rejected(self, strategies_dir):
        registry = PluginRegistry(strategies_dir)
        with pytest.raises(PluginError):
            registry.register(int)

    def test_concrete_strategy_registered_returns_name(self, strategies_dir):
        registry = PluginRegistry(strategies_dir)
        name = registry.register(_ConcreteStrategy)
        assert name == "_ConcreteStrategy"

    def test_register_with_custom_name(self, strategies_dir):
        registry = PluginRegistry(strategies_dir)
        name = registry.register(_ConcreteStrategy, name="custom-strat")
        assert name == "custom-strat"
        assert "custom-strat" in registry.list_strategies()

    def test_registered_strategy_appears_in_list(self, strategies_dir):
        registry = PluginRegistry(strategies_dir)
        assert "_ConcreteStrategy" not in registry.list_strategies()
        registry.register(_ConcreteStrategy)
        assert "_ConcreteStrategy" in registry.list_strategies()

    def test_registered_strategy_is_loadable(self, strategies_dir):
        registry = PluginRegistry(strategies_dir)
        registry.register(_ConcreteStrategy)
        instance = registry.load_strategy("_ConcreteStrategy")
        assert isinstance(instance, _ConcreteStrategy)
        assert instance.id == "concrete"

    def test_registered_strategy_overrides_disk_entry(self, strategies_dir):
        """An in-memory registration shadows a same-named on-disk strategy."""
        _write_strategy(
            strategies_dir / "_ConcreteStrategy",
            {"name": "_ConcreteStrategy", "version": "9.9.9"},
            "class Strategy: pass\n",
        )
        registry = PluginRegistry(strategies_dir)
        registry.register(_ConcreteStrategy)
        instance = registry.load_strategy("_ConcreteStrategy")
        # The in-memory concrete class wins over the trivial on-disk class.
        assert isinstance(instance, _ConcreteStrategy)

    def test_load_unknown_registered_strategy_returns_none(self, strategies_dir):
        registry = PluginRegistry(strategies_dir)
        registry.register(_ConcreteStrategy)
        assert registry.load_strategy("not-registered") is None

    def test_plugin_error_is_not_subclass_of_other_registry_errors(self):
        # PluginError must be catchable independently of ImportError/
        # AttributeError that disk loading raises.
        assert not issubclass(PluginError, ImportError)
        assert not issubclass(PluginError, AttributeError)
