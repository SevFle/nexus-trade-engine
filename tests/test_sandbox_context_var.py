"""
Tests for the ContextVar-based network restriction in StrategySandbox.

The _in_sandbox_execution ContextVar ensures that the global
httpx.AsyncClient.send monkey-patch only enforces network restrictions
when the code is executing inside a sandbox evaluation.  Outside of
sandbox execution, httpx requests proceed normally — this prevents the
restricted send from leaking into test infrastructure, health checks,
or any other code that uses httpx.

Tests cover:
  1. ContextVar default is False (network allowed)
  2. Network allowed outside sandbox execution
  3. Network blocked inside sandbox for non-whitelisted hosts
  4. ContextVar scoped per asyncio task (concurrent isolation)
  5. ContextVar reset after normal evaluation
  6. ContextVar reset after error evaluation
  7. ContextVar reset after timeout
  8. cleanup() resets ContextVar
  9. httpx monkey-patch does not leak after deactivation
  10. Integration: tax/webhook tests can use httpx without restriction
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import contextvars
import pathlib
from typing import Any

import httpx
import pytest

from engine.core.signal import Signal
from engine.plugins.manifest import StrategyManifest
from engine.plugins.sandbox import (
    StrategySandbox,
    _in_sandbox_execution,
)


class _PassiveStrategy:
    name = "passive"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Signal]:
        return []


class _CrashingStrategy:
    name = "crasher"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        raise RuntimeError("boom")


class _SlowStrategy:
    name = "slow"
    version = "1.0.0"

    async def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        await asyncio.sleep(60)
        return []


class _DirectHttpxToEvilStrategy:
    name = "httpx_evil"
    version = "1.0.0"

    async def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        transport = httpx.MockTransport(
            lambda _r: httpx.Response(200)
        )
        async with httpx.AsyncClient(transport=transport) as client:
            await client.get("https://evil.com/api")
        return []


class _DirectHttpxToAllowedStrategy:
    name = "httpx_allowed"
    version = "1.0.0"

    async def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        transport = httpx.MockTransport(
            lambda _r: httpx.Response(200, json={"ok": True})
        )
        async with httpx.AsyncClient(transport=transport) as client:
            await client.get("https://api.anthropic.com/v1/models")
        return []


@pytest.fixture
def manifest() -> StrategyManifest:
    return StrategyManifest(
        id="test",
        name="test",
        version="1.0.0",
        resources={"max_cpu_seconds": 1},
    )


@pytest.fixture
def networked_manifest() -> StrategyManifest:
    return StrategyManifest(
        id="test",
        name="test",
        version="1.0.0",
        resources={"max_cpu_seconds": 5},
        network={"allowed_endpoints": ["api.anthropic.com"]},
    )


# ── 1. ContextVar default ─────────────────────────────────────────────


class TestContextVarDefault:
    def test_default_is_false(self):
        assert _in_sandbox_execution.get() is False

    def test_get_with_explicit_default_false(self):
        assert _in_sandbox_execution.get(False) is False

    def test_context_var_name(self):
        assert _in_sandbox_execution.name == "_in_sandbox_execution"

    def test_fresh_context_has_false(self):
        ctx = contextvars.copy_context()
        result = ctx.run(_in_sandbox_execution.get)
        assert result is False

    async def test_new_task_has_default_false(self):
        async def check():
            return _in_sandbox_execution.get()

        result = await asyncio.create_task(check())
        assert result is False


# ── 2. Network allowed outside sandbox ────────────────────────────────


class TestNetworkAllowedOutsideSandbox:
    async def test_httpx_works_without_sandbox(self):
        transport = httpx.MockTransport(
            lambda _r: httpx.Response(200, json={"ok": True})
        )
        async with httpx.AsyncClient(transport=transport) as client:
            resp = await client.get("https://evil.com/api")
            assert resp.status_code == 200

    async def test_httpx_works_with_sandbox_instance_inactive(
        self, networked_manifest: StrategyManifest
    ):
        sandbox = StrategySandbox(_PassiveStrategy(), networked_manifest)
        try:
            transport = httpx.MockTransport(
                lambda _r: httpx.Response(200, json={"ok": True})
            )
            async with httpx.AsyncClient(transport=transport) as client:
                resp = await client.get("https://evil.com/api")
                assert resp.status_code == 200
        finally:
            sandbox.cleanup()

    async def test_httpx_works_after_sandbox_evaluation(
        self, networked_manifest: StrategyManifest
    ):
        sandbox = StrategySandbox(_PassiveStrategy(), networked_manifest)
        try:
            await sandbox.safe_evaluate(None, None, None)
            transport = httpx.MockTransport(
                lambda _r: httpx.Response(200, json={"ok": True})
            )
            async with httpx.AsyncClient(transport=transport) as client:
                resp = await client.get("https://evil.com/api")
                assert resp.status_code == 200
        finally:
            sandbox.cleanup()


# ── 3. Network blocked inside sandbox ─────────────────────────────────


class TestNetworkBlockedInsideSandbox:
    async def test_blocked_host_raises_in_sandbox(
        self, networked_manifest: StrategyManifest
    ):
        sandbox = StrategySandbox(
            _DirectHttpxToEvilStrategy(), networked_manifest
        )
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors == 1
            assert "not allowed" in (
                sandbox.metrics.last_error or ""
            ).lower()
        finally:
            sandbox.cleanup()

    async def test_allowed_host_passes_in_sandbox(
        self, networked_manifest: StrategyManifest
    ):
        sandbox = StrategySandbox(
            _DirectHttpxToAllowedStrategy(), networked_manifest
        )
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors == 0
        finally:
            sandbox.cleanup()


# ── 4. ContextVar scoped per asyncio task ─────────────────────────────


class TestContextVarPerTaskIsolation:
    async def test_context_var_independent_across_tasks(
        self, networked_manifest: StrategyManifest
    ):
        results: list[bool] = []

        async def task_check_context():
            results.append(_in_sandbox_execution.get())
            await asyncio.sleep(0.01)
            results.append(_in_sandbox_execution.get())

        async def task_set_context():
            _in_sandbox_execution.set(True)
            await asyncio.sleep(0.02)
            val = _in_sandbox_execution.get()
            _in_sandbox_execution.set(False)
            return val

        t1 = asyncio.create_task(task_check_context())
        t2 = asyncio.create_task(task_set_context())
        await asyncio.gather(t1, t2)

        assert all(v is False for v in results)

    async def test_concurrent_sandbox_evaluations_isolated(
        self, networked_manifest: StrategyManifest
    ):
        sandbox_a = StrategySandbox(
            _PassiveStrategy(), networked_manifest
        )
        sandbox_b = StrategySandbox(
            _PassiveStrategy(), networked_manifest
        )
        try:
            await asyncio.gather(
                sandbox_a.safe_evaluate(None, None, None),
                sandbox_b.safe_evaluate(None, None, None),
            )
            assert _in_sandbox_execution.get() is False
        finally:
            sandbox_a.cleanup()
            sandbox_b.cleanup()


# ── 5. ContextVar reset after normal evaluation ───────────────────────


class TestContextVarResetAfterNormal:
    async def test_reset_after_successful_eval(
        self, manifest: StrategyManifest
    ):
        sandbox = StrategySandbox(_PassiveStrategy(), manifest)
        try:
            assert _in_sandbox_execution.get() is False
            await sandbox.safe_evaluate(None, None, None)
            assert _in_sandbox_execution.get() is False
        finally:
            sandbox.cleanup()

    async def test_reset_after_signal_emitting_eval(
        self, manifest: StrategyManifest
    ):
        class SignalStrategy:
            name = "signal_strat"
            version = "1.0.0"

            def on_bar(self, _s, _p):
                return [Signal.buy(symbol="AAPL", strategy_id=self.name)]

        sandbox = StrategySandbox(SignalStrategy(), manifest)
        try:
            await sandbox.safe_evaluate(None, None, None)
            assert _in_sandbox_execution.get() is False
            assert sandbox.metrics.total_evaluations == 1
        finally:
            sandbox.cleanup()


# ── 6. ContextVar reset after error evaluation ────────────────────────


class TestContextVarResetAfterError:
    async def test_reset_after_runtime_error(
        self, manifest: StrategyManifest
    ):
        sandbox = StrategySandbox(_CrashingStrategy(), manifest)
        try:
            assert _in_sandbox_execution.get() is False
            await sandbox.safe_evaluate(None, None, None)
            assert _in_sandbox_execution.get() is False
            assert sandbox.metrics.errors == 1
        finally:
            sandbox.cleanup()

    async def test_reset_after_import_error(
        self, manifest: StrategyManifest
    ):
        class ImportOsStrategy:
            name = "import_os"
            version = "1.0.0"

            def on_bar(self, _s, _p):
                import os  # noqa: F401
                return []

        sandbox = StrategySandbox(ImportOsStrategy(), manifest)
        try:
            await sandbox.safe_evaluate(None, None, None)
            assert _in_sandbox_execution.get() is False
            assert sandbox.metrics.errors == 1
        finally:
            sandbox.cleanup()


# ── 7. ContextVar reset after timeout ─────────────────────────────────


class TestContextVarResetAfterTimeout:
    async def test_reset_after_timeout(self, manifest: StrategyManifest):
        sandbox = StrategySandbox(_SlowStrategy(), manifest)
        try:
            assert _in_sandbox_execution.get() is False
            await sandbox.safe_evaluate(None, None, None)
            assert _in_sandbox_execution.get() is False
            assert sandbox.metrics.errors == 1
            assert "Timeout" in (sandbox.metrics.last_error or "")
        finally:
            sandbox.cleanup()


# ── 8. cleanup() resets ContextVar ────────────────────────────────────


class TestCleanupResetsContextVar:
    def test_cleanup_resets_after_activate(
        self, manifest: StrategyManifest
    ):
        sandbox = StrategySandbox(_PassiveStrategy(), manifest)
        sandbox._activate_restrictions()
        assert _in_sandbox_execution.get() is True
        sandbox.cleanup()
        assert _in_sandbox_execution.get() is False

    def test_deactivate_resets_context_var(
        self, manifest: StrategyManifest
    ):
        sandbox = StrategySandbox(_PassiveStrategy(), manifest)
        sandbox._activate_restrictions()
        assert _in_sandbox_execution.get() is True
        sandbox._deactivate_restrictions()
        assert _in_sandbox_execution.get() is False

    def test_cleanup_idempotent(self, manifest: StrategyManifest):
        sandbox = StrategySandbox(_PassiveStrategy(), manifest)
        sandbox.cleanup()
        sandbox.cleanup()
        assert _in_sandbox_execution.get() is False

    def test_cleanup_restores_all_builtins(self, manifest: StrategyManifest):
        original_open = builtins.open
        original_getattr = builtins.getattr
        original_object = builtins.object

        sandbox = StrategySandbox(_PassiveStrategy(), manifest)
        sandbox._activate_restrictions()
        assert builtins.open is not original_open
        assert builtins.getattr is not original_getattr
        assert builtins.object is not original_object

        sandbox.cleanup()
        assert builtins.open is original_open
        assert builtins.getattr is original_getattr
        assert builtins.object is original_object
        assert _in_sandbox_execution.get() is False


# ── 9. httpx monkey-patch does not leak ───────────────────────────────


class TestHttpxMonkeyPatchNoLeak:
    async def test_send_restored_after_eval(
        self, networked_manifest: StrategyManifest
    ):
        original_send = httpx.AsyncClient.send
        sandbox = StrategySandbox(
            _PassiveStrategy(), networked_manifest
        )
        try:
            await sandbox.safe_evaluate(None, None, None)
            assert httpx.AsyncClient.send is original_send
        finally:
            sandbox.cleanup()

    async def test_send_restored_after_error(
        self, networked_manifest: StrategyManifest
    ):
        original_send = httpx.AsyncClient.send
        sandbox = StrategySandbox(
            _CrashingStrategy(), networked_manifest
        )
        try:
            await sandbox.safe_evaluate(None, None, None)
            assert httpx.AsyncClient.send is original_send
        finally:
            sandbox.cleanup()

    async def test_send_restored_after_timeout(
        self, networked_manifest: StrategyManifest
    ):
        original_send = httpx.AsyncClient.send
        sandbox = StrategySandbox(
            _SlowStrategy(), networked_manifest
        )
        try:
            await sandbox.safe_evaluate(None, None, None)
            assert httpx.AsyncClient.send is original_send
        finally:
            sandbox.cleanup()

    async def test_restricted_send_checks_context_var(
        self, networked_manifest: StrategyManifest
    ):
        sandbox = StrategySandbox(
            _PassiveStrategy(), networked_manifest
        )
        sandbox._original_httpx_send = httpx.AsyncClient.send
        restricted_send = sandbox._make_restricted_send()

        transport = httpx.MockTransport(
            lambda _r: httpx.Response(200, json={"ok": True})
        )
        async with httpx.AsyncClient(transport=transport) as client:
            request = httpx.Request("GET", "https://evil.com/api")

            assert _in_sandbox_execution.get() is False
            response = await restricted_send(
                client, request, stream=False
            )
            assert response.status_code == 200

    async def test_restricted_send_blocks_when_context_true(
        self, networked_manifest: StrategyManifest
    ):
        sandbox = StrategySandbox(
            _PassiveStrategy(), networked_manifest
        )
        sandbox._original_httpx_send = httpx.AsyncClient.send
        restricted_send = sandbox._make_restricted_send()

        transport = httpx.MockTransport(
            lambda _r: httpx.Response(200)
        )
        async with httpx.AsyncClient(transport=transport) as client:
            request = httpx.Request("GET", "https://evil.com/api")

            _in_sandbox_execution.set(True)
            try:
                with pytest.raises(PermissionError, match="not allowed"):
                    await restricted_send(client, request, stream=False)
            finally:
                _in_sandbox_execution.set(False)


# ── 10. Integration: verify test infra httpx is not blocked ───────────


class TestIntegrationTestInfraNotBlocked:
    async def test_httpx_asgi_transport_works_after_sandbox(
        self, networked_manifest: StrategyManifest
    ):
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient

        app = FastAPI()

        @app.get("/test")
        async def test_route():
            return {"status": "ok"}

        sandbox = StrategySandbox(
            _PassiveStrategy(), networked_manifest
        )
        try:
            await sandbox.safe_evaluate(None, None, None)
        finally:
            sandbox.cleanup()

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.get("/test")
            assert resp.status_code == 200
            assert resp.json()["status"] == "ok"

    async def test_multiple_sandbox_lifecycle_cycles(
        self, networked_manifest: StrategyManifest
    ):
        for _ in range(3):
            sandbox = StrategySandbox(
                _PassiveStrategy(), networked_manifest
            )
            try:
                await sandbox.safe_evaluate(None, None, None)
                assert _in_sandbox_execution.get() is False

                transport = httpx.MockTransport(
                    lambda _r: httpx.Response(200)
                )
                async with httpx.AsyncClient(
                    transport=transport
                ) as client:
                    resp = await client.get("https://evil.com/api")
                    assert resp.status_code == 200
            finally:
                sandbox.cleanup()

    async def test_sandbox_from_factory_resets_context_var(
        self, manifest: StrategyManifest
    ):
        sandbox = StrategySandbox.from_factory(
            _PassiveStrategy, manifest
        )
        try:
            assert _in_sandbox_execution.get() is False
            await sandbox.safe_evaluate(None, None, None)
            assert _in_sandbox_execution.get() is False
        finally:
            sandbox.cleanup()


# ── Edge cases ─────────────────────────────────────────────────────────


class TestContextVarEdgeCases:
    def test_set_and_reset_without_sandbox(self):
        assert _in_sandbox_execution.get() is False
        _in_sandbox_execution.set(True)
        assert _in_sandbox_execution.get() is True
        _in_sandbox_execution.set(False)
        assert _in_sandbox_execution.get() is False

    async def test_context_var_not_affected_by_unrelated_exception(self):
        assert _in_sandbox_execution.get() is False

        def _raise_unrelated() -> None:
            raise ValueError("unrelated")

        with contextlib.suppress(ValueError):
            _raise_unrelated()
        assert _in_sandbox_execution.get() is False

    def test_activate_deactivate_cycle(self, manifest: StrategyManifest):
        sandbox = StrategySandbox(_PassiveStrategy(), manifest)
        for _ in range(5):
            sandbox._activate_restrictions()
            assert _in_sandbox_execution.get() is True
            sandbox._deactivate_restrictions()
            assert _in_sandbox_execution.get() is False
        sandbox.cleanup()

    async def test_network_manifest_without_endpoints(
        self, manifest: StrategyManifest
    ):
        sandbox = StrategySandbox(_PassiveStrategy(), manifest)
        try:
            assert sandbox._http_client is None
            await sandbox.safe_evaluate(None, None, None)
            assert _in_sandbox_execution.get() is False
        finally:
            sandbox.cleanup()

    async def test_restricted_send_with_stream_flag(
        self, networked_manifest: StrategyManifest
    ):
        sandbox = StrategySandbox(
            _PassiveStrategy(), networked_manifest
        )
        sandbox._original_httpx_send = httpx.AsyncClient.send
        restricted_send = sandbox._make_restricted_send()

        transport = httpx.MockTransport(
            lambda _r: httpx.Response(200, json={"data": "test"})
        )
        async with httpx.AsyncClient(transport=transport) as client:
            request = httpx.Request("GET", "https://evil.com/api")

            response = await restricted_send(
                client, request, stream=True
            )
            assert response.status_code == 200


# ── 11. File open restriction gated by contextvar ────────────────────


class _FileReadStrategy:
    """Strategy that attempts to read a file outside the sandbox."""

    name = "file_read"
    version = "1.0.0"

    def __init__(self, target_path: str) -> None:
        self._target = target_path

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        with open(self._target) as f:
            f.read()
        return []


class TestFileOpenContextVarGate:
    """
    Verify _restricted_open is gated by _in_sandbox_execution.

    When the contextvar is False (outside sandbox evaluation), the
    monkey-patched builtins.open / io.open must pass through to the
    original implementation unconditionally.  This prevents the sandbox
    file restriction from breaking coverage HTML generation, pytest
    internals, and other tooling that reads files after tests complete.
    """

    def test_restricted_open_passes_through_when_contextvar_false(
        self, manifest: StrategyManifest, tmp_path: Any
    ):
        secret = tmp_path / "secret.txt"
        secret.write_text("data")

        sandbox = StrategySandbox(_PassiveStrategy(), manifest)
        sandbox._original_open = builtins.open
        try:
            assert _in_sandbox_execution.get() is False
            result = sandbox._restricted_open(str(secret))
            assert result.read() == "data"
        finally:
            sandbox.cleanup()

    def test_restricted_open_passes_through_for_package_files(
        self, manifest: StrategyManifest
    ):
        import importlib

        sandbox = StrategySandbox(_PassiveStrategy(), manifest)
        sandbox._original_open = builtins.open
        try:
            coverage_pkg = importlib.import_module("coverage")
            htmlfiles_dir = pathlib.Path(coverage_pkg.__file__).parent / "htmlfiles"
            index_html = htmlfiles_dir / "index.html"
            assert index_html.exists(), "coverage htmlfiles/index.html should exist"

            assert _in_sandbox_execution.get() is False
            with sandbox._restricted_open(str(index_html)) as f:
                content = f.read()
            assert len(content) > 0
        finally:
            sandbox.cleanup()

    def test_restricted_open_allows_write_when_contextvar_false(
        self, manifest: StrategyManifest, tmp_path: Any
    ):
        target = tmp_path / "output.txt"

        sandbox = StrategySandbox(_PassiveStrategy(), manifest)
        sandbox._original_open = builtins.open
        try:
            assert _in_sandbox_execution.get() is False
            with sandbox._restricted_open(str(target), "w") as f:
                f.write("written outside sandbox")
            assert target.read_text() == "written outside sandbox"
        finally:
            sandbox.cleanup()

    def test_restricted_open_blocks_when_contextvar_true(
        self, manifest: StrategyManifest, tmp_path: Any
    ):
        secret = tmp_path / "secret.txt"
        secret.write_text("sensitive")

        sandbox = StrategySandbox(_PassiveStrategy(), manifest)
        sandbox._original_open = builtins.open
        _in_sandbox_execution.set(True)
        try:
            with pytest.raises(PermissionError, match="not allowed"):
                sandbox._restricted_open(str(secret))
        finally:
            _in_sandbox_execution.set(False)
            sandbox.cleanup()

    def test_restricted_open_blocks_write_when_contextvar_true(
        self, manifest: StrategyManifest
    ):
        sandbox = StrategySandbox(_PassiveStrategy(), manifest)
        sandbox._original_open = builtins.open
        _in_sandbox_execution.set(True)
        try:
            target = str(pathlib.Path(sandbox._work_dir) / "writable.txt")
            with pytest.raises(PermissionError, match="Write access"):
                sandbox._restricted_open(target, "w")
        finally:
            _in_sandbox_execution.set(False)
            sandbox.cleanup()

    async def test_open_works_after_sandbox_evaluation(
        self, manifest: StrategyManifest, tmp_path: Any
    ):
        secret = tmp_path / "secret.txt"
        secret.write_text("post-eval data")

        sandbox = StrategySandbox(_PassiveStrategy(), manifest)
        try:
            await sandbox.safe_evaluate(None, None, None)
            assert _in_sandbox_execution.get() is False
            with open(str(secret)) as f:
                assert f.read() == "post-eval data"
        finally:
            sandbox.cleanup()

    async def test_open_blocked_during_evaluation(
        self, manifest: StrategyManifest, tmp_path: Any
    ):
        secret = tmp_path / "secret.txt"
        secret.write_text("sensitive")

        sandbox = StrategySandbox(
            _FileReadStrategy(str(secret)), manifest
        )
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors == 1
            assert "not allowed" in (
                sandbox.metrics.last_error or ""
            ).lower()
        finally:
            sandbox.cleanup()

    async def test_open_works_outside_sandbox_with_monkeypatch_installed(
        self, manifest: StrategyManifest, tmp_path: Any
    ):
        """
        Even when builtins.open is monkey-patched but contextvar is False,
        reads should pass through.  This is the exact scenario that caused
        the coverage INTERNALERROR.
        """
        secret = tmp_path / "tooling_file.txt"
        secret.write_text("tooling data")

        original_open = builtins.open
        sandbox = StrategySandbox(_PassiveStrategy(), manifest)
        sandbox._activate_restrictions()
        try:
            assert builtins.open is not original_open
            assert _in_sandbox_execution.get() is True
            _in_sandbox_execution.set(False)
            with open(str(secret)) as f:
                assert f.read() == "tooling data"
        finally:
            _in_sandbox_execution.set(False)
            sandbox._deactivate_restrictions()
            sandbox.cleanup()
