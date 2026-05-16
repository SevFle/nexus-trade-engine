"""Tests for OpenTelemetry span instrumentation in core engine order lifecycle.

Verifies that OrderManager, CostModel, and ExecutionBackend implementations
emit properly structured spans with correct attributes and parent-child
relationships.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from engine.core.cost_model import DefaultCostModel
from engine.core.execution.backtest import BacktestBackend
from engine.core.execution.base import FillResult
from engine.core.execution.paper import PaperBackend, PaperTradeConfig
from engine.core.order_manager import Order, OrderManager
from engine.core.portfolio import Portfolio
from engine.core.risk_engine import RiskEngine
from engine.core.signal import Side, Signal

if TYPE_CHECKING:
    from engine.core.cost_model import CostBreakdown
    from engine.core.order_manager import Order as OrderType


class FakeExecutionBackend:
    def __init__(self, success: bool = True, price: float = 100.0, quantity: int = 10):
        self._success = success
        self._price = price
        self._quantity = quantity

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def execute(self, order: OrderType, market_price: float, costs: CostBreakdown) -> FillResult:
        if self._success:
            return FillResult(success=True, price=self._price, quantity=self._quantity)
        return FillResult(success=False, reason="Simulated failure")


@pytest.fixture(scope="session")
def _otel_provider():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return provider, exporter


@pytest.fixture
def span_exporter(_otel_provider):
    _provider, exporter = _otel_provider
    exporter.clear()
    return exporter


@pytest.fixture
def portfolio():
    return Portfolio(initial_cash=100_000.0)


@pytest.fixture
def cost_model():
    return DefaultCostModel()


@pytest.fixture
def risk_engine():
    return RiskEngine()


class TestCostModelSpans:
    def test_estimate_total_creates_span(self, span_exporter):
        model = DefaultCostModel()
        model.estimate_total("AAPL", 100, 150.0, "buy", avg_volume=1_000_000)

        spans = span_exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].name == "cost_model.estimate_total"

    def test_estimate_total_sets_attributes(self, span_exporter):
        model = DefaultCostModel()
        model.estimate_total("MSFT", 50, 300.0, "sell", avg_volume=500_000)

        spans = span_exporter.get_finished_spans()
        assert len(spans) == 1
        attrs = spans[0].attributes
        assert attrs["cost.symbol"] == "MSFT"
        assert attrs["cost.quantity"] == 50
        assert attrs["cost.price"] == 300.0
        assert attrs["cost.side"] == "sell"
        assert attrs["cost.avg_volume"] == 500_000
        assert "cost.total" in attrs

    def test_estimate_total_total_attribute_reflects_breakdown(self, span_exporter):
        model = DefaultCostModel(commission_per_trade=5.0, spread_bps=10.0)
        breakdown = model.estimate_total("AAPL", 100, 150.0, "buy")

        spans = span_exporter.get_finished_spans()
        attrs = spans[0].attributes
        assert abs(attrs["cost.total"] - breakdown.total.amount) < 1e-10


class TestBacktestBackendSpans:
    async def test_execute_creates_span(self, span_exporter):
        backend = BacktestBackend(random_seed=42)
        await backend.connect()
        order = _make_order("AAPL", Side.BUY, 10)
        costs = DefaultCostModel().estimate_total("AAPL", 10, 150.0, "buy")

        await backend.execute(order, 150.0, costs)

        spans = span_exporter.get_finished_spans()
        assert any(s.name == "backtest.execute" for s in spans)

    async def test_execute_sets_order_attributes(self, span_exporter):
        backend = BacktestBackend(random_seed=42)
        await backend.connect()
        order = _make_order("GOOGL", Side.BUY, 25)
        costs = DefaultCostModel().estimate_total("GOOGL", 25, 100.0, "buy")

        await backend.execute(order, 100.0, costs)

        spans = span_exporter.get_finished_spans()
        span = _get_span(spans, "backtest.execute")
        assert span.attributes["order.symbol"] == "GOOGL"
        assert span.attributes["order.side"] == "buy"
        assert span.attributes["order.quantity"] == 25
        assert span.attributes["order.market_price"] == 100.0

    async def test_execute_successful_fill_attributes(self, span_exporter):
        backend = BacktestBackend(fill_probability=1.0, random_seed=42)
        await backend.connect()
        order = _make_order("AAPL", Side.BUY, 10)
        costs = DefaultCostModel().estimate_total("AAPL", 10, 150.0, "buy")

        await backend.execute(order, 150.0, costs)

        spans = span_exporter.get_finished_spans()
        span = _get_span(spans, "backtest.execute")
        assert span.attributes["execution.success"] is True
        assert "execution.fill_price" in span.attributes
        assert "execution.fill_quantity" in span.attributes

    async def test_execute_rejection_sets_attributes(self, span_exporter):
        backend = BacktestBackend(fill_probability=0.0, random_seed=42)
        await backend.connect()
        order = _make_order("AAPL", Side.BUY, 10)
        costs = DefaultCostModel().estimate_total("AAPL", 10, 150.0, "buy")

        await backend.execute(order, 150.0, costs)

        spans = span_exporter.get_finished_spans()
        span = _get_span(spans, "backtest.execute")
        assert span.attributes["execution.success"] is False

    async def test_execute_zero_quantity_rejection(self, span_exporter):
        backend = BacktestBackend(random_seed=42)
        await backend.connect()
        order = _make_order("AAPL", Side.BUY, 0)
        costs = DefaultCostModel().estimate_total("AAPL", 0, 150.0, "buy")

        await backend.execute(order, 150.0, costs)

        spans = span_exporter.get_finished_spans()
        span = _get_span(spans, "backtest.execute")
        assert span.attributes["execution.success"] is False
        assert "quantity" in span.attributes["execution.reject_reason"]


class TestPaperBackendSpans:
    async def test_execute_creates_span(self, span_exporter):
        backend = PaperBackend(config=PaperTradeConfig(random_seed=42, latency_ms=0))
        await backend.connect()
        order = _make_order("AAPL", Side.BUY, 10)
        costs = DefaultCostModel().estimate_total("AAPL", 10, 150.0, "buy")

        await backend.execute(order, 150.0, costs)

        spans = span_exporter.get_finished_spans()
        assert any(s.name == "paper.execute" for s in spans)

    async def test_execute_successful_fill_attributes(self, span_exporter):
        backend = PaperBackend(config=PaperTradeConfig(random_seed=42, latency_ms=0))
        await backend.connect()
        order = _make_order("AAPL", Side.BUY, 10)
        costs = DefaultCostModel().estimate_total("AAPL", 10, 150.0, "buy")

        await backend.execute(order, 150.0, costs)

        spans = span_exporter.get_finished_spans()
        span = _get_span(spans, "paper.execute")
        assert span.attributes["execution.success"] is True
        assert "execution.fill_price" in span.attributes
        assert "execution.fill_quantity" in span.attributes
        assert "execution.slippage_bps" in span.attributes

    async def test_execute_not_connected_rejection(self, span_exporter):
        backend = PaperBackend(config=PaperTradeConfig(random_seed=42))
        order = _make_order("AAPL", Side.BUY, 10)
        costs = DefaultCostModel().estimate_total("AAPL", 10, 150.0, "buy")

        await backend.execute(order, 150.0, costs)

        spans = span_exporter.get_finished_spans()
        span = _get_span(spans, "paper.execute")
        assert span.attributes["execution.success"] is False
        assert "not connected" in span.attributes["execution.reject_reason"].lower()


class TestOrderManagerSpans:
    async def test_process_signal_creates_root_span(self, span_exporter, portfolio, cost_model, risk_engine):
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        om.set_execution_backend(FakeExecutionBackend(success=True, price=150.0, quantity=10))

        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        await om.process_signal(signal, market_price=150.0)

        spans = span_exporter.get_finished_spans()
        root_spans = [s for s in spans if s.name == "order_manager.process_signal"]
        assert len(root_spans) == 1

    async def test_process_signal_sets_order_attributes(self, span_exporter, portfolio, cost_model, risk_engine):
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        om.set_execution_backend(FakeExecutionBackend(success=True, price=150.0, quantity=10))

        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        await om.process_signal(signal, market_price=150.0)

        spans = span_exporter.get_finished_spans()
        root = _get_span(spans, "order_manager.process_signal")
        assert root.attributes["order.symbol"] == "AAPL"
        assert root.attributes["order.side"] == "buy"
        assert root.attributes["order.quantity"] == 10
        assert root.attributes["order.market_price"] == 150.0
        assert "order.id" in root.attributes

    async def test_process_signal_creates_child_spans(self, span_exporter, portfolio, cost_model, risk_engine):
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        om.set_execution_backend(FakeExecutionBackend(success=True, price=150.0, quantity=10))

        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        await om.process_signal(signal, market_price=150.0)

        spans = span_exporter.get_finished_spans()
        span_names = {s.name for s in spans}
        assert "order_manager.process_signal" in span_names
        assert "order_manager.calculate_costs" in span_names
        assert "order_manager.risk_check" in span_names

    async def test_process_signal_parent_child_relationship(self, span_exporter, portfolio, cost_model, risk_engine):
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        om.set_execution_backend(FakeExecutionBackend(success=True, price=150.0, quantity=10))

        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        await om.process_signal(signal, market_price=150.0)

        spans = span_exporter.get_finished_spans()
        root = _get_span(spans, "order_manager.process_signal")
        children = [s for s in spans if s.parent is not None and s.parent.span_id == root.context.span_id]
        child_names = {c.name for c in children}
        assert "order_manager.calculate_costs" in child_names
        assert "order_manager.risk_check" in child_names

    async def test_process_signal_filled_status(self, span_exporter, portfolio, cost_model, risk_engine):
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        om.set_execution_backend(FakeExecutionBackend(success=True, price=150.0, quantity=10))

        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        await om.process_signal(signal, market_price=150.0)

        spans = span_exporter.get_finished_spans()
        root = _get_span(spans, "order_manager.process_signal")
        assert root.attributes["order.status"] == "filled"
        assert root.attributes["order.fill_price"] == 150.0
        assert root.attributes["order.fill_quantity"] == 10

    async def test_process_signal_rejected_validation(self, span_exporter, portfolio, cost_model, risk_engine):
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        om.set_execution_backend(FakeExecutionBackend())

        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10_000)
        await om.process_signal(signal, market_price=150.0)

        spans = span_exporter.get_finished_spans()
        root = _get_span(spans, "order_manager.process_signal")
        assert root.attributes["order.status"] == "rejected"

    async def test_process_signal_cost_span_attributes(self, span_exporter, portfolio, risk_engine):
        cost_model = DefaultCostModel(commission_per_trade=5.0)
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        om.set_execution_backend(FakeExecutionBackend(success=True, price=150.0, quantity=10))

        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        await om.process_signal(signal, market_price=150.0)

        spans = span_exporter.get_finished_spans()
        cost_span = _get_span(spans, "order_manager.calculate_costs")
        assert "cost.total" in cost_span.attributes
        assert "cost.commission" in cost_span.attributes
        assert "cost.slippage" in cost_span.attributes

    async def test_process_signal_risk_span_attributes(self, span_exporter, portfolio, cost_model, risk_engine):
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        om.set_execution_backend(FakeExecutionBackend(success=True, price=150.0, quantity=10))

        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        await om.process_signal(signal, market_price=150.0)

        spans = span_exporter.get_finished_spans()
        risk_span = _get_span(spans, "order_manager.risk_check")
        assert risk_span.attributes["risk.approved"] is True

    async def test_process_signal_risk_rejected_span(self, span_exporter, cost_model, portfolio):
        risk_engine = RiskEngine(max_daily_trades=0)
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        om.set_execution_backend(FakeExecutionBackend())

        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        await om.process_signal(signal, market_price=150.0)

        spans = span_exporter.get_finished_spans()
        risk_span = _get_span(spans, "order_manager.risk_check")
        assert risk_span.attributes["risk.approved"] is False
        assert "risk.reason" in risk_span.attributes

        root = _get_span(spans, "order_manager.process_signal")
        assert root.attributes["order.status"] == "risk_rejected"

    async def test_process_signal_no_backend_failure(self, span_exporter, cost_model, risk_engine, portfolio):
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)

        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=1)
        await om.process_signal(signal, market_price=150.0)

        spans = span_exporter.get_finished_spans()
        root = _get_span(spans, "order_manager.process_signal")
        assert root.attributes["order.status"] == "failed"

    async def test_process_signal_includes_cost_model_span(self, span_exporter, portfolio, cost_model, risk_engine):
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        om.set_execution_backend(FakeExecutionBackend(success=True, price=150.0, quantity=10))

        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        await om.process_signal(signal, market_price=150.0)

        spans = span_exporter.get_finished_spans()
        span_names = {s.name for s in spans}
        assert "cost_model.estimate_total" in span_names

    async def test_full_span_hierarchy(self, span_exporter, portfolio, cost_model, risk_engine):
        om = OrderManager(cost_model=cost_model, risk_engine=risk_engine, portfolio=portfolio)
        om.set_execution_backend(FakeExecutionBackend(success=True, price=150.0, quantity=10))

        signal = Signal.buy(symbol="AAPL", strategy_id="test", quantity=10)
        await om.process_signal(signal, market_price=150.0)

        spans = span_exporter.get_finished_spans()
        root = _get_span(spans, "order_manager.process_signal")
        root_span_id = root.context.span_id

        cost_child = _get_span(spans, "order_manager.calculate_costs")
        assert cost_child.parent.span_id == root_span_id

        cost_model_span = _get_span(spans, "cost_model.estimate_total")
        assert cost_model_span.parent.span_id == cost_child.context.span_id

        risk_child = _get_span(spans, "order_manager.risk_check")
        assert risk_child.parent.span_id == root_span_id


class TestTracingSetup:
    def test_get_tracer_returns_tracer(self):
        from engine.observability.tracing import get_tracer

        tracer = get_tracer("test")
        assert tracer is not None

    def test_get_tracer_default_name(self):
        from engine.observability.tracing import get_tracer

        tracer = get_tracer()
        assert tracer is not None


def _make_order(symbol: str, side: Side, quantity: int) -> Order:
    return Order(
        signal_id="test-signal",
        strategy_id="test-strategy",
        symbol=symbol,
        side=side,
        quantity=quantity,
    )


def _get_span(spans, name: str):
    matches = [s for s in spans if s.name == name]
    assert matches, f"Span '{name}' not found in {[s.name for s in spans]}"
    return matches[0]
