"""Tests for engine.core.tca — post-trade Transaction Cost Analysis."""

from __future__ import annotations

import pytest

from engine.core.tca import (
    Fill,
    Side,
    TCAReport,
    aggregate_tca,
    fill_metrics,
)


def _buy_fill(**kw) -> Fill:
    base = {
        "symbol": "AAPL",
        "side": Side.BUY,
        "quantity": 100,
        "fill_price": 100.0,
        "decision_price": 99.0,
        "arrival_price": 99.5,
        "fees": 1.0,
        "broker": "alpaca",
    }
    base.update(kw)
    return Fill(**base)


def _sell_fill(**kw) -> Fill:
    base = {
        "symbol": "AAPL",
        "side": Side.SELL,
        "quantity": 100,
        "fill_price": 100.0,
        "decision_price": 101.0,
        "arrival_price": 100.5,
        "fees": 1.0,
        "broker": "alpaca",
    }
    base.update(kw)
    return Fill(**base)


class TestSinglefillMetrics:
    def test_buy_implementation_shortfall(self):
        f = _buy_fill()
        m = fill_metrics(f)
        assert m.implementation_shortfall == pytest.approx(101.0)

    def test_buy_slippage_vs_arrival(self):
        f = _buy_fill()
        m = fill_metrics(f)
        assert m.slippage_vs_arrival == pytest.approx(50.0)

    def test_sell_implementation_shortfall(self):
        f = _sell_fill()
        m = fill_metrics(f)
        assert m.implementation_shortfall == pytest.approx(101.0)

    def test_sell_slippage_vs_arrival(self):
        f = _sell_fill()
        m = fill_metrics(f)
        assert m.slippage_vs_arrival == pytest.approx(50.0)

    def test_metrics_in_bps(self):
        f = _buy_fill()
        m = fill_metrics(f)
        assert m.implementation_shortfall_bps == pytest.approx(101.0)

    def test_zero_fee_zero_slippage_zero_metrics(self):
        f = _buy_fill(fill_price=99.0, arrival_price=99.0, fees=0.0)
        m = fill_metrics(f)
        assert m.implementation_shortfall == pytest.approx(0.0)
        assert m.slippage_vs_arrival == pytest.approx(0.0)


class TestSideEnum:
    def test_side_values(self):
        assert Side.BUY.value == "buy"
        assert Side.SELL.value == "sell"


class TestAggregation:
    def test_empty_fills_zero_report(self):
        report = aggregate_tca([])
        assert isinstance(report, TCAReport)
        assert report.total_implementation_shortfall == 0.0
        assert report.total_fees == 0.0
        assert report.fill_count == 0

    def test_total_is_sum_of_fills(self):
        f1 = _buy_fill()
        f2 = _buy_fill(quantity=50, fill_price=100.0, decision_price=99.5)
        report = aggregate_tca([f1, f2])
        assert report.total_implementation_shortfall == pytest.approx(127.0)
        assert report.fill_count == 2

    def test_total_fees_aggregated(self):
        f1 = _buy_fill(fees=1.0)
        f2 = _buy_fill(fees=2.5)
        report = aggregate_tca([f1, f2])
        assert report.total_fees == pytest.approx(3.5)

    def test_per_broker_rollup(self):
        a = _buy_fill(broker="alpaca", fees=1.0)
        b = _buy_fill(broker="ibkr", fees=2.0)
        report = aggregate_tca([a, b])
        assert "alpaca" in report.by_broker
        assert "ibkr" in report.by_broker
        assert report.by_broker["alpaca"].total_fees == pytest.approx(1.0)
        assert report.by_broker["ibkr"].total_fees == pytest.approx(2.0)

    def test_per_symbol_rollup(self):
        a = _buy_fill(symbol="AAPL")
        b = _buy_fill(symbol="MSFT")
        c = _buy_fill(symbol="AAPL")
        report = aggregate_tca([a, b, c])
        assert report.by_symbol["AAPL"].fill_count == 2
        assert report.by_symbol["MSFT"].fill_count == 1

    def test_buy_and_sell_offset(self):
        b = _buy_fill(arrival_price=99.5)
        s = _sell_fill(arrival_price=100.5)
        report = aggregate_tca([b, s])
        assert report.total_slippage_vs_arrival == pytest.approx(100.0)


class TestNotional:
    def test_notional_is_quantity_times_fill_price(self):
        f = _buy_fill()
        m = fill_metrics(f)
        assert m.notional == pytest.approx(10_000.0)


class TestValidation:
    def test_negative_quantity_rejected(self):
        with pytest.raises((ValueError, TypeError)):
            Fill(
                symbol="AAPL",
                side=Side.BUY,
                quantity=-10,
                fill_price=100.0,
                decision_price=99.0,
                arrival_price=99.5,
                fees=0.0,
                broker="x",
            )

    def test_zero_fill_price_rejected(self):
        with pytest.raises((ValueError, TypeError)):
            Fill(
                symbol="AAPL",
                side=Side.BUY,
                quantity=10,
                fill_price=0.0,
                decision_price=99.0,
                arrival_price=99.5,
                fees=0.0,
                broker="x",
            )

    def test_nan_price_rejected(self):
        with pytest.raises((ValueError, TypeError)):
            Fill(
                symbol="AAPL",
                side=Side.BUY,
                quantity=10,
                fill_price=float("nan"),
                decision_price=99.0,
                arrival_price=99.5,
                fees=0.0,
                broker="x",
            )

    def test_inf_fees_rejected(self):
        with pytest.raises((ValueError, TypeError)):
            Fill(
                symbol="AAPL",
                side=Side.BUY,
                quantity=10,
                fill_price=100.0,
                decision_price=99.0,
                arrival_price=99.5,
                fees=float("inf"),
                broker="x",
            )


class TestReportShape:
    def test_report_has_average_bps(self):
        f = _buy_fill()
        report = aggregate_tca([f])
        assert report.weighted_average_is_bps == pytest.approx(101.0)
