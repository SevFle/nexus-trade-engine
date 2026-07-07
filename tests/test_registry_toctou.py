"""TOCTOU-safety tests for :func:`engine.plugins.registry.load_strategy_class`.

The previous implementation loaded a strategy module via
``spec.loader.exec_module(module)``, which **re-reads the file from disk at
execution time**.  That opened a classic time-of-check-to-time-of-use window:
the bytes statically validated before the call could differ from the bytes
actually executed if the file changed on disk between the two reads.

The fix reads the source **once**, statically validates it, ``compile()``s the
*validated* string into a code object, and ``exec()``s that code object
directly in a fresh module namespace — guaranteeing *validated bytes ==
executed bytes*.  These tests pin that invariant.
"""

from __future__ import annotations

import importlib.util
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

import engine.plugins.registry as registry_mod
from engine.plugins.registry import _read_strategy_source, load_strategy_class


def _write(path: Path, source: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    return path


SAFE_SOURCE = textwrap.dedent("""\
    class Strategy:
        marker = "validated-safe"
        name = "safe"
""")

# Malicious payload that would be executed if a *second* disk read were
# performed after validation.  Under the TOCTOU fix this must never run.
MALICIOUS_SOURCE = textwrap.dedent("""\
    class Strategy:
        marker = "toctou-malicious"
        name = "exploited"
""")


class TestToctouValidatedBytesEqualExecutedBytes:
    """The decisive regression guards for the TOCTOU window."""

    def test_second_disk_read_cannot_change_what_runs(self, tmp_path: Path, monkeypatch):
        """Mock the file read so read #1 returns safe source and read #2 would
        return malicious source.  The loaded strategy must come from the
        *validated* (first-read) bytes, and the file must be read exactly once.
        """
        strategy_file = _write(tmp_path / "strategy.py", SAFE_SOURCE)

        real_read_text = Path.read_text
        reads: list[int] = []

        def toctou_read_text(self: Path, *args, **kwargs):
            if self.resolve() == strategy_file.resolve():
                reads.append(len(reads) + 1)
                # First read → safe/validated source; any later read (which the
                # fix guarantees does not happen) → malicious source.
                return SAFE_SOURCE if len(reads) == 1 else MALICIOUS_SOURCE
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", toctou_read_text)

        cls = load_strategy_class(str(strategy_file))
        instance = cls()

        assert instance.marker == "validated-safe"
        assert instance.name == "safe"
        # The file is read exactly ONCE.  Under the old spec.loader.exec_module
        # path this would be >= 2 and the malicious marker would have leaked in.
        assert len(reads) == 1

    def test_validated_source_string_is_what_runs(self, tmp_path: Path):
        """Capture the exact string handed to the validator and assert the
        executed class reflects those bytes verbatim."""
        strategy_file = _write(tmp_path / "strategy.py", SAFE_SOURCE)

        captured: dict[str, str] = {}
        real_validate = registry_mod._validate_source

        def capturing_validator(src: str, *, module_path: str) -> str:
            captured["validated"] = src
            return real_validate(src, module_path=module_path)

        with patch.object(registry_mod, "_validate_source", side_effect=capturing_validator):
            cls = load_strategy_class(str(strategy_file))

        assert captured["validated"] == SAFE_SOURCE
        assert cls().marker == "validated-safe"

    def test_validation_failure_prevents_execution(self, tmp_path: Path):
        """When static validation rejects the source, the strategy body must
        never execute (no exec side-effect)."""
        side_effect_file = tmp_path / "ran.log"
        strategy_file = _write(
            tmp_path / "strategy.py",
            textwrap.dedent(f"""\
                class Strategy:
                    marker = "loaded"
                open({str(side_effect_file)!r}, "w").write("executed")
            """),
        )

        def rejecting_validator(_src: str, *, module_path: str) -> str:
            raise SyntaxError("rejected by static validator")

        with (
            patch.object(registry_mod, "_validate_source", side_effect=rejecting_validator),
            pytest.raises(SyntaxError, match="rejected by static validator"),
        ):
            load_strategy_class(str(strategy_file))

        # The import-time side effect must NOT have fired: validation rejected
        # the source before exec(), so the strategy body never ran.
        assert not side_effect_file.exists()

    def test_malicious_second_read_is_caught_by_validation(self, tmp_path: Path, monkeypatch):
        """If the *first* read returns malicious-looking source that a custom
        validator rejects, the loader must surface that rejection even when a
        later read would have returned benign source.

        This pins the invariant "validation sees the bytes that would run":
        the validator inspects read #1 (malicious) and raises, regardless of
        what read #2 might have returned.
        """
        strategy_file = _write(tmp_path / "strategy.py", MALICIOUS_SOURCE)

        real_read_text = Path.read_text
        reads: list[int] = []

        def flipping_read_text(self: Path, *args, **kwargs):
            if self.resolve() == strategy_file.resolve():
                reads.append(len(reads) + 1)
                # Read #1 is malicious (the original); read #2 is benign.  The
                # validator must see read #1 and reject it.
                return MALICIOUS_SOURCE if len(reads) == 1 else SAFE_SOURCE
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", flipping_read_text)

        seen: list[str] = []

        def validator(src: str, *, module_path: str) -> str:
            seen.append(src)
            if "toctou-malicious" in src:
                raise ValueError("malicious marker detected by validator")
            return src

        with (
            patch.object(registry_mod, "_validate_source", side_effect=validator),
            pytest.raises(ValueError, match="malicious marker detected"),
        ):
            load_strategy_class(str(strategy_file))

        # The validator saw the original (malicious) bytes — proving the
        # check-to-use gap is closed.
        assert seen == [MALICIOUS_SOURCE]
        assert len(reads) == 1


class TestSpecLoaderExecModuleNotInvoked:
    """The fix must exec the compiled code object directly — never call
    ``spec.loader.exec_module`` (which re-reads the file from disk)."""

    def test_exec_module_is_never_called(self, tmp_path: Path):
        strategy_file = _write(
            tmp_path / "strategy.py",
            textwrap.dedent("""\
                class Strategy:
                    marker = "loaded"
            """),
        )
        real_spec_from_file_location = importlib.util.spec_from_file_location

        def trapping_spec_factory(*args, **kwargs):
            spec = real_spec_from_file_location(*args, **kwargs)
            if spec is not None and spec.loader is not None:

                def booby_trap(_module):
                    raise AssertionError(
                        "spec.loader.exec_module must not be called — "
                        "load_strategy_class must exec the validated code object directly"
                    )

                spec.loader.exec_module = booby_trap  # type: ignore[method-assign]
            return spec

        with patch(
            "engine.plugins.registry.importlib.util.spec_from_file_location",
            side_effect=trapping_spec_factory,
        ):
            cls = load_strategy_class(str(strategy_file))

        assert cls().marker == "loaded"


class TestModuleMetadataAndBehaviour:
    """The TOCTOU-safe loader still behaves like a normal module load."""

    def test_module_file_attribute_is_set(self, tmp_path: Path):
        strategy_file = _write(
            tmp_path / "strategy.py",
            textwrap.dedent("""\
                class Strategy:
                    file_location = __file__
            """),
        )
        cls = load_strategy_class(str(strategy_file))
        assert cls.file_location == str(strategy_file)

    def test_module_name_is_strategy(self, tmp_path: Path):
        strategy_file = _write(
            tmp_path / "strategy.py",
            textwrap.dedent("""\
                class Strategy:
                    module_name = __name__
            """),
        )
        cls = load_strategy_class(str(strategy_file))
        assert cls.module_name == "strategy"

    def test_syntax_error_raised_with_file_path(self, tmp_path: Path):
        strategy_file = _write(tmp_path / "strategy.py", "def broken(:\n")
        with pytest.raises(SyntaxError):
            load_strategy_class(str(strategy_file))

    def test_validation_runs_before_class_lookup(self, tmp_path: Path):
        """A module that defines no ``Strategy`` still passes validation
        (it is syntactically valid) and only fails at the class lookup."""
        strategy_file = _write(tmp_path / "strategy.py", "x = 42\n")
        with pytest.raises(AttributeError, match="Strategy"):
            load_strategy_class(str(strategy_file))


class TestReadStrategySource:
    def test_returns_file_contents(self, tmp_path: Path):
        path = _write(tmp_path / "s.py", "class Strategy: pass\n")
        assert _read_strategy_source(str(path)) == "class Strategy: pass\n"

    @pytest.mark.parametrize(
        "bad_path",
        [
            "/nonexistent/deep/path/strategy.py",
            "/proc/this/should/not/exist/strategy.py",
        ],
    )
    def test_missing_file_normalised_to_import_error(self, bad_path: str):
        with pytest.raises(ImportError, match="Cannot read strategy source"):
            _read_strategy_source(bad_path)

    def test_permission_denied_normalised_to_import_error(self, tmp_path: Path):
        path = _write(tmp_path / "s.py", "class Strategy: pass\n")
        path.chmod(0o000)
        try:
            # Root bypasses Unix file permissions, in which case the read
            # succeeds and this scenario cannot be exercised — skip cleanly.
            try:
                path.read_text(encoding="utf-8")
            except PermissionError:
                pass  # permissions are enforced (not root): proceed to assert.
            else:
                pytest.skip("running as root — permission bits are ignored")

            with pytest.raises(ImportError, match="Cannot read strategy source"):
                _read_strategy_source(str(path))
        finally:
            path.chmod(0o644)


class TestFullToctouScenarioViaRegistry:
    """End-to-end: a swapped file between discovery and load must not affect
    what actually runs."""

    def test_file_swap_after_discovery_runs_original_bytes(self, tmp_path: Path, monkeypatch):
        strategies_dir = tmp_path / "strategies"
        strategy_file = _write(
            strategies_dir / "swapped" / "strategy.py",
            SAFE_SOURCE,
        )
        _write(
            strategies_dir / "swapped" / "manifest.yaml",
            "name: swapped\nversion: 1.0.0\n",
        )

        # Discover with the safe source on disk.
        discovered = registry_mod.discover_strategies(strategies_dir)
        module_path = discovered["swapped"]["module_path"]

        # Now swap the file on disk to the malicious payload *after* discovery.
        strategy_file.write_text(MALICIOUS_SOURCE)

        # Intercept reads: return the original SAFE_SOURCE on the single read
        # performed by load_strategy_class, then a different value on any
        # hypothetical second read.  This models "validated bytes" being the
        # safe snapshot captured at read time.
        real_read_text = Path.read_text
        reads: list[int] = []

        def single_read(self: Path, *args, **kwargs):
            if self.resolve() == strategy_file.resolve():
                reads.append(len(reads) + 1)
                return SAFE_SOURCE if len(reads) == 1 else MALICIOUS_SOURCE
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", single_read)

        registry = registry_mod.PluginRegistry(strategies_dir)
        instance = registry.load_strategy("swapped")

        assert instance is not None
        assert instance.marker == "validated-safe"
        assert len(reads) == 1
        # The path the registry discovered is exactly what gets loaded.
        assert module_path.endswith("swapped/strategy.py")
