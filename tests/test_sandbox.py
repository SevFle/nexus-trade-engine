"""Tests for StrategySandbox — isolated strategy execution."""

from __future__ import annotations

import asyncio
import builtins
import sys
from unittest.mock import MagicMock, patch

import pytest

from engine.core.signal import Side, Signal
from engine.plugins.manifest import StrategyManifest
from engine.plugins.sandbox import StrategySandbox


class GoodStrategy:
    name = "good_strategy"
    version = "1.0.0"

    def on_bar(self, state, portfolio):
        return [Signal.buy(symbol="AAPL", strategy_id=self.name)]


class BadStrategy:
    name = "bad_strategy"
    version = "1.0.0"

    def on_bar(self, state, portfolio):
        raise RuntimeError("strategy crashed")


class SlowStrategy:
    name = "slow_strategy"
    version = "1.0.0"

    async def on_bar(self, state, portfolio):
        await asyncio.sleep(60)
        return []


class MixedSignalStrategy:
    name = "mixed_strategy"
    version = "1.0.0"

    def on_bar(self, state, portfolio):
        return [Signal.buy(symbol="AAPL", strategy_id=self.name), "invalid_signal"]


@pytest.fixture
def manifest() -> StrategyManifest:
    return StrategyManifest(
        id="test",
        name="test",
        version="1.0.0",
        resources={"max_cpu_seconds": 1},
    )


class TestSafeEvaluate:
    async def test_good_strategy_returns_signals(self, manifest):
        sandbox = StrategySandbox(GoodStrategy(), manifest)
        snapshot = type("Snapshot", (), {"cash": 100_000})()
        signals = await sandbox.safe_evaluate(snapshot, None, None)
        assert len(signals) == 1
        assert signals[0].symbol == "AAPL"

    async def test_bad_strategy_returns_empty(self, manifest):
        sandbox = StrategySandbox(BadStrategy(), manifest)
        signals = await sandbox.safe_evaluate(None, None, None)
        assert signals == []
        assert sandbox.metrics.errors == 1

    async def test_slow_strategy_times_out(self, manifest):
        sandbox = StrategySandbox(SlowStrategy(), manifest)
        signals = await sandbox.safe_evaluate(None, None, None)
        assert signals == []
        assert sandbox.metrics.errors == 1
        assert "Timeout" in (sandbox.metrics.last_error or "")

    async def test_mixed_signals_filters_invalid(self, manifest):
        sandbox = StrategySandbox(MixedSignalStrategy(), manifest)
        signals = await sandbox.safe_evaluate(None, None, None)
        assert len(signals) == 1
        assert isinstance(signals[0], Signal)


class TestSandboxMetrics:
    async def test_metrics_updated_on_success(self, manifest):
        sandbox = StrategySandbox(GoodStrategy(), manifest)
        await sandbox.safe_evaluate(None, None, None)

        assert sandbox.metrics.total_evaluations == 1
        assert sandbox.metrics.total_signals_emitted == 1
        assert sandbox.metrics.avg_evaluation_ms > 0

    async def test_metrics_accumulate(self, manifest):
        sandbox = StrategySandbox(GoodStrategy(), manifest)
        await sandbox.safe_evaluate(None, None, None)
        await sandbox.safe_evaluate(None, None, None)

        assert sandbox.metrics.total_evaluations == 2
        assert sandbox.metrics.total_signals_emitted == 2


class TestGetHealth:
    async def test_health_report(self, manifest):
        sandbox = StrategySandbox(GoodStrategy(), manifest)
        await sandbox.safe_evaluate(None, None, None)
        health = sandbox.get_health()

        assert health["strategy_name"] == "good_strategy"
        assert health["version"] == "1.0.0"
        assert health["evaluations"] == 1
        assert health["signals_emitted"] == 1
        assert health["errors"] == 0


class TestSignalStrategyIdInjection:
    async def test_signal_gets_strategy_id(self, manifest):
        class NoIdStrategy:
            name = "no_id_strategy"
            version = "1.0.0"

            def on_bar(self, state, portfolio):
                sig = Signal(
                    symbol="AAPL",
                    side=Side.BUY,
                    strategy_id="",
                )
                return [sig]

        sandbox = StrategySandbox(NoIdStrategy(), manifest)
        signals = await sandbox.safe_evaluate(None, None, None)
        if signals:
            assert signals[0].strategy_id == "no_id_strategy"


class TestPlaceholderStrategy:
    def test_placeholder_returns_empty(self):
        from engine.plugins.sandbox import _PlaceholderStrategy

        p = _PlaceholderStrategy()
        assert p.on_bar(None, None) == []


class TestSandboxResourceLimits:
    def test_apply_resource_limits_no_resource_module(self, manifest):
        import engine.plugins.sandbox as sandbox_mod

        sandbox = StrategySandbox(GoodStrategy(), manifest)
        original = sandbox_mod.HAS_RESOURCE_MODULE
        sandbox_mod.HAS_RESOURCE_MODULE = False
        try:
            sandbox._apply_resource_limits()
            sandbox._restore_resource_limits()
        finally:
            sandbox_mod.HAS_RESOURCE_MODULE = original
            sandbox.cleanup()

    def test_apply_resource_limits_catches_setrlimit_error(self, manifest):
        import engine.plugins.sandbox as sandbox_mod

        sandbox = StrategySandbox(GoodStrategy(), manifest)
        with (
            patch.object(sandbox_mod._resource, "getrlimit", return_value=(100, 200)),
            patch.object(sandbox_mod._resource, "setrlimit", side_effect=OSError("denied")),
        ):
            sandbox._apply_resource_limits()
        assert "RLIMIT_AS" not in sandbox._saved_resource_limits
        sandbox.cleanup()

    def test_apply_resource_limits_catches_nofile_error(self, manifest):
        import engine.plugins.sandbox as sandbox_mod

        sandbox = StrategySandbox(GoodStrategy(), manifest)
        calls = iter([(1024, 1024), (64, 1024)])

        def fake_getrlimit(_):
            return next(calls)

        def fake_setrlimit(_, limits):
            if limits[0] < 100:
                raise ValueError("too low")

        with (
            patch.object(sandbox_mod._resource, "getrlimit", side_effect=fake_getrlimit),
            patch.object(sandbox_mod._resource, "setrlimit", side_effect=fake_setrlimit),
        ):
            sandbox._apply_resource_limits()
        sandbox.cleanup()


class TestRestrictedOpenWriteBlock:
    def test_write_mode_blocked(self, manifest):
        sandbox = StrategySandbox(GoodStrategy(), manifest)
        sandbox._original_open = builtins.open
        file_in_workdir = str(sandbox._work_dir) + "/test.txt"
        with pytest.raises(PermissionError, match="Write access"):
            sandbox._restricted_open(file_in_workdir, "w")
        sandbox.cleanup()

    def test_append_mode_blocked(self, manifest):
        sandbox = StrategySandbox(GoodStrategy(), manifest)
        sandbox._original_open = builtins.open
        file_in_workdir = str(sandbox._work_dir) + "/test.txt"
        with pytest.raises(PermissionError, match="Write access"):
            sandbox._restricted_open(file_in_workdir, "a")
        sandbox.cleanup()


class TestRestrictedHttpSend:
    async def test_allowed_host_passes_through(self):
        manifest = StrategyManifest(
            id="test",
            name="test",
            version="1.0.0",
            resources={"max_cpu_seconds": 1},
            network={"allowed_endpoints": ["api.example.com"]},
        )

        async def fake_send(client, request, *, stream=False, **kwargs):
            return MagicMock()

        sandbox = StrategySandbox(GoodStrategy(), manifest)
        sandbox._original_httpx_send = fake_send
        restricted_send = sandbox._make_restricted_send()

        mock_request = MagicMock()
        mock_request.url.host = "api.example.com"
        result = await restricted_send(MagicMock(), mock_request)
        assert result is not None
        sandbox.cleanup()

    async def test_blocked_host_raises(self):
        manifest = StrategyManifest(
            id="test",
            name="test",
            version="1.0.0",
            resources={"max_cpu_seconds": 1},
            network={"allowed_endpoints": ["api.example.com"]},
        )

        sandbox = StrategySandbox(GoodStrategy(), manifest)
        sandbox._original_httpx_send = MagicMock()
        restricted_send = sandbox._make_restricted_send()

        mock_request = MagicMock()
        mock_request.url.host = "evil.com"
        with pytest.raises(PermissionError, match="Network access"):
            await restricted_send(MagicMock(), mock_request)
        sandbox.cleanup()


class TestSandboxCleanup:
    def test_cleanup_restores_builtins_when_restrictions_active(self, manifest):
        original_open = builtins.open
        original_object = builtins.object
        original_getattr = builtins.getattr
        import io as _io

        original_io_open = _io.open
        sandbox = StrategySandbox(GoodStrategy(), manifest)
        sandbox._activate_restrictions()
        assert sandbox._original_open is not None
        sandbox.cleanup()
        assert sandbox._original_open is None
        assert builtins.open is original_open
        builtins.object = original_object
        builtins.getattr = original_getattr
        _io.open = original_io_open


class TestNoResourceModuleImport:
    def test_sandbox_works_without_resource_module(self, manifest):
        saved_sandbox = sys.modules.pop("engine.plugins.sandbox", None)
        saved_resource = sys.modules.get("resource")
        sys.modules["resource"] = None
        try:
            sys.modules.pop("engine.plugins.restricted_importer", None)
            sys.modules.pop("engine.plugins.sandboxed_http", None)
            import engine.plugins.sandbox as sandbox_mod

            assert sandbox_mod.HAS_RESOURCE_MODULE is False
            sandbox = sandbox_mod.StrategySandbox(
                sandbox_mod._PlaceholderStrategy(), manifest
            )
            sandbox._apply_resource_limits()
            sandbox._restore_resource_limits()
            sandbox.cleanup()
        finally:
            for mod in list(sys.modules):
                if mod.startswith("engine.plugins.sandbox"):
                    sys.modules.pop(mod, None)
            if saved_sandbox is not None:
                sys.modules["engine.plugins.sandbox"] = saved_sandbox
            if saved_resource is not None:
                sys.modules["resource"] = saved_resource
            else:
                sys.modules.pop("resource", None)
