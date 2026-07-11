"""Unit tests for the plugin source size guard.

Covers the defence-in-depth size bounding introduced in
:mod:`engine.plugins.restricted_importer`:

* the exported :data:`MAX_PLUGIN_SIZE` constant (1 MiB),
* :func:`read_plugin_source` — the single, size-bounded read that feeds both
  static validation and ``compile``/``exec`` in
  :func:`engine.plugins.registry.load_strategy_class`.

A hostile or runaway ``strategy.py`` must be rejected *before* any
``ast.parse`` / ``compile`` work happens.  Two independent guards are
exercised:

  1. ``os.fstat`` on the open file descriptor — fails fast for obviously
     oversized files without materialising their contents.
  2. A *capped* read of ``max_size + 1`` bytes — catches a file that grew
     between the ``fstat`` and the read (TOCTOU) or whose backing store
     underreports its size (pipes / procfs).
"""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import engine.plugins.restricted_importer as ri
from engine.plugins.restricted_importer import (
    MAX_PLUGIN_SIZE,
    read_plugin_source,
)


class TestMaxPluginSizeConstant:
    """The size cap is a named, exported, well-known constant."""

    def test_max_plugin_size_is_one_mebibyte(self) -> None:
        # 1 MiB — generous for source code, well clear of anything reasonable,
        # while still bounding a hostile multi-gigabyte file.
        assert MAX_PLUGIN_SIZE == 1 << 20
        assert MAX_PLUGIN_SIZE == 1024 * 1024

    def test_max_plugin_size_exported_in_dunder_all(self) -> None:
        assert "MAX_PLUGIN_SIZE" in ri.__all__

    def test_read_plugin_source_default_uses_max_plugin_size(self) -> None:
        # The default ``max_size`` argument is bound to MAX_PLUGIN_SIZE at
        # definition time, so callers that omit it get the documented cap.
        import inspect

        sig = inspect.signature(read_plugin_source)
        assert sig.parameters["max_size"].default is MAX_PLUGIN_SIZE


class TestReadPluginSourceHappyPath:
    def test_reads_small_file_bytes(self, tmp_path: Path) -> None:
        payload = b"class Strategy:\n    pass\n"
        path = tmp_path / "small.py"
        path.write_bytes(payload)

        assert read_plugin_source(str(path)) == payload

    def test_file_at_exact_limit_is_accepted(self, tmp_path: Path) -> None:
        # Exactly at the limit must be accepted — the guard is strict ">".
        limit = 16
        path = tmp_path / "exact.py"
        path.write_bytes(b"a" * limit)

        assert read_plugin_source(str(path), max_size=limit) == b"a" * limit

    def test_returns_bytes_not_str(self, tmp_path: Path) -> None:
        path = tmp_path / "s.py"
        path.write_text("x = 1\n")
        data = read_plugin_source(str(path))
        assert isinstance(data, bytes)


class TestReadPluginSourceFstatGuard:
    """Guard #1: ``os.fstat`` rejects obviously-oversized files fast."""

    def test_oversized_file_rejected_by_fstat(self, tmp_path: Path) -> None:
        limit = 8
        path = tmp_path / "huge.py"
        path.write_bytes(b"x" * (limit + 100))

        with pytest.raises(ValueError, match="exceeds"):
            read_plugin_source(str(path), max_size=limit)

    def test_fstat_rejection_message_includes_observed_size_and_limit(
        self, tmp_path: Path
    ) -> None:
        limit = 8
        size = 200
        path = tmp_path / "huge.py"
        path.write_bytes(b"x" * size)

        with pytest.raises(ValueError) as exc:
            read_plugin_source(str(path), max_size=limit)

        msg = str(exc.value)
        assert str(size) in msg  # the observed fstat size
        assert str(limit) in msg  # the configured limit
        assert "MAX_PLUGIN_SIZE" in msg

    def test_fstat_guard_runs_before_parsing(self, tmp_path: Path) -> None:
        # The size guard must reject *before* any ast.parse/compile work —
        # so the rejection happens even for content that is not valid Python.
        limit = 4
        path = tmp_path / "junk.py"
        path.write_bytes(b"this is not valid python !!! " * 10)

        with pytest.raises(ValueError, match="exceeds"):
            read_plugin_source(str(path), max_size=limit)


class TestReadPluginSourceCappedReadGuard:
    """Guard #2: a capped read catches oversize even when fstat underreports.

    Simulates a backing store (pipe / procfs / concurrent writer) whose
    ``fstat`` size is smaller than the real byte count, so guard #1 passes
    but the capped ``read(max_size + 1)`` still detects the oversize.
    """

    @staticmethod
    def _fake_file(data: bytes):
        class _FakeFile:
            def __init__(self) -> None:
                self._buf = io.BytesIO(data)

            def fileno(self) -> int:
                # Any int; os.fstat is monkeypatched in the test.
                return -1

            def read(self, n: int = -1) -> bytes:
                return self._buf.read(n)

            def __enter__(self) -> _FakeFile:
                return self

            def __exit__(self, *a: object) -> bool:
                return False

        return _FakeFile()

    @staticmethod
    def _fake_opener(data: bytes):
        fake = TestReadPluginSourceCappedReadGuard._fake_file(data)

        def opener(_path: str, _mode: str = "rb"):
            return fake

        return opener

    def test_capped_read_rejects_when_fstat_underreports(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        limit = 4
        payload = b"x" * (limit + 10)  # real bytes exceed the limit

        # fstat lies: reports the file as empty so guard #1 passes.
        monkeypatch.setattr(ri.os, "fstat", lambda fd: MagicMock(st_size=0))

        with pytest.raises(ValueError, match="exceeds"):
            read_plugin_source(
                "phantom.py",
                max_size=limit,
                opener=self._fake_opener(payload),
            )

    def test_capped_read_message_includes_bytes_read(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        limit = 4
        payload = b"y" * (limit + 5)

        monkeypatch.setattr(ri.os, "fstat", lambda fd: MagicMock(st_size=0))

        with pytest.raises(ValueError) as exc:
            read_plugin_source(
                "phantom.py",
                max_size=limit,
                opener=self._fake_opener(payload),
            )

        msg = str(exc.value)
        # The capped read reads exactly max_size + 1 bytes before bailing.
        assert str(limit + 1) in msg
        assert "before bailing out" in msg

    def test_capped_read_accepts_exact_limit_when_fstat_underreports(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # When the real bytes are exactly at the limit (even though fstat
        # underreports), the file is accepted — the trailing byte is absent.
        limit = 4
        payload = b"z" * limit

        monkeypatch.setattr(ri.os, "fstat", lambda fd: MagicMock(st_size=0))

        data = read_plugin_source(
            "phantom.py",
            max_size=limit,
            opener=self._fake_opener(payload),
        )
        assert data == payload


class TestReadPluginSourceUsesProvidedOpener:
    """The single physical read is attributable to the caller's opener.

    This is the hook that lets ``registry.load_strategy_class`` route the
    read through its own module-global ``open`` (so test harnesses can patch
    ``engine.plugins.registry.open`` to assert the file is read exactly once
    — the TOCTOU guarantee).
    """

    def test_opener_receives_path_and_binary_mode(self, tmp_path: Path) -> None:
        path = tmp_path / "s.py"
        path.write_bytes(b"x = 1\n")

        captured: dict[str, object] = {}
        real_open = open

        def opener(p, mode="rb"):
            captured["path"] = p
            captured["mode"] = mode
            return real_open(p, mode)

        read_plugin_source(str(path), opener=opener)

        assert captured["path"] == str(path)
        assert captured["mode"] == "rb"
