"""Comprehensive tests for PaperExecutionBackend, factory, and registry."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from unittest.mock import MagicMock

import pytest

from engine.core.execution.base import ExecutionBackend, FillResult
from engine.core.execution.factory import (
    _reset_for_tests,
    create_backend,
    list_backends,
    register_backend,
)
from engine.core.execution.paper import (
    PaperExecutionBackend,
    PaperFillStats,
    SlippageModel,
)
from engine.observability.metrics import RecordingBackend


class _FakeSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


@dataclass
class _FakeOrder:
    id: str = "ord-1"
    symbol: str = "AAPL"
    quantity: int = 100
    side: _FakeSide = _FakeSide.BUY


def _make_cost(slippage_amount: float = 5.0):
    mock_cost = MagicMock()
    mock_cost.slippage = MagicMock()
    mock_cost.slippage.amount = slippage_amount
    return mock_cost


class _RecordingPriceProvider:
    def __init__(self, prices: dict[str, float] | None = None):
        self._prices = prices or {"AAPL": 150.0, "MSFT": 300.0, "GOOG": 2800.0}
        self.calls: list[str] = []

    def __call__(self, symbol: str) -> float | None:
        self.calls.append(symbol)
        return self._prices.get(symbol)


# ---------------------------------------------------------------------------
# PaperExecutionBackend — Construction & Validation
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_default_params(self):
        backend = PaperExecutionBackend()
        assert backend.fill_probability == 0.95
        assert backend.slippage_model == SlippageModel.RANDOM
        assert backend.slippage_bps == 5.0
        assert backend.partial_fill_enabled is True
        assert backend._connected is False

    def test_custom_params(self):
        backend = PaperExecutionBackend(
            fill_probability=0.8,
            slippage_model=SlippageModel.FIXED,
            slippage_bps=10.0,
            slippage_fixed_amount=0.05,
            slippage_jitter_range=0.5,
            partial_fill_enabled=False,
            partial_fill_min_ratio=0.9,
            partial_fill_volume_threshold=1000,
            latency_ms_mean=100.0,
            latency_ms_std=30.0,
            random_seed=42,
        )
        assert backend.fill_probability == 0.8
        assert backend.slippage_model == SlippageModel.FIXED
        assert backend.slippage_bps == 10.0
        assert backend.slippage_fixed_amount == 0.05
        assert backend.partial_fill_enabled is False
        assert backend.partial_fill_min_ratio == 0.9

    def test_invalid_fill_probability_zero(self):
        with pytest.raises(ValueError, match="fill_probability"):
            PaperExecutionBackend(fill_probability=-0.1)

    def test_invalid_fill_probability_above_one(self):
        with pytest.raises(ValueError, match="fill_probability"):
            PaperExecutionBackend(fill_probability=1.5)

    def test_negative_slippage_bps_rejected(self):
        with pytest.raises(ValueError, match="slippage_bps"):
            PaperExecutionBackend(slippage_bps=-1.0)

    def test_invalid_partial_fill_ratio(self):
        with pytest.raises(ValueError, match="partial_fill_min_ratio"):
            PaperExecutionBackend(partial_fill_min_ratio=1.5)

    def test_boundary_fill_probability_zero(self):
        backend = PaperExecutionBackend(fill_probability=0.0)
        assert backend.fill_probability == 0.0

    def test_boundary_fill_probability_one(self):
        backend = PaperExecutionBackend(fill_probability=1.0)
        assert backend.fill_probability == 1.0

    def test_accepts_price_provider(self):
        provider = _RecordingPriceProvider()
        backend = PaperExecutionBackend(price_provider=provider)
        assert backend._price_provider is provider

    def test_accepts_metrics_backend(self):
        metrics = RecordingBackend()
        backend = PaperExecutionBackend(metrics=metrics)
        assert backend.metrics is metrics


# ---------------------------------------------------------------------------
# PaperExecutionBackend — Connect / Disconnect
# ---------------------------------------------------------------------------


class TestConnectDisconnect:
    async def test_connect_sets_connected(self):
        backend = PaperExecutionBackend()
        await backend.connect()
        assert backend._connected is True

    async def test_disconnect_clears_connected(self):
        backend = PaperExecutionBackend()
        await backend.connect()
        await backend.disconnect()
        assert backend._connected is False

    async def test_connect_emits_metric(self):
        metrics = RecordingBackend()
        backend = PaperExecutionBackend(metrics=metrics)
        await backend.connect()
        assert metrics.counters.get(("paper_backend.connect", ())) == 1.0

    async def test_disconnect_emits_metric(self):
        metrics = RecordingBackend()
        backend = PaperExecutionBackend(metrics=metrics)
        await backend.connect()
        await backend.disconnect()
        assert metrics.counters.get(("paper_backend.disconnect", ())) == 1.0

    async def test_disconnect_logs_session_duration(self):
        metrics = RecordingBackend()
        backend = PaperExecutionBackend(metrics=metrics)
        await backend.connect()
        await backend.disconnect()
        assert ("paper_backend.session_duration_seconds", ()) in metrics.histograms

    async def test_disconnect_without_connect_is_safe(self):
        backend = PaperExecutionBackend()
        await backend.disconnect()
        assert backend._connected is False


# ---------------------------------------------------------------------------
# PaperExecutionBackend — Execute — Not Connected
# ---------------------------------------------------------------------------


class TestExecuteNotConnected:
    async def test_execute_not_connected(self):
        backend = PaperExecutionBackend()
        result = await backend.execute(_FakeOrder(), 150.0, _make_cost())
        assert result.success is False
        assert "not connected" in result.reason.lower()

    async def test_not_connected_emits_metric(self):
        metrics = RecordingBackend()
        backend = PaperExecutionBackend(metrics=metrics)
        result = await backend.execute(_FakeOrder(), 150.0, _make_cost())
        assert result.success is False
        key = ("paper_backend.execute", (("outcome", "not_connected"),))
        assert metrics.counters.get(key) == 1.0


# ---------------------------------------------------------------------------
# PaperExecutionBackend — Execute — Fill Probability
# ---------------------------------------------------------------------------


class TestFillProbability:
    async def test_fill_probability_one_always_fills(self):
        backend = PaperExecutionBackend(fill_probability=1.0, random_seed=42)
        await backend.connect()
        for _ in range(20):
            result = await backend.execute(_FakeOrder(), 100.0, _make_cost())
            assert result.success is True

    async def test_fill_probability_zero_never_fills(self):
        backend = PaperExecutionBackend(fill_probability=0.0, random_seed=42)
        await backend.connect()
        for _ in range(20):
            result = await backend.execute(_FakeOrder(), 100.0, _make_cost())
            assert result.success is False
            assert "fill failure" in result.reason.lower()

    async def test_fill_failure_emits_metric(self):
        metrics = RecordingBackend()
        backend = PaperExecutionBackend(
            fill_probability=0.0, random_seed=42, metrics=metrics
        )
        await backend.connect()
        await backend.execute(_FakeOrder(), 100.0, _make_cost())
        key = ("paper_backend.execute", (("outcome", "fill_rejected"),))
        assert metrics.counters.get(key) == 1.0

    async def test_fill_probability_respects_seed(self):
        backend1 = PaperExecutionBackend(fill_probability=0.5, random_seed=42)
        backend2 = PaperExecutionBackend(fill_probability=0.5, random_seed=42)
        await backend1.connect()
        await backend2.connect()
        results1 = []
        results2 = []
        for _ in range(50):
            r1 = await backend1.execute(_FakeOrder(), 100.0, _make_cost())
            r2 = await backend2.execute(_FakeOrder(), 100.0, _make_cost())
            results1.append(r1.success)
            results2.append(r2.success)
        assert results1 == results2


# ---------------------------------------------------------------------------
# PaperExecutionBackend — Execute — Slippage Models
# ---------------------------------------------------------------------------


class TestSlippageFixed:
    async def test_fixed_slippage_buy(self):
        backend = PaperExecutionBackend(
            fill_probability=1.0,
            slippage_model=SlippageModel.FIXED,
            slippage_fixed_amount=0.10,
            random_seed=42,
        )
        await backend.connect()
        order = _FakeOrder(side=_FakeSide.BUY, quantity=100)
        result = await backend.execute(order, 100.0, _make_cost())
        assert result.success is True
        assert result.price == 100.1

    async def test_fixed_slippage_sell(self):
        backend = PaperExecutionBackend(
            fill_probability=1.0,
            slippage_model=SlippageModel.FIXED,
            slippage_fixed_amount=0.10,
            random_seed=42,
        )
        await backend.connect()
        order = _FakeOrder(side=_FakeSide.SELL, quantity=100)
        result = await backend.execute(order, 100.0, _make_cost())
        assert result.success is True
        assert result.price == 99.9


class TestSlippagePercentage:
    async def test_percentage_slippage_buy(self):
        backend = PaperExecutionBackend(
            fill_probability=1.0,
            slippage_model=SlippageModel.PERCENTAGE,
            slippage_bps=10.0,
            random_seed=42,
        )
        await backend.connect()
        order = _FakeOrder(side=_FakeSide.BUY, quantity=100)
        result = await backend.execute(order, 100.0, _make_cost())
        assert result.success is True
        assert result.price == pytest.approx(100.1, abs=0.01)

    async def test_percentage_slippage_sell(self):
        backend = PaperExecutionBackend(
            fill_probability=1.0,
            slippage_model=SlippageModel.PERCENTAGE,
            slippage_bps=10.0,
            random_seed=42,
        )
        await backend.connect()
        order = _FakeOrder(side=_FakeSide.SELL, quantity=100)
        result = await backend.execute(order, 100.0, _make_cost())
        assert result.success is True
        assert result.price == pytest.approx(99.9, abs=0.01)


class TestSlippageRandom:
    async def test_random_slippage_incorporates_cost_model(self):
        backend = PaperExecutionBackend(
            fill_probability=1.0,
            slippage_model=SlippageModel.RANDOM,
            slippage_bps=5.0,
            random_seed=42,
        )
        await backend.connect()
        order = _FakeOrder(side=_FakeSide.BUY, quantity=100)
        result = await backend.execute(order, 100.0, _make_cost(10.0))
        assert result.success is True
        assert result.price > 100.0

    async def test_random_slippage_deterministic_with_seed(self):
        backend1 = PaperExecutionBackend(
            fill_probability=1.0, slippage_model=SlippageModel.RANDOM, random_seed=42
        )
        backend2 = PaperExecutionBackend(
            fill_probability=1.0, slippage_model=SlippageModel.RANDOM, random_seed=42
        )
        await backend1.connect()
        await backend2.connect()
        order = _FakeOrder(side=_FakeSide.BUY, quantity=100)
        r1 = await backend1.execute(order, 100.0, _make_cost(10.0))
        r2 = await backend2.execute(order, 100.0, _make_cost(10.0))
        assert r1.price == r2.price


# ---------------------------------------------------------------------------
# PaperExecutionBackend — Execute — Price Resolution
# ---------------------------------------------------------------------------


class TestPriceResolution:
    async def test_uses_price_provider_when_available(self):
        provider = _RecordingPriceProvider({"AAPL": 155.0})
        backend = PaperExecutionBackend(
            fill_probability=1.0,
            slippage_model=SlippageModel.FIXED,
            slippage_fixed_amount=0.0,
            random_seed=42,
            price_provider=provider,
        )
        await backend.connect()
        order = _FakeOrder(symbol="AAPL")
        result = await backend.execute(order, 150.0, _make_cost())
        assert result.success is True
        assert result.price == 155.0
        assert "AAPL" in provider.calls

    async def test_falls_back_to_market_price(self):
        provider = _RecordingPriceProvider({"MSFT": 300.0})
        backend = PaperExecutionBackend(
            fill_probability=1.0,
            slippage_model=SlippageModel.FIXED,
            slippage_fixed_amount=0.0,
            random_seed=42,
            price_provider=provider,
        )
        await backend.connect()
        order = _FakeOrder(symbol="AAPL")
        result = await backend.execute(order, 150.0, _make_cost())
        assert result.success is True
        assert result.price == 150.0

    async def test_provider_returns_none_uses_market_price(self):
        provider = _RecordingPriceProvider({})
        backend = PaperExecutionBackend(
            fill_probability=1.0,
            slippage_model=SlippageModel.FIXED,
            slippage_fixed_amount=0.0,
            random_seed=42,
            price_provider=provider,
        )
        await backend.connect()
        order = _FakeOrder(symbol="AAPL")
        result = await backend.execute(order, 150.0, _make_cost())
        assert result.success is True
        assert result.price == 150.0

    async def test_zero_market_price_no_provider_fails(self):
        backend = PaperExecutionBackend(
            fill_probability=1.0,
            random_seed=42,
        )
        await backend.connect()
        order = _FakeOrder(symbol="AAPL")
        result = await backend.execute(order, 0.0, _make_cost())
        assert result.success is False
        assert "no valid price" in result.reason.lower()

    async def test_set_price_provider_after_construction(self):
        backend = PaperExecutionBackend(
            fill_probability=1.0,
            slippage_model=SlippageModel.FIXED,
            slippage_fixed_amount=0.0,
            random_seed=42,
        )
        await backend.connect()
        result1 = await backend.execute(_FakeOrder(symbol="AAPL"), 150.0, _make_cost())
        assert result1.success is True
        assert result1.price == 150.0

        backend.set_price_provider(_RecordingPriceProvider({"AAPL": 160.0}))
        result2 = await backend.execute(_FakeOrder(symbol="AAPL"), 150.0, _make_cost())
        assert result2.success is True
        assert result2.price == 160.0


# ---------------------------------------------------------------------------
# PaperExecutionBackend — Execute — Partial Fills
# ---------------------------------------------------------------------------


class TestPartialFills:
    async def test_small_order_no_partial_fill(self):
        backend = PaperExecutionBackend(
            fill_probability=1.0,
            partial_fill_enabled=True,
            partial_fill_volume_threshold=500,
            random_seed=42,
        )
        await backend.connect()
        order = _FakeOrder(quantity=100)
        result = await backend.execute(order, 100.0, _make_cost())
        assert result.success is True
        assert result.quantity == 100

    async def test_large_order_can_partially_fill(self):
        backend = PaperExecutionBackend(
            fill_probability=1.0,
            partial_fill_enabled=True,
            partial_fill_min_ratio=0.5,
            partial_fill_volume_threshold=500,
            random_seed=42,
            slippage_model=SlippageModel.FIXED,
            slippage_fixed_amount=0.0,
        )
        await backend.connect()
        order = _FakeOrder(quantity=1000)
        fills = []
        for _ in range(100):
            r = await backend.execute(order, 100.0, _make_cost())
            fills.append(r.quantity)
        assert min(fills) >= 500
        assert max(fills) <= 1000

    async def test_partial_fill_disabled(self):
        backend = PaperExecutionBackend(
            fill_probability=1.0,
            partial_fill_enabled=False,
            random_seed=42,
            slippage_model=SlippageModel.FIXED,
            slippage_fixed_amount=0.0,
        )
        await backend.connect()
        order = _FakeOrder(quantity=10000)
        result = await backend.execute(order, 100.0, _make_cost())
        assert result.success is True
        assert result.quantity == 10000

    async def test_partial_fill_minimum_one_share(self):
        backend = PaperExecutionBackend(
            fill_probability=1.0,
            partial_fill_enabled=True,
            partial_fill_min_ratio=0.001,
            partial_fill_volume_threshold=1,
            random_seed=42,
            slippage_model=SlippageModel.FIXED,
            slippage_fixed_amount=0.0,
        )
        await backend.connect()
        order = _FakeOrder(quantity=501)
        results = []
        for _ in range(100):
            r = await backend.execute(order, 100.0, _make_cost())
            results.append(r.quantity)
        assert all(q >= 1 for q in results)


# ---------------------------------------------------------------------------
# PaperExecutionBackend — Fill Statistics
# ---------------------------------------------------------------------------


class TestFillStats:
    async def test_stats_track_successful_fills(self):
        backend = PaperExecutionBackend(fill_probability=1.0, random_seed=42)
        await backend.connect()
        await backend.execute(_FakeOrder(quantity=100), 100.0, _make_cost())
        assert backend.stats.total_fills == 1
        assert backend.stats.successful_fills == 1
        assert backend.stats.failed_fills == 0

    async def test_stats_track_failed_fills(self):
        backend = PaperExecutionBackend(fill_probability=0.0, random_seed=42)
        await backend.connect()
        await backend.execute(_FakeOrder(), 100.0, _make_cost())
        assert backend.stats.total_fills == 1
        assert backend.stats.successful_fills == 0
        assert backend.stats.failed_fills == 1

    async def test_stats_track_partial_fills(self):
        backend = PaperExecutionBackend(
            fill_probability=1.0,
            partial_fill_enabled=True,
            partial_fill_min_ratio=0.5,
            partial_fill_volume_threshold=100,
            random_seed=42,
            slippage_model=SlippageModel.FIXED,
            slippage_fixed_amount=0.0,
        )
        await backend.connect()
        order = _FakeOrder(quantity=1000)
        for _ in range(100):
            await backend.execute(order, 100.0, _make_cost())
        assert backend.stats.partial_fills > 0

    async def test_stats_fill_rate(self):
        backend = PaperExecutionBackend(fill_probability=0.5, random_seed=42)
        await backend.connect()
        for _ in range(100):
            await backend.execute(_FakeOrder(), 100.0, _make_cost())
        assert 0.3 < backend.stats.fill_rate < 0.7

    async def test_stats_avg_slippage(self):
        backend = PaperExecutionBackend(
            fill_probability=1.0,
            slippage_model=SlippageModel.PERCENTAGE,
            slippage_bps=10.0,
            random_seed=42,
        )
        await backend.connect()
        await backend.execute(_FakeOrder(), 100.0, _make_cost())
        assert backend.stats.avg_slippage_bps > 0

    async def test_stats_total_fill_value(self):
        backend = PaperExecutionBackend(
            fill_probability=1.0,
            slippage_model=SlippageModel.FIXED,
            slippage_fixed_amount=0.0,
            random_seed=42,
        )
        await backend.connect()
        await backend.execute(_FakeOrder(quantity=100), 150.0, _make_cost())
        assert backend.stats.total_fill_value == 15000.0
        assert backend.stats.total_fill_quantity == 100

    async def test_stats_as_dict(self):
        backend = PaperExecutionBackend(fill_probability=1.0, random_seed=42)
        await backend.connect()
        await backend.execute(_FakeOrder(quantity=100), 100.0, _make_cost())
        d = backend.stats.as_dict()
        assert "total_fills" in d
        assert "successful_fills" in d
        assert "failed_fills" in d
        assert "partial_fills" in d
        assert "avg_slippage_bps" in d
        assert "fill_rate" in d
        assert "total_fill_quantity" in d
        assert "total_fill_value" in d

    async def test_stats_empty_fill_rate_zero(self):
        stats = PaperFillStats()
        assert stats.fill_rate == 0.0
        assert stats.avg_slippage_bps == 0.0

    async def test_stats_track_not_connected_as_failed(self):
        backend = PaperExecutionBackend()
        result = await backend.execute(_FakeOrder(), 100.0, _make_cost())
        assert result.success is False
        assert backend.stats.total_fills == 1
        assert backend.stats.failed_fills == 1


# ---------------------------------------------------------------------------
# PaperExecutionBackend — Metrics Integration
# ---------------------------------------------------------------------------


class TestMetricsIntegration:
    async def test_filled_emits_slippage_histogram(self):
        metrics = RecordingBackend()
        backend = PaperExecutionBackend(
            fill_probability=1.0, random_seed=42, metrics=metrics
        )
        await backend.connect()
        await backend.execute(
            _FakeOrder(side=_FakeSide.BUY, quantity=100), 100.0, _make_cost()
        )
        key = ("paper_backend.slippage_bps", (("side", "buy"),))
        assert key in metrics.histograms
        assert len(metrics.histograms[key]) == 1

    async def test_sell_side_metrics_tagged(self):
        metrics = RecordingBackend()
        backend = PaperExecutionBackend(
            fill_probability=1.0, random_seed=42, metrics=metrics
        )
        await backend.connect()
        await backend.execute(
            _FakeOrder(side=_FakeSide.SELL, quantity=100), 100.0, _make_cost()
        )
        key = ("paper_backend.slippage_bps", (("side", "sell"),))
        assert key in metrics.histograms

    async def test_execute_filled_counter(self):
        metrics = RecordingBackend()
        backend = PaperExecutionBackend(
            fill_probability=1.0, random_seed=42, metrics=metrics
        )
        await backend.connect()
        await backend.execute(_FakeOrder(), 100.0, _make_cost())
        key = ("paper_backend.execute", (("outcome", "filled"),))
        assert metrics.counters.get(key) == 1.0


# ---------------------------------------------------------------------------
# PaperExecutionBackend — SlippageModel enum
# ---------------------------------------------------------------------------


class TestSlippageModelEnum:
    def test_values(self):
        assert SlippageModel.FIXED == "fixed"
        assert SlippageModel.PERCENTAGE == "percentage"
        assert SlippageModel.RANDOM == "random"

    def test_from_string(self):
        assert SlippageModel("fixed") == SlippageModel.FIXED
        assert SlippageModel("percentage") == SlippageModel.PERCENTAGE
        assert SlippageModel("random") == SlippageModel.RANDOM

    def test_invalid_string_raises(self):
        with pytest.raises(ValueError):
            SlippageModel("invalid")


# ---------------------------------------------------------------------------
# PaperExecutionBackend — Price Rounding
# ---------------------------------------------------------------------------


class TestPriceRounding:
    async def test_fill_price_rounded_to_4_decimals(self):
        backend = PaperExecutionBackend(
            fill_probability=1.0,
            slippage_model=SlippageModel.PERCENTAGE,
            slippage_bps=3.33,
            random_seed=42,
        )
        await backend.connect()
        order = _FakeOrder(side=_FakeSide.BUY, quantity=100)
        result = await backend.execute(order, 99.99, _make_cost())
        assert result.success is True
        s = str(result.price)
        if "." in s:
            assert len(s.split(".")[1]) <= 4


# ---------------------------------------------------------------------------
# PaperExecutionBackend — Backward Compatibility (PaperBackend alias)
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    async def test_paper_backend_alias_works(self):
        from engine.core.execution.paper import PaperBackend

        backend = PaperBackend()
        assert isinstance(backend, PaperExecutionBackend)
        assert isinstance(backend, ExecutionBackend)
        await backend.connect()
        assert backend._connected is True

    async def test_paper_backend_default_fill_probability(self):
        from engine.core.execution.paper import PaperBackend

        backend = PaperBackend()
        assert backend.fill_probability == 1.0

    async def test_existing_tests_still_pass_pattern(self):
        from engine.core.execution.paper import PaperBackend

        backend = PaperBackend()
        await backend.connect()
        order = _FakeOrder(side=_FakeSide.BUY, quantity=100)
        result = await backend.execute(order, 100.0, _make_cost(10.0))
        assert result.success is True
        assert result.quantity == 100
        assert result.price > 0


# ---------------------------------------------------------------------------
# Factory — create_backend
# ---------------------------------------------------------------------------


class TestFactory:
    def setup_method(self):
        _reset_for_tests()

    def test_create_paper_backend(self):
        backend = create_backend("paper")
        assert isinstance(backend, PaperExecutionBackend)

    def test_create_paper_backend_case_insensitive(self):
        backend = create_backend("PAPER")
        assert isinstance(backend, PaperExecutionBackend)

    def test_create_backtest_backend(self):
        from engine.core.execution.backtest import BacktestBackend

        backend = create_backend("backtest")
        assert isinstance(backend, BacktestBackend)

    def test_create_live_backend(self):
        from engine.core.execution.live import LiveBackend

        backend = create_backend("live")
        assert isinstance(backend, LiveBackend)

    def test_create_paper_with_custom_params(self):
        backend = create_backend(
            "paper",
            fill_probability=0.8,
            slippage_model="fixed",
            slippage_bps=10.0,
            random_seed=42,
        )
        assert isinstance(backend, PaperExecutionBackend)
        assert backend.fill_probability == 0.8
        assert backend.slippage_model == SlippageModel.FIXED

    def test_create_paper_with_price_provider(self):
        provider = _RecordingPriceProvider()
        backend = create_backend("paper", price_provider=provider)
        assert isinstance(backend, PaperExecutionBackend)
        assert backend._price_provider is provider

    def test_create_paper_with_metrics(self):
        metrics = RecordingBackend()
        backend = create_backend("paper", metrics=metrics)
        assert isinstance(backend, PaperExecutionBackend)
        assert backend.metrics is metrics

    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError, match="Unknown execution backend"):
            create_backend("nonexistent")

    def test_empty_name_raises(self):
        with pytest.raises(ValueError):
            create_backend("")

    def test_create_backtest_with_seed(self):
        from engine.core.execution.backtest import BacktestBackend

        backend = create_backend("backtest", random_seed=42)
        assert isinstance(backend, BacktestBackend)

    def test_create_live_with_params(self):
        from engine.core.execution.live import LiveBackend

        backend = create_backend(
            "live",
            broker_name="ibkr",
            api_key="key",
            api_secret="secret",
        )
        assert isinstance(backend, LiveBackend)
        assert backend.broker_name == "ibkr"

    def test_extra_kwargs_ignored_gracefully(self):
        backend = create_backend("paper", random_seed=42, unknown_param="value")
        assert isinstance(backend, PaperExecutionBackend)


# ---------------------------------------------------------------------------
# Factory — register_backend
# ---------------------------------------------------------------------------


class TestRegistry:
    def setup_method(self):
        _reset_for_tests()

    def test_register_custom_backend(self):
        class CustomBackend(ExecutionBackend):
            async def execute(self, order, market_price, costs):
                return FillResult(success=True)

            async def connect(self):
                pass

            async def disconnect(self):
                pass

        register_backend("custom", CustomBackend)
        assert "custom" in list_backends()
        backend = create_backend("custom")
        assert isinstance(backend, CustomBackend)

    def test_register_overwrites_existing(self):
        class BackendV1(ExecutionBackend):
            async def execute(self, order, market_price, costs):
                return FillResult(success=True)

            async def connect(self):
                pass

            async def disconnect(self):
                pass

        class BackendV2(ExecutionBackend):
            async def execute(self, order, market_price, costs):
                return FillResult(success=True, price=42.0)

            async def connect(self):
                pass

            async def disconnect(self):
                pass

        register_backend("versioned", BackendV1)
        register_backend("versioned", BackendV2)
        backend = create_backend("versioned")
        assert isinstance(backend, BackendV2)

    def test_register_empty_name_rejected(self):
        class Dummy(ExecutionBackend):
            async def execute(self, order, market_price, costs):
                return FillResult()

            async def connect(self):
                pass

            async def disconnect(self):
                pass

        with pytest.raises(ValueError, match="non-empty"):
            register_backend("", Dummy)

    def test_register_non_backend_rejected(self):
        with pytest.raises(TypeError, match="ExecutionBackend"):
            register_backend("bad", type("NotABackend", (), {}))

    def test_register_name_is_lowercased(self):
        class MyBackend(ExecutionBackend):
            async def execute(self, order, market_price, costs):
                return FillResult(success=True)

            async def connect(self):
                pass

            async def disconnect(self):
                pass

        register_backend("MyBackend", MyBackend)
        assert "mybackend" in list_backends()

    def test_register_whitespace_trimmed(self):
        class TidyBackend(ExecutionBackend):
            async def execute(self, order, market_price, costs):
                return FillResult(success=True)

            async def connect(self):
                pass

            async def disconnect(self):
                pass

        register_backend("  tidy  ", TidyBackend)
        assert "tidy" in list_backends()


# ---------------------------------------------------------------------------
# Factory — list_backends
# ---------------------------------------------------------------------------


class TestListBackends:
    def setup_method(self):
        _reset_for_tests()

    def test_lists_builtins(self):
        backends = list_backends()
        assert "backtest" in backends
        assert "paper" in backends
        assert "live" in backends

    def test_includes_registered(self):
        class Extra(ExecutionBackend):
            async def execute(self, order, market_price, costs):
                return FillResult()

            async def connect(self):
                pass

            async def disconnect(self):
                pass

        register_backend("extra", Extra)
        backends = list_backends()
        assert "extra" in backends

    def test_returns_sorted(self):
        backends = list_backends()
        assert backends == sorted(backends)


# ---------------------------------------------------------------------------
# Integration — Full execution pipeline via factory
# ---------------------------------------------------------------------------


class TestIntegration:
    async def test_paper_backend_via_factory_full_pipeline(self):
        metrics = RecordingBackend()
        provider = _RecordingPriceProvider({"AAPL": 150.0})
        backend = create_backend(
            "paper",
            fill_probability=1.0,
            slippage_model=SlippageModel.FIXED,
            slippage_fixed_amount=0.05,
            random_seed=42,
            price_provider=provider,
            metrics=metrics,
        )
        await backend.connect()

        buy_order = _FakeOrder(side=_FakeSide.BUY, quantity=200, symbol="AAPL")
        result = await backend.execute(buy_order, 100.0, _make_cost())
        assert result.success is True
        assert result.price == 150.05
        assert result.quantity == 200

        sell_order = _FakeOrder(side=_FakeSide.SELL, quantity=200, symbol="AAPL")
        result = await backend.execute(sell_order, 155.0, _make_cost())
        assert result.success is True
        assert result.price == 149.95

        await backend.disconnect()
        assert backend.stats.total_fills == 2
        assert backend.stats.successful_fills == 2

    async def test_concurrent_executions_are_safe(self):
        import asyncio

        backend = PaperExecutionBackend(fill_probability=1.0, random_seed=42)
        await backend.connect()

        async def single_exec(i: int) -> FillResult:
            order = _FakeOrder(quantity=100)
            return await backend.execute(order, 100.0, _make_cost())

        results = await asyncio.gather(*[single_exec(i) for i in range(50)])
        assert all(r.success for r in results)
        assert backend.stats.total_fills == 50
        assert backend.stats.successful_fills == 50

    async def test_stress_fill_probability_distribution(self):
        backend = PaperExecutionBackend(fill_probability=0.5, random_seed=42)
        await backend.connect()
        success_count = 0
        for _ in range(1000):
            result = await backend.execute(_FakeOrder(), 100.0, _make_cost())
            if result.success:
                success_count += 1
        assert 400 < success_count < 600
