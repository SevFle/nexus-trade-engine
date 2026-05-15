from __future__ import annotations

import builtins
from typing import Any

import pytest

from engine.core.signal import Signal
from engine.plugins.manifest import StrategyManifest
from engine.plugins.sandbox import StrategySandbox


class _MaliciousBase:
    name = "malicious"
    version = "1.0.0"


class _EscapeViaSubclassChain(_MaliciousBase):
    def on_bar(self, _s: Any, _p: Any) -> list[Any]:
        for _ in builtins.object.__subclasses__():  # type: ignore[type-arg]
            pass
        return []


class _EscapeViaGlobalsAccess(_MaliciousBase):
    def on_bar(self, _s: Any, _p: Any) -> list[Any]:
        fn = self.on_bar
        _ = builtins.getattr(fn, "__globals__")  # noqa: B009
        return []


class _EscapeViaFilesystem(_MaliciousBase):
    def on_bar(self, _s: Any, _p: Any) -> list[Any]:
        with open("/etc/passwd") as f:
            f.read()
        return []


class _EscapeViaImport(_MaliciousBase):
    def on_bar(self, _s: Any, _p: Any) -> list[Any]:
        import subprocess  # noqa: F401

        return []


class _EscapeViaEval(_MaliciousBase):
    def on_bar(self, _s: Any, _p: Any) -> list[Any]:
        builtins.eval("__import__('os').system('id')")  # noqa: S307
        return []


class _EscapeViaExec(_MaliciousBase):
    def on_bar(self, _s: Any, _p: Any) -> list[Any]:
        builtins.exec("import os")  # noqa: S102
        return []


class _EscapeViaMro(_MaliciousBase):
    def on_bar(self, _s: Any, _p: Any) -> list[Any]:
        _ = builtins.getattr(int, "__mro__")  # noqa: B009
        return []


class _EscapeViaWrite(_MaliciousBase):
    def on_bar(self, _s: Any, _p: Any) -> list[Any]:
        with open("/tmp/combined_attack_write", "w") as f:  # noqa: S108
            f.write("pwned")
        return []


class _EscapeViaCompile(_MaliciousBase):
    def on_bar(self, _s: Any, _p: Any) -> list[Any]:
        builtins.compile("import os", "<mal>", "exec")
        return []


class _WellBehavedStrategy(_MaliciousBase):
    def on_bar(self, _s: Any, _p: Any) -> list[Any]:
        return [Signal.buy(symbol="AAPL", strategy_id=self.name)]


@pytest.fixture
def manifest() -> StrategyManifest:
    return StrategyManifest(
        id="test_combined",
        name="test_combined",
        version="1.0.0",
        resources={"max_cpu_seconds": 2},
    )


def _assert_blocked(sandbox: StrategySandbox) -> None:
    assert sandbox.metrics.errors >= 1


class TestSubclassChainEscape:
    async def test_object_subclasses_blocked(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_EscapeViaSubclassChain(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            _assert_blocked(sandbox)
        finally:
            sandbox.cleanup()


class TestGlobalsAccessEscape:
    async def test_globals_access_blocked(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_EscapeViaGlobalsAccess(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            _assert_blocked(sandbox)
        finally:
            sandbox.cleanup()


class TestFilesystemEscape:
    async def test_etc_passwd_read_blocked(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_EscapeViaFilesystem(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            _assert_blocked(sandbox)
        finally:
            sandbox.cleanup()


class TestImportEscape:
    async def test_subprocess_import_blocked(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_EscapeViaImport(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            _assert_blocked(sandbox)
        finally:
            sandbox.cleanup()


class TestEvalEscape:
    async def test_eval_blocked(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_EscapeViaEval(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            _assert_blocked(sandbox)
        finally:
            sandbox.cleanup()


class TestExecEscape:
    async def test_exec_blocked(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_EscapeViaExec(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            _assert_blocked(sandbox)
        finally:
            sandbox.cleanup()


class TestMroEscape:
    async def test_mro_access_blocked(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_EscapeViaMro(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            _assert_blocked(sandbox)
        finally:
            sandbox.cleanup()


class TestWriteEscape:
    async def test_write_blocked(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_EscapeViaWrite(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            _assert_blocked(sandbox)
        finally:
            sandbox.cleanup()


class TestCompileEscape:
    async def test_compile_blocked(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_EscapeViaCompile(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            _assert_blocked(sandbox)
        finally:
            sandbox.cleanup()


class TestWellBehavedPasses:
    async def test_good_strategy_passes(self, manifest: StrategyManifest) -> None:
        sandbox = StrategySandbox(_WellBehavedStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert len(signals) == 1
            assert signals[0].symbol == "AAPL"
            assert sandbox.metrics.errors == 0
        finally:
            sandbox.cleanup()


class TestCleanupAfterBlock:
    async def test_builtins_restored_after_violation(self, manifest: StrategyManifest) -> None:
        original_import = builtins.__import__
        original_open = builtins.open
        original_object = builtins.object

        sandbox = StrategySandbox(_EscapeViaImport(), manifest)
        try:
            await sandbox.safe_evaluate(None, None, None)
            assert builtins.__import__ is original_import
            assert builtins.open is original_open
            assert builtins.object is original_object
        finally:
            sandbox.cleanup()

    async def test_builtins_restored_after_eval_block(self, manifest: StrategyManifest) -> None:
        original_import = builtins.__import__
        original_open = builtins.open
        original_object = builtins.object

        sandbox = StrategySandbox(_EscapeViaEval(), manifest)
        try:
            await sandbox.safe_evaluate(None, None, None)
            assert builtins.__import__ is original_import
            assert builtins.open is original_open
            assert builtins.object is original_object
        finally:
            sandbox.cleanup()

    async def test_builtins_restored_after_fs_block(self, manifest: StrategyManifest) -> None:
        original_import = builtins.__import__
        original_open = builtins.open
        original_object = builtins.object

        sandbox = StrategySandbox(_EscapeViaFilesystem(), manifest)
        try:
            await sandbox.safe_evaluate(None, None, None)
            assert builtins.__import__ is original_import
            assert builtins.open is original_open
            assert builtins.object is original_object
        finally:
            sandbox.cleanup()
