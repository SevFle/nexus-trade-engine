"""
Comprehensive tests for the ``_restricted_open`` contextvar guard.

The uncommitted fix to ``engine/plugins/sandbox.py`` adds two things:

1. A module-level ``_real_open = builtins.open`` captured at import time,
   so the original builtin is always reachable even after monkey-patching.

2. A guard at the very top of ``_restricted_open``:

       if not _in_sandbox_execution.get(False):
           return _real_open(file, mode, *args, **kwargs)

   This ensures that when the sandbox is NOT active (contextvar is
   ``False``), every ``open()`` call — even one routed through a leaked
   ``builtins.open`` monkey-patch — delegates to the real builtin with
   zero restrictions.  Only when ``_in_sandbox_execution`` is ``True``
   does the whitelist / blocklist logic execute.

Before this guard, a leaked monkey-patch (e.g. from a test that forgot to
call ``cleanup()``) caused *every* subsequent ``open()`` in the entire
process to raise ``PermissionError``, cascading into 902+ test failures
across the suite — because coverage's HTML report generator, config
loaders, and unrelated test fixtures all call ``open()``.

These tests verify the guard comprehensively so the regression cannot
silently return.
"""

from __future__ import annotations

import asyncio
import builtins
import io as _io_module
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from engine.plugins.manifest import StrategyManifest
from engine.plugins.sandbox import (
    StrategySandbox,
    _in_sandbox_execution,
    _real_open,
)

if TYPE_CHECKING:
    from engine.core.signal import Signal


# ── Helpers & fixtures ────────────────────────────────────────────────


class _PassiveStrategy:
    name = "passive"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Signal]:
        return []


class _OpenStrategy:
    """Strategy that calls ``open()`` on a given path during evaluation."""

    name = "open_strategy"
    version = "1.0.0"

    def __init__(self, path: str, mode: str = "r") -> None:
        self._path = path
        self._mode = mode

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        with open(self._path, self._mode) as f:
            f.read()
        return []


@pytest.fixture
def manifest() -> StrategyManifest:
    return StrategyManifest(
        id="test",
        name="test",
        version="1.0.0",
        resources={"max_cpu_seconds": 2},
    )


@pytest.fixture(autouse=True)
def _reset_contextvar():
    """Ensure the contextvar is False before and after every test."""
    _in_sandbox_execution.set(False)
    yield
    _in_sandbox_execution.set(False)


@pytest.fixture
def patched_open(manifest: StrategyManifest):
    """
    Simulate a *leaked* ``builtins.open`` monkey-patch.

    Installs ``_restricted_open`` onto ``builtins.open`` (mirroring what
    ``_activate_restrictions`` does) but does NOT set the contextvar to
    ``True`` — exactly the scenario of a patch that leaked from an
    incompletely-cleaned-up sandbox.  The guard must make this a no-op.
    """
    sandbox = StrategySandbox(_PassiveStrategy(), manifest)
    saved_open = builtins.open
    saved_io_open = _io_module.open
    sandbox._original_open = saved_open
    builtins.open = sandbox._restricted_open
    _io_module.open = sandbox._restricted_open
    try:
        assert _in_sandbox_execution.get() is False
        yield sandbox
    finally:
        builtins.open = saved_open
        _io_module.open = saved_io_open
        sandbox._original_open = None
        sandbox.cleanup()


# ── 1. _real_open identity and immutability ───────────────────────────


class TestRealOpenIdentity:
    """``_real_open`` must be the genuine builtin ``open`` captured at import."""

    def test_real_open_is_builtin_open(self) -> None:
        assert _real_open is builtins.open

    def test_real_open_is_io_open(self) -> None:
        assert _real_open is _io_module.open

    def test_real_open_is_builtin_function(self) -> None:
        import builtins as bi

        assert _real_open is bi.open
        assert type(_real_open).__name__ == "builtin_function_or_method"

    def test_real_open_survives_after_activation(self, manifest: StrategyManifest) -> None:
        """Activating restrictions must not change _real_open."""
        sandbox = StrategySandbox(_PassiveStrategy(), manifest)
        before = _real_open
        sandbox._activate_restrictions()
        try:
            assert _real_open is before
        finally:
            sandbox._deactivate_restrictions()
            sandbox.cleanup()

    def test_real_open_not_replaced_by_restricted_open(self, manifest: StrategyManifest) -> None:
        """_real_open must never point at a sandbox's _restricted_open."""
        sandbox = StrategySandbox(_PassiveStrategy(), manifest)
        sandbox._activate_restrictions()
        try:
            assert _real_open is not sandbox._restricted_open
        finally:
            sandbox._deactivate_restrictions()
            sandbox.cleanup()


# ── 2. Guard: contextvar False → unrestricted delegation ─────────────


class TestGuardDelegatesWhenInactive:
    """When ``_in_sandbox_execution`` is False, ``_restricted_open`` must be
    a pure passthrough to ``_real_open`` with no restriction logic."""

    def test_read_arbitrary_file(self, tmp_path: Path) -> None:
        target = tmp_path / "data.txt"
        target.write_text("payload")
        f = StrategySandbox(_PassiveStrategy(), StrategyManifest(
            id="t", name="t", version="1",
        ))._restricted_open(target, "r")
        assert f.read() == "payload"
        f.close()

    def test_write_arbitrary_file(self, tmp_path: Path) -> None:
        target = tmp_path / "out.txt"
        sandbox = StrategySandbox(_PassiveStrategy(), StrategyManifest(
            id="t", name="t", version="1",
        ))
        f = sandbox._restricted_open(target, "w")
        f.write("written")
        f.close()
        assert target.read_text() == "written"
        sandbox.cleanup()

    def test_append_mode_allowed(self, tmp_path: Path) -> None:
        target = tmp_path / "log.txt"
        target.write_text("line1\n")
        sandbox = StrategySandbox(_PassiveStrategy(), StrategyManifest(
            id="t", name="t", version="1",
        ))
        f = sandbox._restricted_open(target, "a")
        f.write("line2\n")
        f.close()
        assert target.read_text() == "line1\nline2\n"
        sandbox.cleanup()

    def test_update_mode_allowed(self, tmp_path: Path) -> None:
        target = tmp_path / "rw.txt"
        target.write_text("hello")
        sandbox = StrategySandbox(_PassiveStrategy(), StrategyManifest(
            id="t", name="t", version="1",
        ))
        f = sandbox._restricted_open(target, "r+")
        f.seek(0)
        f.write("H")
        f.close()
        assert target.read_text() == "Hello"
        sandbox.cleanup()

    def test_file_descriptor_allowed(self) -> None:
        """FD-based open must work when sandbox is inactive."""
        sandbox = StrategySandbox(_PassiveStrategy(), StrategyManifest(
            id="t", name="t", version="1",
        ))
        f = sandbox._restricted_open(0)
        f.close()
        sandbox.cleanup()

    def test_binary_mode_passthrough(self, tmp_path: Path) -> None:
        target = tmp_path / "bin.dat"
        target.write_bytes(b"\x00\x01\x02")
        sandbox = StrategySandbox(_PassiveStrategy(), StrategyManifest(
            id="t", name="t", version="1",
        ))
        f = sandbox._restricted_open(target, "rb")
        assert f.read() == b"\x00\x01\x02"
        f.close()
        sandbox.cleanup()

    def test_extra_kwargs_passthrough(self, tmp_path: Path) -> None:
        """encoding / errors / newline must pass through to _real_open."""
        target = tmp_path / "enc.txt"
        target.write_text("café", encoding="utf-8")
        sandbox = StrategySandbox(_PassiveStrategy(), StrategyManifest(
            id="t", name="t", version="1",
        ))
        f = sandbox._restricted_open(target, "r", encoding="utf-8")
        assert f.read() == "café"
        f.close()

        f2 = sandbox._restricted_open(target, "r", encoding="latin-1")
        assert f2.read() != "café"
        f2.close()
        sandbox.cleanup()

    def test_buffering_kwarg_passthrough(self, tmp_path: Path) -> None:
        target = tmp_path / "buf.txt"
        target.write_text("x" * 1000)
        sandbox = StrategySandbox(_PassiveStrategy(), StrategyManifest(
            id="t", name="t", version="1",
        ))
        f = sandbox._restricted_open(target, "r", buffering=1)
        assert len(f.read()) == 1000
        f.close()
        sandbox.cleanup()

    def test_returns_real_file_object(self, tmp_path: Path) -> None:
        """Return type must be a real TextIOWrapper / BufferedReader, not a proxy."""
        target = tmp_path / "f.txt"
        target.write_text("data")
        sandbox = StrategySandbox(_PassiveStrategy(), StrategyManifest(
            id="t", name="t", version="1",
        ))
        result = sandbox._restricted_open(target, "r")
        assert isinstance(result, _io_module.TextIOWrapper)
        result.close()

        result_bin = sandbox._restricted_open(target, "rb")
        assert isinstance(result_bin, _io_module.BufferedReader)
        result_bin.close()
        sandbox.cleanup()

    def test_path_object_argument(self, tmp_path: Path) -> None:
        target = tmp_path / "p.txt"
        target.write_text("ok")
        sandbox = StrategySandbox(_PassiveStrategy(), StrategyManifest(
            id="t", name="t", version="1",
        ))
        f = sandbox._restricted_open(target)
        assert f.read() == "ok"
        f.close()
        sandbox.cleanup()

    def test_string_path_argument(self, tmp_path: Path) -> None:
        target = tmp_path / "s.txt"
        target.write_text("ok")
        sandbox = StrategySandbox(_PassiveStrategy(), StrategyManifest(
            id="t", name="t", version="1",
        ))
        f = sandbox._restricted_open(str(target))
        assert f.read() == "ok"
        f.close()
        sandbox.cleanup()

    def test_symlink_outside_workdir_allowed(self, tmp_path: Path) -> None:
        """Symlinks resolve normally when sandbox is inactive."""
        real = tmp_path / "real.txt"
        real.write_text("real")
        link = tmp_path / "link.txt"
        link.symlink_to(real)
        sandbox = StrategySandbox(_PassiveStrategy(), StrategyManifest(
            id="t", name="t", version="1",
        ))
        f = sandbox._restricted_open(link)
        assert f.read() == "real"
        f.close()
        sandbox.cleanup()


# ── 3. Guard: contextvar True → restrictions enforced ────────────────


class TestGuardEnforcesWhenActive:
    """When ``_in_sandbox_execution`` is True, the full path/write logic runs."""

    def test_disallowed_path_blocked(self, manifest: StrategyManifest, tmp_path: Path) -> None:
        secret = tmp_path / "secret"
        secret.write_text("x")
        sandbox = StrategySandbox(_PassiveStrategy(), manifest)
        _in_sandbox_execution.set(True)
        try:
            with pytest.raises(PermissionError, match="not allowed"):
                sandbox._restricted_open(str(secret))
        finally:
            _in_sandbox_execution.set(False)
            sandbox.cleanup()

    def test_write_blocked_even_in_workdir(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_PassiveStrategy(), manifest)
        work_file = os.path.join(sandbox._work_dir, "w.txt")
        _in_sandbox_execution.set(True)
        try:
            with pytest.raises(PermissionError, match="Write access"):
                sandbox._restricted_open(work_file, "w")
        finally:
            _in_sandbox_execution.set(False)
            sandbox.cleanup()

    def test_append_blocked_even_in_workdir(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_PassiveStrategy(), manifest)
        work_file = os.path.join(sandbox._work_dir, "a.txt")
        _in_sandbox_execution.set(True)
        try:
            with pytest.raises(PermissionError, match="Write access"):
                sandbox._restricted_open(work_file, "a")
        finally:
            _in_sandbox_execution.set(False)
            sandbox.cleanup()

    def test_fd_blocked(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_PassiveStrategy(), manifest)
        _in_sandbox_execution.set(True)
        try:
            with pytest.raises(PermissionError, match="File descriptor"):
                sandbox._restricted_open(0)
        finally:
            _in_sandbox_execution.set(False)
            sandbox.cleanup()

    def test_read_in_workdir_allowed(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_PassiveStrategy(), manifest)
        sandbox._original_open = _real_open
        work_file = os.path.join(sandbox._work_dir, "ok.txt")
        with _real_open(work_file, "w") as f:
            f.write("ok")
        _in_sandbox_execution.set(True)
        try:
            f = sandbox._restricted_open(work_file, "r")
            assert f.read() == "ok"
            f.close()
        finally:
            _in_sandbox_execution.set(False)
            sandbox._original_open = None
            sandbox.cleanup()

    def test_read_artifact_allowed(self, tmp_path: Path) -> None:
        artifact = tmp_path / "model.bin"
        artifact.write_bytes(b"\x42")
        manifest = StrategyManifest(
            id="t", name="t", version="1",
            artifacts=[str(artifact)],
        )
        sandbox = StrategySandbox(_PassiveStrategy(), manifest)
        sandbox._original_open = _real_open
        _in_sandbox_execution.set(True)
        try:
            f = sandbox._restricted_open(str(artifact), "rb")
            assert f.read() == b"\x42"
            f.close()
        finally:
            _in_sandbox_execution.set(False)
            sandbox._original_open = None
            sandbox.cleanup()

    def test_read_artifact_dir_child_allowed(self, tmp_path: Path) -> None:
        art_dir = tmp_path / "models"
        art_dir.mkdir()
        child = art_dir / "weights.bin"
        child.write_bytes(b"\x00")
        manifest = StrategyManifest(
            id="t", name="t", version="1",
            artifacts=[str(art_dir)],
        )
        sandbox = StrategySandbox(_PassiveStrategy(), manifest)
        sandbox._original_open = _real_open
        _in_sandbox_execution.set(True)
        try:
            f = sandbox._restricted_open(str(child), "rb")
            assert f.read() == b"\x00"
            f.close()
        finally:
            _in_sandbox_execution.set(False)
            sandbox._original_open = None
            sandbox.cleanup()


# ── 4. Leaked monkey-patch is a no-op ────────────────────────────────


class TestLeakedPatchIsNoOp:
    """The exact scenario that caused 902 errors: a leaked ``builtins.open``
    patch that still routes through ``_restricted_open``, but the contextvar
    is ``False``."""

    def test_open_reads_any_file(self, patched_open: StrategySandbox, tmp_path: Path) -> None:
        target = tmp_path / "leak.txt"
        target.write_text("survived")
        with builtins.open(target) as f:
            assert f.read() == "survived"

    def test_open_reads_system_file(self, patched_open: StrategySandbox) -> None:
        with builtins.open(os.__file__) as f:
            assert "import" in f.read()

    def test_open_writes_any_file(self, patched_open: StrategySandbox, tmp_path: Path) -> None:
        target = tmp_path / "w.txt"
        with builtins.open(target, "w") as f:
            f.write("written")
        assert target.read_text() == "written"

    def test_open_via_io_module(self, patched_open: StrategySandbox, tmp_path: Path) -> None:
        target = tmp_path / "io.txt"
        target.write_text("io-ok")
        with _io_module.open(target) as f:  # noqa: UP020
            assert f.read() == "io-ok"

    def test_open_fd_via_io_module(self, patched_open: StrategySandbox, tmp_path: Path) -> None:
        target = tmp_path / "fd.txt"
        target.write_text("fd-ok")
        raw = _real_open(target)
        fd = raw.fileno()
        with _io_module.open(fd) as f:  # noqa: UP020
            assert f.read() == "fd-ok"

    def test_tempfile_works(self, patched_open: StrategySandbox) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as tf:
            tf.write("temp")
            path = tf.name
        with builtins.open(path) as f:
            assert f.read() == "temp"
        os.unlink(path)

    def test_multiple_consecutive_opens(self, patched_open: StrategySandbox, tmp_path: Path) -> None:
        for i in range(20):
            target = tmp_path / f"f{i}.txt"
            target.write_text(str(i))
            with builtins.open(target) as f:
                assert f.read() == str(i)

    def test_coverage_html_generation_simulation(
        self, patched_open: StrategySandbox, tmp_path: Path,
    ) -> None:
        """Simulate coverage.py writing HTML: many writes to unknown paths."""
        out_dir = tmp_path / "htmlcov"
        out_dir.mkdir()
        for name in ("index.html", "engine___plugins___sandbox_py.html", "style.css"):
            target = out_dir / name
            with builtins.open(target, "w", encoding="utf-8") as f:
                f.write(f"<html>{name}</html>")
            assert target.exists()


# ── 5. Transition boundary: False → True → False ─────────────────────


class TestContextVarTransitionBoundary:
    """The guard must react instantly to contextvar changes — no caching."""

    def test_same_instance_delegates_then_enforces_then_delegates(
        self, manifest: StrategyManifest, tmp_path: Path,
    ) -> None:
        secret = tmp_path / "secret"
        secret.write_text("s")
        sandbox = StrategySandbox(_PassiveStrategy(), manifest)
        sandbox._original_open = builtins.open

        # Phase 1: inactive — unrestricted
        f = sandbox._restricted_open(str(secret))
        assert f.read() == "s"
        f.close()

        # Phase 2: active — restricted
        _in_sandbox_execution.set(True)
        with pytest.raises(PermissionError):
            sandbox._restricted_open(str(secret))

        # Phase 3: inactive again — unrestricted
        _in_sandbox_execution.set(False)
        f = sandbox._restricted_open(str(secret))
        assert f.read() == "s"
        f.close()

        sandbox.cleanup()

    def test_rapid_toggle(self, manifest: StrategyManifest, tmp_path: Path) -> None:
        secret = tmp_path / "s"
        secret.write_text("x")
        sandbox = StrategySandbox(_PassiveStrategy(), manifest)
        sandbox._original_open = builtins.open
        try:
            for _ in range(10):
                _in_sandbox_execution.set(True)
                with pytest.raises(PermissionError):
                    sandbox._restricted_open(str(secret))
                _in_sandbox_execution.set(False)
                f = sandbox._restricted_open(str(secret))
                f.close()
        finally:
            sandbox.cleanup()

    def test_workdir_allowed_only_when_active(self, manifest: StrategyManifest) -> None:
        """In sandbox mode, workdir reads are allowed; in non-sandbox, any path is."""
        sandbox = StrategySandbox(_PassiveStrategy(), manifest)
        sandbox._original_open = builtins.open
        work_file = os.path.join(sandbox._work_dir, "wf.txt")
        with _real_open(work_file, "w") as f:
            f.write("wf")

        # Inactive: reads workdir AND any external path
        f = sandbox._restricted_open(work_file)
        assert f.read() == "wf"
        f.close()

        _in_sandbox_execution.set(True)
        try:
            # Active: workdir still allowed
            f = sandbox._restricted_open(work_file)
            assert f.read() == "wf"
            f.close()
            # Active: external path blocked
            with pytest.raises(PermissionError):
                sandbox._restricted_open("/etc/hostname")
        finally:
            _in_sandbox_execution.set(False)
            sandbox.cleanup()


# ── 6. Full evaluation lifecycle ──────────────────────────────────────


class TestEvaluationLifecycle:
    """End-to-end: ``safe_evaluate`` toggles the contextvar correctly."""

    async def test_open_works_during_evaluation_in_workdir(
        self, manifest: StrategyManifest,
    ) -> None:
        sandbox = StrategySandbox(_PassiveStrategy(), manifest)
        work_file = os.path.join(sandbox._work_dir, "data.txt")
        with _real_open(work_file, "w") as f:
            f.write("data")

        class ReadWorkDirStrategy:
            name = "rwd"
            version = "1.0.0"

            def on_bar(self, _s, _p):
                with open(work_file) as fh:
                    fh.read()
                return []

        sandbox.strategy = ReadWorkDirStrategy()
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors == 0
        finally:
            sandbox.cleanup()

    async def test_open_blocked_during_evaluation_outside_workdir(
        self, manifest: StrategyManifest, tmp_path: Path,
    ) -> None:
        secret = tmp_path / "secret.txt"
        secret.write_text("s")
        sandbox = StrategySandbox(_OpenStrategy(str(secret)), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors == 1
            assert "not allowed" in (sandbox.metrics.last_error or "").lower()
        finally:
            sandbox.cleanup()

    async def test_open_unrestricted_after_evaluation(
        self, manifest: StrategyManifest, tmp_path: Path,
    ) -> None:
        secret = tmp_path / "post.txt"
        secret.write_text("post")
        sandbox = StrategySandbox(_PassiveStrategy(), manifest)
        try:
            await sandbox.safe_evaluate(None, None, None)
            # After eval, contextvar is False, so open works everywhere
            with open(secret) as f:
                assert f.read() == "post"
            with open(os.__file__) as f:
                assert "import" in f.read()
            assert _in_sandbox_execution.get() is False
        finally:
            sandbox.cleanup()

    async def test_open_unrestricted_after_crashed_evaluation(
        self, manifest: StrategyManifest, tmp_path: Path,
    ) -> None:
        secret = tmp_path / "crash.txt"
        secret.write_text("c")

        class CrashStrategy:
            name = "crash"
            version = "1.0.0"

            def on_bar(self, _s, _p):
                raise RuntimeError("boom")

        sandbox = StrategySandbox(CrashStrategy(), manifest)
        try:
            await sandbox.safe_evaluate(None, None, None)
            assert sandbox.metrics.errors == 1
            with open(secret) as f:
                assert f.read() == "c"
            assert _in_sandbox_execution.get() is False
        finally:
            sandbox.cleanup()

    async def test_open_unrestricted_after_timeout(
        self, manifest: StrategyManifest, tmp_path: Path,
    ) -> None:
        secret = tmp_path / "to.txt"
        secret.write_text("t")

        class TimeoutStrategy:
            name = "to"
            version = "1.0.0"

            async def on_bar(self, _s, _p):
                await asyncio.sleep(60)
                return []

        sandbox = StrategySandbox(TimeoutStrategy(), manifest)
        try:
            await sandbox.safe_evaluate(None, None, None)
            assert sandbox.metrics.errors == 1
            with open(secret) as f:
                assert f.read() == "t"
            assert _in_sandbox_execution.get() is False
        finally:
            sandbox.cleanup()

    async def test_multiple_evaluations_preserve_guard(
        self, manifest: StrategyManifest,
    ) -> None:
        sandbox = StrategySandbox(_PassiveStrategy(), manifest)
        try:
            for _ in range(5):
                await sandbox.safe_evaluate(None, None, None)
                assert _in_sandbox_execution.get() is False
        finally:
            sandbox.cleanup()


# ── 7. io.open patch also guarded ────────────────────────────────────


class TestIoOpenPatchGuarded:
    """``_activate_restrictions`` also patches ``io.open``.  The guard must
    apply there too."""

    def test_io_open_delegates_when_inactive(self, manifest: StrategyManifest, tmp_path: Path) -> None:
        target = tmp_path / "io.txt"
        target.write_text("io-data")
        sandbox = StrategySandbox(_PassiveStrategy(), manifest)
        saved = _io_module.open
        sandbox._original_io_open = saved
        _io_module.open = sandbox._restricted_open
        try:
            assert _in_sandbox_execution.get() is False
            with _io_module.open(target, "w") as f:  # noqa: UP020
                f.write("overwritten")
            assert target.read_text() == "overwritten"
        finally:
            _io_module.open = saved
            sandbox._original_io_open = None
            sandbox.cleanup()

    def test_io_open_enforces_when_active(self, manifest: StrategyManifest, tmp_path: Path) -> None:
        secret = tmp_path / "io-secret"
        secret.write_text("x")
        sandbox = StrategySandbox(_PassiveStrategy(), manifest)
        saved = _io_module.open
        sandbox._original_io_open = saved
        _io_module.open = sandbox._restricted_open
        try:
            _in_sandbox_execution.set(True)
            with pytest.raises(PermissionError):
                _io_module.open(str(secret))  # noqa: UP020, SIM115
        finally:
            _in_sandbox_execution.set(False)
            _io_module.open = saved
            sandbox._original_io_open = None
            sandbox.cleanup()


# ── 8. Concurrent task isolation ──────────────────────────────────────


class TestConcurrentTaskIsolation:
    """ContextVar is per-task: one task in sandbox must not affect another."""

    async def test_concurrent_sandbox_and_normal_open(
        self, manifest: StrategyManifest, tmp_path: Path,
    ) -> None:
        target = tmp_path / "concurrent.txt"
        target.write_text("concurrent-ok")

        sandbox = StrategySandbox(_PassiveStrategy(), manifest)

        async def sandbox_task():
            await sandbox.safe_evaluate(None, None, None)

        async def normal_task():
            # While the sandbox might be evaluating, this task's contextvar
            # is independent (default False), so open must work.
            for _ in range(5):
                await asyncio.sleep(0)
                with open(target) as f:
                    assert f.read() == "concurrent-ok"
                assert _in_sandbox_execution.get() is False

        try:
            await asyncio.gather(sandbox_task(), normal_task())
        finally:
            sandbox.cleanup()

    async def test_two_sandboxes_sequentially(self, manifest: StrategyManifest) -> None:
        s1 = StrategySandbox(_PassiveStrategy(), manifest)
        s2 = StrategySandbox(_PassiveStrategy(), manifest)
        try:
            await s1.safe_evaluate(None, None, None)
            assert _in_sandbox_execution.get() is False
            await s2.safe_evaluate(None, None, None)
            assert _in_sandbox_execution.get() is False
        finally:
            s1.cleanup()
            s2.cleanup()


# ── 9. from_factory lifecycle ────────────────────────────────────────


class TestFromFactoryLifecycle:
    """``from_factory`` activates/deactivates during instantiation."""

    def test_context_var_false_after_factory(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox.from_factory(_PassiveStrategy, manifest)
        try:
            assert _in_sandbox_execution.get() is False
        finally:
            sandbox.cleanup()

    def test_open_unrestricted_after_factory(self, manifest: StrategyManifest, tmp_path: Path) -> None:
        target = tmp_path / "factory.txt"
        target.write_text("f")
        sandbox = StrategySandbox.from_factory(_PassiveStrategy, manifest)
        try:
            with open(target) as f:
                assert f.read() == "f"
        finally:
            sandbox.cleanup()


# ── 10. Path traversal & prefix matching (active sandbox only) ───────


class TestPathTraversalActiveOnly:
    """Security-critical: path traversal and prefix attacks blocked only
    when the contextvar is True."""

    def test_dotdot_traversal_blocked_in_sandbox(
        self, manifest: StrategyManifest, tmp_path: Path,
    ) -> None:
        sandbox = StrategySandbox(_PassiveStrategy(), manifest)
        # Build a path that escapes the workdir via ..
        escape = os.path.join(sandbox._work_dir, "..", "..", "etc", "hostname")
        _in_sandbox_execution.set(True)
        try:
            with pytest.raises(PermissionError):
                sandbox._restricted_open(escape)
        finally:
            _in_sandbox_execution.set(False)
            sandbox.cleanup()

    def test_dotdot_traversal_allowed_outside_sandbox(
        self, manifest: StrategyManifest, tmp_path: Path,
    ) -> None:
        sandbox = StrategySandbox(_PassiveStrategy(), manifest)
        escape = os.path.join(sandbox._work_dir, "..", "..", "etc", "hostname")
        # Outside sandbox, should not raise
        f = sandbox._restricted_open(escape)
        f.close()
        sandbox.cleanup()

    def test_prefix_confusion_blocked_in_sandbox(self, tmp_path: Path) -> None:
        """``/tmp/data`` must not match artifact ``/tmp/database``."""
        art_dir = tmp_path / "data"
        art_dir.mkdir()
        evil = tmp_path / "database"
        evil.mkdir()
        secret = evil / "secret.txt"
        secret.write_text("stolen")
        manifest = StrategyManifest(
            id="t", name="t", version="1",
            artifacts=[str(art_dir)],
        )
        sandbox = StrategySandbox(_PassiveStrategy(), manifest)
        _in_sandbox_execution.set(True)
        try:
            with pytest.raises(PermissionError):
                sandbox._restricted_open(str(secret))
        finally:
            _in_sandbox_execution.set(False)
            sandbox.cleanup()


# ── 11. Import-time capture verified ─────────────────────────────────


class TestImportTimeCapture:
    """Verify ``_real_open`` is captured at import time and survives reload."""

    def test_real_open_captured_before_any_patch(self) -> None:
        """Even before any StrategySandbox is created, _real_open is valid."""
        f = _real_open(os.__file__)
        content = f.read()
        f.close()
        assert "import" in content

    def test_real_open_stable_across_sandbox_instances(self, manifest: StrategyManifest) -> None:
        """_real_open must not change no matter how many sandboxes are created."""
        before = _real_open
        for _ in range(3):
            s = StrategySandbox(_PassiveStrategy(), manifest)
            s._activate_restrictions()
            assert _real_open is before
            s._deactivate_restrictions()
            s.cleanup()
        assert _real_open is before

    def test_real_open_is_callable(self) -> None:
        assert callable(_real_open)

    def test_real_open_name(self) -> None:
        assert _real_open.__name__ == "open"


# ── 12. No restriction side-effects when inactive ────────────────────


class TestNoSideEffectsWhenInactive:
    """Calling ``_restricted_open`` with contextvar=False must have zero
    observable difference from calling the real ``open`` directly."""

    def test_same_return_identity_as_real_open(self, tmp_path: Path) -> None:
        target = tmp_path / "id.txt"
        target.write_text("id")
        sandbox = StrategySandbox(_PassiveStrategy(), StrategyManifest(
            id="t", name="t", version="1",
        ))
        result = sandbox._restricted_open(target)
        direct = _real_open(target)
        try:
            assert type(result) is type(direct)
            assert result.read() == direct.read()
        finally:
            result.close()
            direct.close()
            sandbox.cleanup()

    def test_no_metrics_changed(self, manifest: StrategyManifest, tmp_path: Path) -> None:
        target = tmp_path / "m.txt"
        target.write_text("m")
        sandbox = StrategySandbox(_PassiveStrategy(), manifest)
        errors_before = sandbox.metrics.errors
        f = sandbox._restricted_open(str(target), "w")
        f.write("x")
        f.close()
        assert sandbox.metrics.errors == errors_before
        sandbox.cleanup()

    def test_does_not_inspect_manifest_artifacts(self, manifest: StrategyManifest, tmp_path: Path) -> None:
        """When inactive, the manifest's artifacts list is never read.
        We verify by using a manifest whose artifacts don't exist."""
        manifest = StrategyManifest(
            id="t", name="t", version="1",
            artifacts=["/nonexistent/path/that/should/not/be/touched"],
        )
        sandbox = StrategySandbox(_PassiveStrategy(), manifest)
        target = tmp_path / "ok.txt"
        target.write_text("ok")
        # If the guard weren't there, os.path.realpath on the nonexistent
        # artifact would still work but the target path wouldn't be in the
        # allowed list and would raise PermissionError.
        f = sandbox._restricted_open(target)
        assert f.read() == "ok"
        f.close()
        sandbox.cleanup()
