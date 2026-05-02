"""Unit tests for the wash-sale detector — gh#156."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from engine.core.tax import (
    WASH_SALE_WINDOW_DAYS,
    Trade,
    TradeSide,
    detect_wash_sales,
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


# ---------------------------------------------------------------------------
# Trade validation
# ---------------------------------------------------------------------------


class TestTrade:
    def test_zero_quantity_rejected(self):
        with pytest.raises(ValueError):
            Trade(
                trade_id="t1",
                symbol="AAPL",
                side=TradeSide.BUY,
                quantity=Decimal("0"),
                price=Decimal("100"),
                when=datetime(2026, 5, 1, tzinfo=UTC),
            )

    def test_negative_price_rejected(self):
        with pytest.raises(ValueError):
            Trade(
                trade_id="t1",
                symbol="AAPL",
                side=TradeSide.BUY,
                quantity=Decimal("10"),
                price=Decimal("-1"),
                when=datetime(2026, 5, 1, tzinfo=UTC),
            )


# ---------------------------------------------------------------------------
# Constant
# ---------------------------------------------------------------------------


class TestConstants:
    def test_window_is_thirty_days(self):
        # IRS Section 1091 — 30 days each side. The detector tolerates
        # exactly +/- WASH_SALE_WINDOW_DAYS as a match.
        assert WASH_SALE_WINDOW_DAYS == 30


# ---------------------------------------------------------------------------
# Detector behaviour
# ---------------------------------------------------------------------------


class TestNoMatch:
    def test_gain_sale_not_a_wash_sale(self):
        # Buy at 100, sell at 120 — gain, no wash sale possible.
        trades = [
            _trade("b1", "AAPL", TradeSide.BUY, "10", "100", 0),
            _trade("s1", "AAPL", TradeSide.SELL, "10", "120", 5),
            _trade("b2", "AAPL", TradeSide.BUY, "10", "121", 10),
        ]
        assert detect_wash_sales(trades) == []

    def test_loss_with_no_replacement_is_not_wash(self):
        trades = [
            _trade("b1", "AAPL", TradeSide.BUY, "10", "100", 0),
            _trade("s1", "AAPL", TradeSide.SELL, "10", "80", 5),
        ]
        assert detect_wash_sales(trades) == []

    def test_replacement_outside_window_does_not_match(self):
        # Buy 0, sell 5 (loss), buy again at day 36 — outside 30-day window.
        trades = [
            _trade("b1", "AAPL", TradeSide.BUY, "10", "100", 0),
            _trade("s1", "AAPL", TradeSide.SELL, "10", "80", 5),
            _trade("b2", "AAPL", TradeSide.BUY, "10", "85", 36),
        ]
        assert detect_wash_sales(trades) == []

    def test_different_symbol_does_not_match(self):
        trades = [
            _trade("b1", "AAPL", TradeSide.BUY, "10", "100", 0),
            _trade("s1", "AAPL", TradeSide.SELL, "10", "80", 5),
            _trade("b2", "MSFT", TradeSide.BUY, "10", "80", 10),
        ]
        assert detect_wash_sales(trades) == []


class TestSimpleMatch:
    def test_replacement_after_loss_within_window(self):
        # Buy 10@100, sell 10@80 (loss=200), re-buy 10@85 within 30 days.
        trades = [
            _trade("b1", "AAPL", TradeSide.BUY, "10", "100", 0),
            _trade("s1", "AAPL", TradeSide.SELL, "10", "80", 5),
            _trade("b2", "AAPL", TradeSide.BUY, "10", "85", 10),
        ]
        adjustments = detect_wash_sales(trades)
        assert len(adjustments) == 1
        adj = adjustments[0]
        assert adj.sale_trade_id == "s1"
        assert adj.replacement_trade_id == "b2"
        assert adj.symbol == "AAPL"
        assert adj.matched_quantity == Decimal("10")
        # Full loss is disallowed because replacement qty == sale qty.
        assert adj.disallowed_loss == Decimal("200.0000")


class TestPartialMatch:
    def test_replacement_smaller_than_sale_partial_disallow(self):
        # Buy 10@100, sell 10@80 (loss=200), replacement 4 shares only.
        # Disallowed loss is prorated: 200 * (4/10) = 80.
        trades = [
            _trade("b1", "AAPL", TradeSide.BUY, "10", "100", 0),
            _trade("s1", "AAPL", TradeSide.SELL, "10", "80", 5),
            _trade("b2", "AAPL", TradeSide.BUY, "4", "85", 10),
        ]
        adjustments = detect_wash_sales(trades)
        assert len(adjustments) == 1
        adj = adjustments[0]
        assert adj.matched_quantity == Decimal("4")
        assert adj.disallowed_loss == Decimal("80.0000")

    def test_two_replacements_consume_loss_in_order(self):
        # Sale qty 10, two later buys of 6 and 5. Should match all 10.
        trades = [
            _trade("b1", "AAPL", TradeSide.BUY, "10", "100", 0),
            _trade("s1", "AAPL", TradeSide.SELL, "10", "80", 5),
            _trade("b2", "AAPL", TradeSide.BUY, "6", "85", 10),
            _trade("b3", "AAPL", TradeSide.BUY, "5", "85", 12),
        ]
        adjustments = detect_wash_sales(trades)
        # Two adjustments: 6 shares to b2, 4 shares to b3.
        replacements = {a.replacement_trade_id: a.matched_quantity for a in adjustments}
        assert replacements == {"b2": Decimal("6"), "b3": Decimal("4")}
        # Loss totals 200; split should add to 200 (within rounding).
        total = sum((a.disallowed_loss for a in adjustments), Decimal("0"))
        assert total == Decimal("200.0000")


class TestExactBoundary:
    def test_buy_exactly_thirty_days_after_matches(self):
        # The window is inclusive at the boundary.
        trades = [
            _trade("b1", "AAPL", TradeSide.BUY, "10", "100", 0),
            _trade("s1", "AAPL", TradeSide.SELL, "10", "80", 0),
            _trade("b2", "AAPL", TradeSide.BUY, "10", "85", WASH_SALE_WINDOW_DAYS),
        ]
        adjustments = detect_wash_sales(trades)
        assert len(adjustments) == 1
        assert adjustments[0].replacement_trade_id == "b2"

    def test_buy_thirty_one_days_after_does_not_match(self):
        trades = [
            _trade("b1", "AAPL", TradeSide.BUY, "10", "100", 0),
            _trade("s1", "AAPL", TradeSide.SELL, "10", "80", 0),
            _trade(
                "b2",
                "AAPL",
                TradeSide.BUY,
                "10",
                "85",
                WASH_SALE_WINDOW_DAYS + 1,
            ),
        ]
        assert detect_wash_sales(trades) == []


class TestExplicitBasis:
    def test_basis_map_overrides_fifo(self):
        # Same shape as the simple match, but explicit cost basis says
        # the sale was at break-even (no loss). Detector must skip.
        trades = [
            _trade("b1", "AAPL", TradeSide.BUY, "10", "100", 0),
            _trade("s1", "AAPL", TradeSide.SELL, "10", "80", 5),
            _trade("b2", "AAPL", TradeSide.BUY, "10", "85", 10),
        ]
        # Sale gross = 800. If basis = 800, loss = 0.
        adjustments = detect_wash_sales(
            trades, cost_basis_for={"s1": Decimal("800")}
        )
        assert adjustments == []
