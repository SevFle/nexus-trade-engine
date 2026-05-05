"""
Comprehensive unit tests for tax/wash_sale.py — Trade model, TradeSide,
detect_wash_sales, detect_wash_sales_for_jurisdiction, and edge cases
not covered by test_wash_sale.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from engine.core.tax import (
    WASH_SALE_WINDOW_DAYS,
    Trade,
    TradeSide,
    WashSaleAdjustment,
    detect_wash_sales,
    detect_wash_sales_for_jurisdiction,
)


def _trade(
    tid: str,
    symbol: str,
    side: TradeSide,
    qty: str,
    price: str,
    days_offset: int,
) -> Trade:
    base = datetime(2026, 5, 1, tzinfo=UTC)
    return Trade(
        trade_id=tid,
        symbol=symbol,
        side=side,
        quantity=Decimal(qty),
        price=Decimal(price),
        when=base + timedelta(days=days_offset),
    )


class TestTradeSide:
    def test_buy_value(self):
        assert TradeSide.BUY == "buy"

    def test_sell_value(self):
        assert TradeSide.SELL == "sell"


class TestTradeGross:
    def test_gross_calculation(self):
        t = Trade(
            trade_id="t1",
            symbol="AAPL",
            side=TradeSide.BUY,
            quantity=Decimal("10"),
            price=Decimal("150"),
            when=datetime(2026, 5, 1, tzinfo=UTC),
        )
        assert t.gross == Decimal("1500")

    def test_gross_with_fractional_quantity(self):
        t = Trade(
            trade_id="t1",
            symbol="AAPL",
            side=TradeSide.BUY,
            quantity=Decimal("2.5"),
            price=Decimal("100"),
            when=datetime(2026, 5, 1, tzinfo=UTC),
        )
        assert t.gross == Decimal("250")


class TestTradeValidation:
    def test_zero_quantity_rejected(self):
        with pytest.raises(ValueError, match="quantity must be positive"):
            Trade(
                trade_id="t1",
                symbol="AAPL",
                side=TradeSide.BUY,
                quantity=Decimal("0"),
                price=Decimal("100"),
                when=datetime(2026, 5, 1, tzinfo=UTC),
            )

    def test_negative_quantity_rejected(self):
        with pytest.raises(ValueError, match="quantity must be positive"):
            Trade(
                trade_id="t1",
                symbol="AAPL",
                side=TradeSide.BUY,
                quantity=Decimal("-5"),
                price=Decimal("100"),
                when=datetime(2026, 5, 1, tzinfo=UTC),
            )

    def test_negative_price_rejected(self):
        with pytest.raises(ValueError, match="price must be non-negative"):
            Trade(
                trade_id="t1",
                symbol="AAPL",
                side=TradeSide.BUY,
                quantity=Decimal("10"),
                price=Decimal("-1"),
                when=datetime(2026, 5, 1, tzinfo=UTC),
            )

    def test_zero_price_allowed(self):
        t = Trade(
            trade_id="t1",
            symbol="AAPL",
            side=TradeSide.BUY,
            quantity=Decimal("10"),
            price=Decimal("0"),
            when=datetime(2026, 5, 1, tzinfo=UTC),
        )
        assert t.price == Decimal("0")

    def test_frozen_dataclass(self):
        t = Trade(
            trade_id="t1",
            symbol="AAPL",
            side=TradeSide.BUY,
            quantity=Decimal("10"),
            price=Decimal("100"),
            when=datetime(2026, 5, 1, tzinfo=UTC),
        )
        with pytest.raises(AttributeError):
            t.quantity = Decimal("20")


class TestWashSaleAdjustment:
    def test_fields(self):
        adj = WashSaleAdjustment(
            sale_trade_id="s1",
            replacement_trade_id="b2",
            symbol="AAPL",
            matched_quantity=Decimal("10"),
            disallowed_loss=Decimal("200"),
        )
        assert adj.sale_trade_id == "s1"
        assert adj.replacement_trade_id == "b2"
        assert adj.symbol == "AAPL"
        assert adj.matched_quantity == Decimal("10")
        assert adj.disallowed_loss == Decimal("200")


class TestDetectWashSalesEdgeCases:
    def test_empty_trades_list(self):
        assert detect_wash_sales([]) == []

    def test_window_days_zero_returns_empty(self):
        trades = [
            _trade("b1", "AAPL", TradeSide.BUY, "10", "100", 0),
            _trade("s1", "AAPL", TradeSide.SELL, "10", "80", 5),
            _trade("b2", "AAPL", TradeSide.BUY, "10", "85", 10),
        ]
        assert detect_wash_sales(trades, window_days=0) == []

    def test_negative_window_days_raises(self):
        with pytest.raises(ValueError, match="window_days must be non-negative"):
            detect_wash_sales([], window_days=-1)

    def test_only_buys_no_adjustments(self):
        trades = [
            _trade("b1", "AAPL", TradeSide.BUY, "10", "100", 0),
            _trade("b2", "AAPL", TradeSide.BUY, "10", "105", 5),
        ]
        assert detect_wash_sales(trades) == []

    def test_only_sells_no_adjustments(self):
        trades = [
            _trade("s1", "AAPL", TradeSide.SELL, "10", "100", 0),
        ]
        assert detect_wash_sales(trades) == []

    def test_replacement_before_loss_within_window(self):
        trades = [
            _trade("b1", "AAPL", TradeSide.BUY, "10", "100", 0),
            _trade("b2", "AAPL", TradeSide.BUY, "10", "85", 3),
            _trade("s1", "AAPL", TradeSide.SELL, "10", "80", 5),
        ]
        adjustments = detect_wash_sales(trades)
        assert len(adjustments) == 1
        assert adjustments[0].replacement_trade_id == "b2"

    def test_multiple_losses_same_symbol(self):
        trades = [
            _trade("b1", "AAPL", TradeSide.BUY, "10", "100", 0),
            _trade("s1", "AAPL", TradeSide.SELL, "10", "80", 5),
            _trade("b2", "AAPL", TradeSide.BUY, "10", "85", 10),
            _trade("s2", "AAPL", TradeSide.SELL, "10", "75", 15),
            _trade("b3", "AAPL", TradeSide.BUY, "10", "80", 20),
        ]
        adjustments = detect_wash_sales(trades)
        assert len(adjustments) >= 1

    def test_sell_at_zero_price_full_loss(self):
        trades = [
            _trade("b1", "AAPL", TradeSide.BUY, "10", "100", 0),
            _trade("s1", "AAPL", TradeSide.SELL, "10", "0", 5),
            _trade("b2", "AAPL", TradeSide.BUY, "10", "50", 10),
        ]
        adjustments = detect_wash_sales(trades)
        assert len(adjustments) == 1
        assert adjustments[0].disallowed_loss == Decimal("1000.0000")

    def test_self_trade_not_matched(self):
        trades = [
            _trade("b1", "AAPL", TradeSide.BUY, "10", "100", 0),
            _trade("s1", "AAPL", TradeSide.SELL, "10", "80", 5),
            _trade("b2", "AAPL", TradeSide.BUY, "10", "85", 10),
        ]
        adjustments = detect_wash_sales(trades)
        for adj in adjustments:
            assert adj.replacement_trade_id != adj.sale_trade_id

    def test_output_sorted_by_ids(self):
        trades = [
            _trade("b1", "AAPL", TradeSide.BUY, "10", "100", 0),
            _trade("s1", "AAPL", TradeSide.SELL, "10", "80", 5),
            _trade("b2", "AAPL", TradeSide.BUY, "5", "85", 7),
            _trade("b3", "AAPL", TradeSide.BUY, "5", "86", 8),
        ]
        adjustments = detect_wash_sales(trades)
        if len(adjustments) > 1:
            for i in range(len(adjustments) - 1):
                assert (adjustments[i].sale_trade_id, adjustments[i].replacement_trade_id) <= (
                    adjustments[i + 1].sale_trade_id,
                    adjustments[i + 1].replacement_trade_id,
                )


class TestDetectWashSalesCostBasisOverride:
    def test_explicit_basis_overrides_fifo(self):
        trades = [
            _trade("b1", "AAPL", TradeSide.BUY, "10", "100", 0),
            _trade("s1", "AAPL", TradeSide.SELL, "10", "80", 5),
            _trade("b2", "AAPL", TradeSide.BUY, "10", "85", 10),
        ]
        adjustments = detect_wash_sales(
            trades, cost_basis_for={"s1": Decimal("800")}
        )
        assert adjustments == []

    def test_explicit_basis_with_loss(self):
        trades = [
            _trade("b1", "AAPL", TradeSide.BUY, "10", "100", 0),
            _trade("s1", "AAPL", TradeSide.SELL, "10", "80", 5),
            _trade("b2", "AAPL", TradeSide.BUY, "10", "85", 10),
        ]
        adjustments = detect_wash_sales(
            trades, cost_basis_for={"s1": Decimal("900")}
        )
        assert len(adjustments) == 1
        assert adjustments[0].disallowed_loss == Decimal("100.0000")


class TestDetectWashSalesForJurisdiction:
    def test_with_window_days_zero_jurisdiction(self):
        class FakeJurisdiction:
            wash_sale_window_days = 0

        trades = [
            _trade("b1", "AAPL", TradeSide.BUY, "10", "100", 0),
            _trade("s1", "AAPL", TradeSide.SELL, "10", "80", 5),
            _trade("b2", "AAPL", TradeSide.BUY, "10", "85", 10),
        ]
        result = detect_wash_sales_for_jurisdiction(trades, FakeJurisdiction())
        assert result == []

    def test_with_standard_jurisdiction(self):
        class USJurisdiction:
            wash_sale_window_days = 30

        trades = [
            _trade("b1", "AAPL", TradeSide.BUY, "10", "100", 0),
            _trade("s1", "AAPL", TradeSide.SELL, "10", "80", 5),
            _trade("b2", "AAPL", TradeSide.BUY, "10", "85", 10),
        ]
        result = detect_wash_sales_for_jurisdiction(trades, USJurisdiction())
        assert len(result) == 1

    def test_with_custom_window_jurisdiction(self):
        class CustomJurisdiction:
            wash_sale_window_days = 60

        trades = [
            _trade("b1", "AAPL", TradeSide.BUY, "10", "100", 0),
            _trade("s1", "AAPL", TradeSide.SELL, "10", "80", 5),
            _trade("b2", "AAPL", TradeSide.BUY, "10", "85", 40),
        ]
        result = detect_wash_sales_for_jurisdiction(trades, CustomJurisdiction())
        assert len(result) == 1

    def test_jurisdiction_passes_cost_basis(self):
        class USJurisdiction:
            wash_sale_window_days = 30

        trades = [
            _trade("b1", "AAPL", TradeSide.BUY, "10", "100", 0),
            _trade("s1", "AAPL", TradeSide.SELL, "10", "80", 5),
            _trade("b2", "AAPL", TradeSide.BUY, "10", "85", 10),
        ]
        result = detect_wash_sales_for_jurisdiction(
            trades, USJurisdiction(), cost_basis_for={"s1": Decimal("800")}
        )
        assert result == []
