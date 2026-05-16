"""Tests for paper trade broker interface types."""

from __future__ import annotations

from engine.core.execution.paper_broker_interface import (
    FillPriority,
    OrderRejectReason,
    PaperOrderStatus,
    PaperPortfolioSnapshot,
    PaperPosition,
    PaperTradeBrokerConfig,
    PaperTradeFill,
    PaperTradeRiskConfig,
)


class TestPaperPosition:
    def test_is_long(self):
        pos = PaperPosition(
            symbol="AAPL", quantity=100, avg_entry_price=150.0,
            current_price=160.0, unrealized_pnl=1000.0,
            realized_pnl=0.0, market_value=16000.0,
        )
        assert pos.is_long
        assert not pos.is_short

    def test_is_short(self):
        pos = PaperPosition(
            symbol="AAPL", quantity=-100, avg_entry_price=150.0,
            current_price=140.0, unrealized_pnl=1000.0,
            realized_pnl=0.0, market_value=14000.0,
        )
        assert pos.is_short
        assert not pos.is_long

    def test_zero_quantity(self):
        pos = PaperPosition(
            symbol="AAPL", quantity=0, avg_entry_price=0.0,
            current_price=0.0, unrealized_pnl=0.0,
            realized_pnl=0.0, market_value=0.0,
        )
        assert not pos.is_long
        assert not pos.is_short


class TestPaperPortfolioSnapshot:
    def test_buying_power(self):
        snap = PaperPortfolioSnapshot(
            total_equity=100_000.0,
            cash=50_000.0,
            positions={},
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            total_pnl=0.0,
            timestamp="2024-01-01T00:00:00Z",
        )
        assert snap.buying_power == 75_000.0


class TestPaperTradeBrokerConfig:
    def test_defaults(self):
        config = PaperTradeBrokerConfig()
        assert config.fill_probability == 0.95
        assert config.partial_fill_enabled is True
        assert config.latency_ms == 50.0
        assert config.commission_per_share == 0.005
        assert config.risk_config is not None

    def test_custom_risk_config(self):
        risk = PaperTradeRiskConfig(max_position_size=500)
        config = PaperTradeBrokerConfig(risk_config=risk)
        assert config.risk_config.max_position_size == 500

    def test_post_init_defaults(self):
        config = PaperTradeBrokerConfig()
        assert config.slippage_model_kwargs == {}
        assert config.risk_config is not None


class TestPaperTradeRiskConfig:
    def test_defaults(self):
        risk = PaperTradeRiskConfig()
        assert risk.max_position_size == 10_000
        assert risk.max_orders_per_minute == 60
        assert risk.banned_symbols == set()
        assert risk.allowed_symbols is None

    def test_custom_banned_symbols(self):
        risk = PaperTradeRiskConfig(banned_symbols={"AAPL", "TSLA"})
        assert "AAPL" in risk.banned_symbols
        assert "TSLA" in risk.banned_symbols


class TestEnums:
    def test_order_reject_reasons(self):
        assert OrderRejectReason.NOT_CONNECTED == "not_connected"
        assert OrderRejectReason.RISK_LIMIT_EXCEEDED == "risk_limit_exceeded"
        assert OrderRejectReason.SYMBOL_BANNED == "symbol_banned"

    def test_paper_order_status(self):
        assert PaperOrderStatus.PENDING == "pending"
        assert PaperOrderStatus.FILLED == "filled"
        assert PaperOrderStatus.CANCELLED == "cancelled"

    def test_fill_priority(self):
        assert FillPriority.FIFO == "fifo"
        assert FillPriority.PRO_RATA == "pro_rata"


class TestPaperTradeFill:
    def test_fill_creation(self):
        fill = PaperTradeFill(
            fill_id="fill-1",
            order_id="ord-1",
            symbol="AAPL",
            side="buy",
            quantity=100,
            price=150.0,
            commission=0.5,
            timestamp="2024-01-01T00:00:00Z",
            slippage_bps=3.5,
        )
        assert fill.fill_id == "fill-1"
        assert fill.slippage_bps == 3.5
