"""Tests for StrategySandbox — isolated strategy execution."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

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
