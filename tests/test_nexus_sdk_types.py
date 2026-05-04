"""Comprehensive tests for nexus_sdk.types module.

Covers Money, CostBreakdown, and PortfolioSnapshot.
"""

from __future__ import annotations

import pytest

from nexus_sdk.types import CostBreakdown, Money, PortfolioSnapshot


class TestMoney:
    def test_default_currency(self):
        m = Money(amount=100.0)
        assert m.currency == "USD"

    def test_custom_currency(self):
        m = Money(amount=50.0, currency="EUR")
        assert m.currency == "EUR"

    def test_as_pct_of_total(self):
        m = Money(amount=25.0)
        assert m.as_pct_of(100.0) == 25.0

    def test_as_pct_of_zero_total(self):
        m = Money(amount=25.0)
        assert m.as_pct_of(0.0) == 0.0

    def test_as_pct_of_negative_total(self):
        m = Money(amount=10.0)
        result = m.as_pct_of(-100.0)
        assert result == -10.0

    def test_as_pct_of_larger_than_total(self):
        m = Money(amount=200.0)
        result = m.as_pct_of(100.0)
        assert result == 200.0

    def test_as_pct_zero_amount(self):
        m = Money(amount=0.0)
        assert m.as_pct_of(100.0) == 0.0

    def test_as_pct_negative_amount(self):
        m = Money(amount=-50.0)
        assert m.as_pct_of(100.0) == -50.0


class TestCostBreakdown:
    def test_default_all_zero(self):
        cb = CostBreakdown()
        assert cb.commission.amount == 0.0
        assert cb.spread.amount == 0.0
        assert cb.slippage.amount == 0.0
        assert cb.exchange_fee.amount == 0.0
        assert cb.tax_estimate.amount == 0.0

    def test_total_property_sums_all(self):
        cb = CostBreakdown(
            commission=Money(1.0),
            spread=Money(2.0),
            slippage=Money(3.0),
            exchange_fee=Money(0.5),
            tax_estimate=Money(1.5),
        )
        total = cb.total
        assert isinstance(total, Money)
        assert total.amount == 8.0

    def test_total_with_only_commission(self):
        cb = CostBreakdown(commission=Money(10.0))
        assert cb.total.amount == 10.0

    def test_total_with_zero_values(self):
        cb = CostBreakdown()
        assert cb.total.amount == 0.0

    def test_total_with_large_values(self):
        cb = CostBreakdown(
            commission=Money(100.0),
            spread=Money(200.0),
            slippage=Money(300.0),
        )
        assert cb.total.amount == 600.0


class TestPortfolioSnapshot:
    def test_defaults(self):
        snap = PortfolioSnapshot()
        assert snap.cash == 0.0
        assert snap.positions == {}
        assert snap.total_value == 0.0
        assert snap.realized_pnl == 0.0
        assert snap.unrealized_pnl == 0.0
        assert snap.day_pnl == 0.0
        assert snap.total_return_pct == 0.0

    def test_get_position_existing(self):
        snap = PortfolioSnapshot(
            positions={"AAPL": {"qty": 100, "market_value": 15000.0}},
        )
        pos = snap.get_position("AAPL")
        assert pos is not None
        assert pos["qty"] == 100

    def test_get_position_missing(self):
        snap = PortfolioSnapshot(positions={"AAPL": {"qty": 100}})
        assert snap.get_position("MSFT") is None

    def test_get_position_empty_positions(self):
        snap = PortfolioSnapshot()
        assert snap.get_position("AAPL") is None

    def test_has_position_true(self):
        snap = PortfolioSnapshot(positions={"AAPL": {"qty": 100}})
        assert snap.has_position("AAPL") is True

    def test_has_position_false(self):
        snap = PortfolioSnapshot(positions={"AAPL": {"qty": 100}})
        assert snap.has_position("MSFT") is False

    def test_has_position_empty(self):
        snap = PortfolioSnapshot()
        assert snap.has_position("AAPL") is False

    def test_allocation_weight_with_position(self):
        snap = PortfolioSnapshot(
            total_value=100_000.0,
            positions={"AAPL": {"market_value": 25_000.0}},
        )
        weight = snap.allocation_weight("AAPL")
        assert abs(weight - 0.25) < 1e-10

    def test_allocation_weight_missing_symbol(self):
        snap = PortfolioSnapshot(
            total_value=100_000.0,
            positions={"AAPL": {"market_value": 25_000.0}},
        )
        assert snap.allocation_weight("MSFT") == 0.0

    def test_allocation_weight_zero_total_value(self):
        snap = PortfolioSnapshot(
            total_value=0.0,
            positions={"AAPL": {"market_value": 25_000.0}},
        )
        assert snap.allocation_weight("AAPL") == 0.0

    def test_allocation_weight_position_without_market_value(self):
        snap = PortfolioSnapshot(
            total_value=100_000.0,
            positions={"AAPL": {"qty": 100}},
        )
        assert snap.allocation_weight("AAPL") == 0.0

    def test_summary_format(self):
        snap = PortfolioSnapshot(
            cash=50_000.0,
            total_value=150_000.0,
            positions={"AAPL": {"qty": 100}, "MSFT": {"qty": 50}},
        )
        s = snap.summary()
        assert "$150,000.00" in s
        assert "$50,000.00" in s
        assert "Positions: 2" in s

    def test_summary_zero_values(self):
        snap = PortfolioSnapshot()
        s = snap.summary()
        assert "$0.00" in s
        assert "Positions: 0" in s

    def test_with_custom_timestamp(self):
        from datetime import UTC, datetime

        ts = datetime(2025, 1, 1, tzinfo=UTC)
        snap = PortfolioSnapshot(timestamp=ts, cash=100_000.0)
        assert snap.timestamp == ts
