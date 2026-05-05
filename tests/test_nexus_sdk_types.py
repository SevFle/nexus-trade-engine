"""Comprehensive tests for nexus_sdk.types module.

Covers Money, CostBreakdown, and PortfolioSnapshot with edge cases,
boundary values, and error conditions.
"""

from __future__ import annotations

from datetime import UTC, datetime

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

    def test_as_pct_of_zero_total_raises(self):
        m = Money(amount=25.0)
        with pytest.raises(ValueError, match="total must not be zero"):
            m.as_pct_of(0.0)

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

    def test_as_pct_very_large_values(self):
        m = Money(amount=1e15)
        result = m.as_pct_of(2e15)
        assert result == 50.0

    def test_as_pct_very_small_total(self):
        m = Money(amount=0.001)
        result = m.as_pct_of(0.01)
        assert result == pytest.approx(10.0)

    def test_as_pct_exactly_100(self):
        m = Money(amount=50.0)
        assert m.as_pct_of(50.0) == 100.0

    def test_as_pct_both_negative(self):
        m = Money(amount=-25.0)
        result = m.as_pct_of(-50.0)
        assert result == 50.0

    def test_as_pct_negative_zero_float(self):
        m = Money(amount=25.0)
        with pytest.raises(ValueError, match="total must not be zero"):
            m.as_pct_of(-0.0)

    def test_as_pct_near_zero_total_raises(self):
        m = Money(amount=25.0)
        with pytest.raises(ValueError, match="total must not be zero"):
            m.as_pct_of(1e-13)

    def test_negative_amount(self):
        m = Money(amount=-100.0)
        assert m.amount == -100.0

    def test_zero_amount(self):
        m = Money(amount=0.0)
        assert m.amount == 0.0

    def test_various_currencies(self):
        for currency in ("USD", "EUR", "GBP", "JPY", "CHF"):
            m = Money(amount=100.0, currency=currency)
            assert m.currency == currency

    def test_as_pct_of_near_zero_above_epsilon_works(self):
        m = Money(amount=1e-10)
        result = m.as_pct_of(1e-11)
        assert result == pytest.approx(1000.0)

    def test_as_pct_of_below_epsilon_raises(self):
        m = Money(amount=1.0)
        with pytest.raises(ValueError, match="total must not be zero"):
            m.as_pct_of(1e-13)

    def test_as_pct_of_at_epsilon_boundary_does_not_raise(self):
        m = Money(amount=1.0)
        result = m.as_pct_of(1e-12)
        assert result == pytest.approx(1e14)


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

    def test_total_with_negative_components(self):
        cb = CostBreakdown(
            commission=Money(10.0),
            spread=Money(-5.0),
            slippage=Money(3.0),
        )
        assert cb.total.amount == 8.0

    def test_total_with_all_negative(self):
        cb = CostBreakdown(
            commission=Money(-1.0),
            spread=Money(-2.0),
            slippage=Money(-3.0),
            exchange_fee=Money(-4.0),
            tax_estimate=Money(-5.0),
        )
        assert cb.total.amount == -15.0

    def test_total_preserves_default_currency(self):
        cb = CostBreakdown(commission=Money(10.0, currency="USD"))
        assert cb.total.currency == "USD"

    def test_each_component_is_money_instance(self):
        cb = CostBreakdown()
        assert isinstance(cb.commission, Money)
        assert isinstance(cb.spread, Money)
        assert isinstance(cb.slippage, Money)
        assert isinstance(cb.exchange_fee, Money)
        assert isinstance(cb.tax_estimate, Money)

    def test_mixed_currency_components(self):
        cb = CostBreakdown(
            commission=Money(10.0, currency="USD"),
            spread=Money(5.0, currency="EUR"),
        )
        with pytest.raises(ValueError, match="different currencies"):
            _ = cb.total

    def test_very_small_cost_values(self):
        cb = CostBreakdown(
            commission=Money(0.0001),
            spread=Money(0.0002),
            slippage=Money(0.0003),
        )
        assert cb.total.amount == pytest.approx(0.0006)

    def test_very_large_cost_values(self):
        cb = CostBreakdown(
            commission=Money(1e9),
            spread=Money(2e9),
            slippage=Money(3e9),
        )
        assert cb.total.amount == 6e9

    def test_single_component_nonzero(self):
        cb = CostBreakdown(tax_estimate=Money(42.0))
        assert cb.total.amount == 42.0
        assert cb.commission.amount == 0.0

    def test_all_same_non_usd_currency(self):
        cb = CostBreakdown(
            commission=Money(1.0, currency="EUR"),
            spread=Money(2.0, currency="EUR"),
            slippage=Money(3.0, currency="EUR"),
            exchange_fee=Money(4.0, currency="EUR"),
            tax_estimate=Money(5.0, currency="EUR"),
        )
        total = cb.total
        assert total.amount == 15.0
        assert total.currency == "EUR"

    def test_three_way_mixed_currency_raises(self):
        cb = CostBreakdown(
            commission=Money(1.0, currency="USD"),
            spread=Money(2.0, currency="EUR"),
            slippage=Money(3.0, currency="GBP"),
        )
        with pytest.raises(ValueError, match="different currencies"):
            _ = cb.total

    def test_four_mixed_one_different_raises(self):
        cb = CostBreakdown(
            commission=Money(1.0, currency="JPY"),
            spread=Money(2.0, currency="JPY"),
            slippage=Money(3.0, currency="JPY"),
            exchange_fee=Money(4.0, currency="JPY"),
            tax_estimate=Money(5.0, currency="USD"),
        )
        with pytest.raises(ValueError, match="different currencies"):
            _ = cb.total

    def test_total_currency_comes_from_commission(self):
        cb = CostBreakdown(
            commission=Money(10.0, currency="CHF"),
            spread=Money(0.0, currency="CHF"),
            slippage=Money(0.0, currency="CHF"),
            exchange_fee=Money(0.0, currency="CHF"),
            tax_estimate=Money(0.0, currency="CHF"),
        )
        assert cb.total.currency == "CHF"


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
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        snap = PortfolioSnapshot(timestamp=ts, cash=100_000.0)
        assert snap.timestamp == ts

    def test_allocation_weight_multiple_positions(self):
        snap = PortfolioSnapshot(
            total_value=100_000.0,
            positions={
                "AAPL": {"market_value": 50_000.0},
                "MSFT": {"market_value": 30_000.0},
                "GOOGL": {"market_value": 20_000.0},
            },
        )
        assert snap.allocation_weight("AAPL") == pytest.approx(0.5)
        assert snap.allocation_weight("MSFT") == pytest.approx(0.3)
        assert snap.allocation_weight("GOOGL") == pytest.approx(0.2)

    def test_allocation_weights_sum_approximately_one(self):
        snap = PortfolioSnapshot(
            total_value=100_000.0,
            positions={
                "AAPL": {"market_value": 40_000.0},
                "MSFT": {"market_value": 35_000.0},
                "TSLA": {"market_value": 25_000.0},
            },
        )
        total_weight = sum(
            snap.allocation_weight(s) for s in ["AAPL", "MSFT", "TSLA"]
        )
        assert total_weight == pytest.approx(1.0)

    def test_realized_pnl_tracking(self):
        snap = PortfolioSnapshot(
            cash=120_000.0,
            total_value=120_000.0,
            realized_pnl=20_000.0,
        )
        assert snap.realized_pnl == 20_000.0

    def test_unrealized_pnl_tracking(self):
        snap = PortfolioSnapshot(
            unrealized_pnl=-5000.0,
        )
        assert snap.unrealized_pnl == -5000.0

    def test_day_pnl_tracking(self):
        snap = PortfolioSnapshot(day_pnl=1500.0)
        assert snap.day_pnl == 1500.0

    def test_total_return_pct(self):
        snap = PortfolioSnapshot(total_return_pct=12.5)
        assert snap.total_return_pct == 12.5

    def test_negative_total_return_pct(self):
        snap = PortfolioSnapshot(total_return_pct=-8.3)
        assert snap.total_return_pct == -8.3

    def test_position_with_all_fields(self):
        pos = {
            "qty": 100,
            "market_value": 15000.0,
            "avg_cost": 140.0,
            "current_price": 150.0,
        }
        snap = PortfolioSnapshot(
            total_value=15000.0,
            positions={"AAPL": pos},
        )
        got = snap.get_position("AAPL")
        assert got["qty"] == 100
        assert got["market_value"] == 15000.0
        assert got["avg_cost"] == 140.0
        assert got["current_price"] == 150.0

    def test_summary_format_with_fractional_cash(self):
        snap = PortfolioSnapshot(
            cash=1_234.56,
            total_value=9_876.54,
            positions={"AAPL": {"qty": 10}},
        )
        s = snap.summary()
        assert "$9,876.54" in s
        assert "$1,234.56" in s
        assert "Positions: 1" in s

    def test_many_positions(self):
        positions = {
            f"SYM{i}": {"qty": i, "market_value": float(i * 100)}
            for i in range(50)
        }
        snap = PortfolioSnapshot(
            total_value=127_500.0,
            positions=positions,
        )
        assert snap.has_position("SYM0")
        assert snap.has_position("SYM49")
        assert not snap.has_position("SYM50")
        assert len(snap.positions) == 50

    def test_timestamp_auto_generated(self):
        before = datetime.now(UTC)
        snap = PortfolioSnapshot()
        after = datetime.now(UTC)
        assert before <= snap.timestamp <= after

    def test_allocation_weight_market_value_zero(self):
        snap = PortfolioSnapshot(
            total_value=100_000.0,
            positions={"AAPL": {"market_value": 0.0}},
        )
        assert snap.allocation_weight("AAPL") == 0.0

    def test_get_position_returns_dict(self):
        snap = PortfolioSnapshot(
            positions={"AAPL": {"qty": 100}},
        )
        pos = snap.get_position("AAPL")
        assert isinstance(pos, dict)

    def test_negative_cash(self):
        snap = PortfolioSnapshot(cash=-500.0)
        assert snap.cash == -500.0

    def test_empty_position_dict_value(self):
        snap = PortfolioSnapshot(
            total_value=100_000.0,
            positions={"AAPL": {}},
        )
        assert snap.get_position("AAPL") == {}
        assert snap.allocation_weight("AAPL") == 0.0
        assert snap.has_position("AAPL") is True
