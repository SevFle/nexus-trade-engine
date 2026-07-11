"""TOCTOU (time-of-check/time-of-use) integration tests for plugin loading.

These tests pin the security property that the **exact bytes which pass static
validation are the bytes that get executed** — never a later re-read of the
file from disk.  Without this guarantee an attacker who can swap a plugin file
on disk *between* validation and execution could smuggle un-validated code
(forbidden ``import`` statements, ``exec``/``eval`` calls) into the module
namespace.

The fix lives in :func:`engine.plugins.restricted_importer.validate_file`,
which reads the file once and returns the validated source, and in
:func:`engine.plugins.registry.load_strategy_class`, which compiles+execs the
returned bytes directly instead of re-reading via ``spec.loader.exec_module``.
"""

from __future__ import annotations

import builtins
from pathlib import Path

from engine.plugins.registry import load_strategy_class
from engine.plugins.restricted_importer import validate_file

ORIGINAL_SOURCE = (
    'class Strategy:\n'
    '    name = "original"\n'
    '    version = "1"\n'
)
TAMPERED_SOURCE = (
    'class Strategy:\n'
    '    name = "tampered"\n'
    '    version = "2"\n'
)


def _write_plugin(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


class TestValidateFileReturnsFrozenSource:
    """``validate_file`` returns the validated bytes; tampering the disk file
    afterwards must not change what a caller executes from those bytes."""

    def test_returned_source_reflects_original_not_tampered_disk(self, tmp_path: Path) -> None:
        plugin = _write_plugin(tmp_path / "strategy.py", ORIGINAL_SOURCE)

        # Read + validate: returns the exact bytes that passed validation.
        source = validate_file(str(plugin))
        assert isinstance(source, bytes)
        assert source == ORIGINAL_SOURCE.encode()

        # ── attacker swaps the file on disk AFTER validation ──────────
        plugin.write_text(TAMPERED_SOURCE)
        # Sanity check: the disk file really did change.
        assert plugin.read_text() == TAMPERED_SOURCE

        # The caller compiles+execs the VALIDATED bytes — never a re-read.
        namespace: dict[str, object] = {}
        exec(compile(source, str(plugin), "exec"), namespace)  # noqa: S102

        strategy_cls = namespace["Strategy"]
        # The executed code is the *original validated version*, proving the
        # post-validation disk tamper had no effect on what ran.
        assert strategy_cls.name == "original"
        assert strategy_cls.version == "1"

    def test_validate_file_reads_each_call_freshly(self, tmp_path: Path) -> None:
        """A second ``validate_file`` call re-reads the current disk content.

        This guards the opposite extreme: the helper is not *caching* stale
        bytes — each invocation reflects the live file. Combined with the
        first test this shows the freeze happens at *return* time (the caller
        holds a stable bytes object), not globally.
        """
        plugin = _write_plugin(tmp_path / "strategy.py", ORIGINAL_SOURCE)
        first = validate_file(str(plugin))
        assert first == ORIGINAL_SOURCE.encode()

        plugin.write_text(TAMPERED_SOURCE)
        second = validate_file(str(plugin))
        assert second == TAMPERED_SOURCE.encode()
        # The earlier return value is unaffected by the re-read.
        assert first == ORIGINAL_SOURCE.encode()

    def test_validate_file_rejects_blocked_source(self, tmp_path: Path) -> None:
        """Forbidden imports must raise before any bytes are returned."""
        plugin = _write_plugin(
            tmp_path / "evil.py",
            "import os\n\nclass Strategy:\n    name = 'x'\n",
        )
        import pytest

        with pytest.raises(ImportError, match="import validator"):
            validate_file(str(plugin))


class TestLoadStrategyClassExecutesValidatedBytes:
    """End-to-end: ``load_strategy_class`` must execute the validated bytes,
    not a re-read of the (possibly tampered) file on disk."""

    def test_disk_swap_between_validate_and_exec_is_ineffective(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        plugin = _write_plugin(tmp_path / "strategy.py", ORIGINAL_SOURCE)

        # Wrap ``validate_file`` as seen by the registry so that *after* it
        # returns the validated bytes — but before ``load_strategy_class``
        # compiles+execs them — we overwrite the file on disk with tampered
        # content.  This simulates an attacker who wins the race window
        # between validation and execution.
        import engine.plugins.registry as registry_mod

        real_validate_file = registry_mod.validate_file
        tampered: dict[str, bool] = {"done": False}

        def validate_then_swap(path, *args, **kwargs):
            source = real_validate_file(path, *args, **kwargs)
            # Swap the on-disk file now — the validated bytes are already
            # captured in ``source`` and must be what gets executed.
            if not tampered["done"]:
                tampered["done"] = True
                plugin.write_text(TAMPERED_SOURCE)
            return source

        monkeypatch.setattr(registry_mod, "validate_file", validate_then_swap)

        cls = load_strategy_class(str(plugin))
        instance = cls()

        # The executed code is the ORIGINAL validated version, even though the
        # file on disk was swapped to the tampered content before exec ran.
        assert instance.name == "original"
        assert instance.version == "1"
        # And the disk file really was tampered (the swap happened).
        assert plugin.read_text() == TAMPERED_SOURCE

    def test_no_second_disk_read_of_strategy_file(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        """``load_strategy_class`` reads ``strategy.py`` exactly once."""
        plugin = _write_plugin(tmp_path / "strategy.py", ORIGINAL_SOURCE)

        reads: list[str] = []
        real_open = builtins.open

        def open_spy(file, mode="r", *args, **kwargs):
            try:
                import os as _os

                reads.append(_os.fspath(file))
            except TypeError:
                pass
            return real_open(file, mode, *args, **kwargs)

        # The single read happens inside ``validate_file`` in the
        # restricted_importer module, so install the spy there.
        monkeypatch.setattr(
            "engine.plugins.restricted_importer.open", open_spy, raising=False
        )

        load_strategy_class(str(plugin))

        strategy_reads = [p for p in reads if p.endswith("strategy.py")]
        assert len(strategy_reads) == 1, (
            f"strategy.py must be read exactly once (validate + exec share the "
            f"same bytes), got {len(strategy_reads)} reads: {strategy_reads}"
        )
