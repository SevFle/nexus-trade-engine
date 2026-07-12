"""Tests for PluginRegistry — discover, load, list strategies.

Also covers the in-memory :class:`engine.plugins.plugin_registry.PluginRegistry`
public API (register / unregister / get / list_all / clear / __contains__ /
__len__) and its validation errors.
"""

from __future__ import annotations

import builtins
import re
import textwrap
from pathlib import Path

import pytest
import yaml

from engine.plugins.plugin_registry import (
    DuplicatePluginError,
    PluginError,
    PluginNotFoundError,
)
from engine.plugins.plugin_registry import (
    PluginRegistry as InMemoryPluginRegistry,
)
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


class TestRegistrySyntaxErrorAndOversizedLogging:
    """Item 3: the SyntaxError logging path and oversized-file rejection.

    ``PluginRegistry.load_strategy`` must swallow ``SyntaxError`` (and the
    ``ValueError`` raised by the size guard) and surface them via the warning
    log *with the captured exception message*, returning ``None`` rather than
    propagating — so one broken/oversized strategy never takes down the whole
    registry load.
    """

    def test_syntax_error_in_strategy_returns_none_and_logs(
        self, strategies_dir, monkeypatch
    ):
        from unittest.mock import MagicMock

        import engine.plugins.registry as reg

        path = strategies_dir / "bad_syntax"
        # Malformed Python → ast.parse (inside ImportValidator.validate) and
        # compile both raise SyntaxError.
        _write_strategy(
            path,
            {"name": "bad_syntax", "version": "1.0.0"},
            "def foo(:\n    pass\n",
        )

        mock_logger = MagicMock()
        monkeypatch.setattr(reg, "logger", mock_logger)

        registry = PluginRegistry(strategies_dir)
        # SyntaxError is caught, not propagated.
        assert registry.load_strategy("bad_syntax") is None

        # The exception message is captured in the warning/error log.
        mock_logger.exception.assert_called_once()
        call = mock_logger.exception.call_args
        assert call.args[0] == "strategy_load_failed"
        assert call.kwargs["strategy"] == "bad_syntax"
        error = call.kwargs["error"]
        assert isinstance(error, str)
        assert error  # non-empty: the exception message was captured

    def test_syntax_error_propagates_from_load_strategy_class(self, strategies_dir):
        """At the class level (no swallowing) SyntaxError surfaces directly."""
        path = strategies_dir / "bad_syntax"
        _write_strategy(
            path,
            {"name": "bad_syntax", "version": "1.0.0"},
            "def foo(:\n    pass\n",
        )
        with pytest.raises(SyntaxError):
            load_strategy_class(str(path / "strategy.py"))

    def test_oversized_strategy_file_returns_none_and_logs(
        self, strategies_dir, monkeypatch
    ):
        from unittest.mock import MagicMock

        import engine.plugins.registry as reg
        from engine.plugins.restricted_importer import MAX_PLUGIN_SIZE

        path = strategies_dir / "too_big"
        # One byte over the hard cap → read_plugin_source raises ValueError
        # *before* any ast.parse/compile work happens.
        _write_strategy(
            path,
            {"name": "too_big", "version": "1.0.0"},
            "# " + "x" * MAX_PLUGIN_SIZE,
        )
        assert (path / "strategy.py").stat().st_size > MAX_PLUGIN_SIZE

        mock_logger = MagicMock()
        monkeypatch.setattr(reg, "logger", mock_logger)

        registry = PluginRegistry(strategies_dir)
        assert registry.load_strategy("too_big") is None

        mock_logger.exception.assert_called_once()
        call = mock_logger.exception.call_args
        assert call.args[0] == "strategy_load_failed"
        assert call.kwargs["strategy"] == "too_big"
        error = call.kwargs["error"]
        assert "exceeds" in error
        assert "MAX_PLUGIN_SIZE" in error

    def test_oversized_strategy_file_raises_value_error_from_load_strategy_class(
        self, strategies_dir
    ):
        from engine.plugins.restricted_importer import MAX_PLUGIN_SIZE

        path = strategies_dir / "too_big"
        _write_strategy(
            path,
            {"name": "too_big", "version": "1.0.0"},
            "# " + "x" * MAX_PLUGIN_SIZE,
        )
        with pytest.raises(ValueError, match="exceeds"):
            load_strategy_class(str(path / "strategy.py"))


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


# ---------------------------------------------------------------------- #
# In-memory PluginRegistry (register / unregister / get / list_all /     #
# clear / __contains__ / __len__)                                        #
# ---------------------------------------------------------------------- #
from nexus_sdk.strategy import IStrategy  # noqa: E402


class _ValidStrategy(IStrategy):
    """A fully-implemented IStrategy used as the happy-path fixture."""

    @property
    def id(self) -> str:
        return "valid"

    @property
    def name(self) -> str:
        return "Valid Strategy"

    @property
    def version(self) -> str:
        return "1.0.0"

    async def initialize(self, config) -> None:
        return None

    async def dispose(self) -> None:
        return None

    async def evaluate(self, portfolio, market, costs):
        return []

    def get_config_schema(self) -> dict:
        return {"type": "object"}


class _AlternativeValidStrategy(IStrategy):
    """Second valid strategy so multi-plugin tests have distinct classes."""

    @property
    def id(self) -> str:
        return "alt"

    @property
    def name(self) -> str:
        return "Alt"

    @property
    def version(self) -> str:
        return "2.0.0"

    async def initialize(self, config) -> None:
        return None

    async def dispose(self) -> None:
        return None

    async def evaluate(self, portfolio, market, costs):
        return []

    def get_config_schema(self) -> dict:
        return {}


class _IncompleteStrategy(IStrategy):
    """Subclasses IStrategy but leaves abstract methods unimplemented."""


class _BareClass:
    """An ordinary class that does not implement IStrategy at all."""


class TestPluginRegistryRegisterValidation:
    """Validation performed at registration time."""

    def test_register_non_class_raises_plugin_error(self):
        registry = InMemoryPluginRegistry()
        # Instance instead of a class.
        with pytest.raises(PluginError, match="must be a class"):
            registry.register("instance", _ValidStrategy())
        # A bare value.
        with pytest.raises(PluginError, match="must be a class"):
            registry.register("number", 42)
        # A function (callable but not a class).
        def some_function() -> None:
            return None

        with pytest.raises(PluginError, match="must be a class"):
            registry.register("func", some_function)

    def test_register_empty_name_raises_plugin_error(self):
        registry = InMemoryPluginRegistry()
        with pytest.raises(PluginError, match="non-empty str"):
            registry.register("", _ValidStrategy)

    def test_register_non_string_name_raises_plugin_error(self):
        registry = InMemoryPluginRegistry()
        with pytest.raises(PluginError, match="non-empty str"):
            registry.register(123, _ValidStrategy)  # type: ignore[arg-type]

    def test_register_bare_class_missing_istrategy_raises_plugin_error(self):
        """A class that is not an IStrategy subclass is rejected."""
        registry = InMemoryPluginRegistry()
        with pytest.raises(PluginError, match=re.escape("subclass of nexus_sdk.strategy.IStrategy")):
            registry.register("bare", _BareClass)
        # Registry stays empty after a failed registration.
        assert len(registry) == 0

    def test_register_abstract_subclass_raises_plugin_error(self):
        """An IStrategy subclass with unimplemented abstract methods is rejected."""
        registry = InMemoryPluginRegistry()
        with pytest.raises(PluginError, match="unimplemented abstract methods"):
            registry.register("incomplete", _IncompleteStrategy)
        # And the partially-valid class never pollutes the registry.
        assert "incomplete" not in registry

    def test_duplicate_register_raises_duplicate_plugin_error(self):
        registry = InMemoryPluginRegistry()
        registry.register("dupe", _ValidStrategy)
        with pytest.raises(DuplicatePluginError, match="already registered"):
            registry.register("dupe", _AlternativeValidStrategy)
        # Original registration is preserved.
        assert registry.get("dupe") is _ValidStrategy

    def test_duplicate_register_is_plugin_error_subclass(self):
        """DuplicatePluginError must be catchable as the base PluginError."""
        registry = InMemoryPluginRegistry()
        registry.register("dupe", _ValidStrategy)
        with pytest.raises(PluginError):
            registry.register("dupe", _ValidStrategy)


class TestPluginRegistryUnregister:
    def test_unregister_unknown_raises_plugin_not_found_error(self):
        registry = InMemoryPluginRegistry()
        with pytest.raises(PluginNotFoundError, match="not registered"):
            registry.unregister("ghost")

    def test_unregister_unknown_is_plugin_error_subclass(self):
        registry = InMemoryPluginRegistry()
        with pytest.raises(PluginError):
            registry.unregister("ghost")

    def test_unregister_known_removes_it(self):
        registry = InMemoryPluginRegistry()
        registry.register("s", _ValidStrategy)
        registry.unregister("s")
        assert registry.get("s") is None
        assert "s" not in registry
        assert len(registry) == 0

    def test_unregister_then_re_register_works(self):
        registry = InMemoryPluginRegistry()
        registry.register("s", _ValidStrategy)
        registry.unregister("s")
        # Re-using the name after removal must not trip DuplicatePluginError.
        registry.register("s", _AlternativeValidStrategy)
        assert registry.get("s") is _AlternativeValidStrategy


class TestPluginRegistryLifecycle:
    """register -> get -> unregister -> get full lifecycle."""

    def test_register_get_unregister_get_lifecycle(self):
        registry = InMemoryPluginRegistry()

        # Initially absent.
        assert registry.get("life") is None
        assert "life" not in registry
        assert len(registry) == 0

        # Register -> present.
        registry.register("life", _ValidStrategy)
        assert registry.get("life") is _ValidStrategy
        assert "life" in registry
        assert len(registry) == 1

        # Unregister -> absent again.
        registry.unregister("life")
        assert registry.get("life") is None
        assert "life" not in registry
        assert len(registry) == 0

    def test_get_returns_none_for_unknown(self):
        registry = InMemoryPluginRegistry()
        assert registry.get("nope") is None

    def test_get_returns_the_exact_class_registered(self):
        registry = InMemoryPluginRegistry()
        registry.register("exact", _ValidStrategy)
        assert registry.get("exact") is _ValidStrategy

    def test_multiple_registrations_coexist(self):
        registry = InMemoryPluginRegistry()
        registry.register("a", _ValidStrategy)
        registry.register("b", _AlternativeValidStrategy)
        assert len(registry) == 2
        assert registry.get("a") is _ValidStrategy
        assert registry.get("b") is _AlternativeValidStrategy


class TestPluginRegistryListAllCopySemantics:
    def test_list_all_returns_copy_not_internal_state(self):
        registry = InMemoryPluginRegistry()
        registry.register("a", _ValidStrategy)
        snapshot = registry.list_all()
        # Mutating the returned list must not affect the registry.
        snapshot.append("a")
        snapshot.clear()
        assert registry.list_all() == ["a"]

    def test_list_all_returns_new_object_each_call(self):
        registry = InMemoryPluginRegistry()
        registry.register("a", _ValidStrategy)
        first = registry.list_all()
        second = registry.list_all()
        assert first == second == ["a"]
        assert first is not second

    def test_list_all_is_empty_for_fresh_registry(self):
        assert InMemoryPluginRegistry().list_all() == []

    def test_list_all_reflects_current_keys(self):
        registry = InMemoryPluginRegistry()
        registry.register("a", _ValidStrategy)
        registry.register("b", _AlternativeValidStrategy)
        assert set(registry.list_all()) == {"a", "b"}
        registry.unregister("a")
        assert registry.list_all() == ["b"]


class TestPluginRegistryClear:
    def test_clear_empties_registry(self):
        registry = InMemoryPluginRegistry()
        registry.register("a", _ValidStrategy)
        registry.register("b", _AlternativeValidStrategy)
        assert len(registry) == 2
        registry.clear()
        assert len(registry) == 0
        assert registry.list_all() == []
        assert registry.get("a") is None

    def test_clear_on_empty_registry_is_noop(self):
        registry = InMemoryPluginRegistry()
        registry.clear()  # must not raise
        assert len(registry) == 0

    def test_clear_allows_reuse_of_names(self):
        registry = InMemoryPluginRegistry()
        registry.register("a", _ValidStrategy)
        registry.clear()
        # Name is free again after clear.
        registry.register("a", _AlternativeValidStrategy)
        assert registry.get("a") is _AlternativeValidStrategy


class TestPluginRegistryContainsAndLen:
    def test_contains_true_for_registered(self):
        registry = InMemoryPluginRegistry()
        registry.register("s", _ValidStrategy)
        assert "s" in registry

    def test_contains_false_for_unknown(self):
        registry = InMemoryPluginRegistry()
        assert "ghost" not in registry

    def test_contains_false_after_unregister(self):
        registry = InMemoryPluginRegistry()
        registry.register("s", _ValidStrategy)
        assert "s" in registry
        registry.unregister("s")
        assert "s" not in registry

    def test_contains_unhashable_name_returns_false_not_typeerror(self):
        """Unhashable keys must degrade to False instead of raising."""
        registry = InMemoryPluginRegistry()
        registry.register("s", _ValidStrategy)
        assert ["s"] not in registry  # list is unhashable
        assert {"s": 1} not in registry  # dict is unhashable

    def test_len_tracks_registrations(self):
        registry = InMemoryPluginRegistry()
        assert len(registry) == 0
        registry.register("a", _ValidStrategy)
        assert len(registry) == 1
        registry.register("b", _AlternativeValidStrategy)
        assert len(registry) == 2
        registry.unregister("a")
        assert len(registry) == 1
        registry.clear()
        assert len(registry) == 0


class TestPluginRegistryExceptionHierarchy:
    def test_duplicate_plugin_error_is_plugin_error(self):
        assert issubclass(DuplicatePluginError, PluginError)

    def test_plugin_not_found_error_is_plugin_error(self):
        assert issubclass(PluginNotFoundError, PluginError)

    def test_errors_are_distinct(self):
        assert DuplicatePluginError is not PluginNotFoundError
        assert DuplicatePluginError is not PluginError
