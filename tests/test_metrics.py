"""
Tests for performance metrics calculation.
"""

import numpy as np
import pytest

from engine.core.metrics import (
    PerformanceMetrics,
    compute_cagr,
    compute_max_drawdown,
    compute_sharpe_ratio,
)


class TestFlatCurve:
    def test_flat_curve_no_trades(self):
        equity_curve = [{"total_value": 100000.0, "cash": 100000.0}] * 10
        metrics = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=[],
            initial_cash=100000.0,
        )
        report = metrics.calculate()

        assert report.total_return_pct == pytest.approx(0.0, abs=1e-6)
        assert report.annualized_return_pct == pytest.approx(0.0, abs=1e-6)
        assert report.sharpe_ratio == pytest.approx(0.0, abs=1e-6)
        assert report.sortino_ratio == pytest.approx(0.0, abs=1e-6)
        assert report.max_drawdown_pct == pytest.approx(0.0, abs=1e-6)
        assert report.total_trades == 0
        assert report.win_rate == pytest.approx(0.0, abs=1e-6)


class TestLinearGrowth:
    def test_1pct_daily_returns_positive_sharpe(self):
        initial = 100000.0
        equity_curve = [
            {"total_value": initial * (1.01**i), "cash": initial * (0.5 * 1.01**i)}
            for i in range(252)
        ]
        trade_log = [
            {
                "symbol": "AAPL",
                "side": "buy",
                "quantity": 100,
                "fill_price": 150.0,
                "realized_pnl": 150.0,
            }
        ]
        metrics = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=trade_log,
            initial_cash=initial,
        )
        report = metrics.calculate()

        assert report.total_return_pct > 0
        assert report.sharpe_ratio > 0
        assert report.max_drawdown_pct == pytest.approx(0.0, abs=0.01)


class TestCrashAndRecovery:
    def test_50pct_crash_then_recovery(self):
        initial = 100000.0
        values = [initial * (1 - 0.5 * i / 50) for i in range(50)]
        values += [values[-1] * (1 + 0.5 / 50 * i) for i in range(1, 51)]
        equity_curve = [{"total_value": v, "cash": v * 0.3} for v in values]

        metrics = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=[],
            initial_cash=initial,
        )
        report = metrics.calculate()

        assert report.max_drawdown_pct == pytest.approx(50.0, abs=1.0)


class TestAlternatingReturns:
    def test_alternating_1pct_returns_sharpe_near_zero(self):
        initial = 100000.0
        values = [initial]
        for i in range(100):
            if i % 2 == 0:
                values.append(values[-1] * 1.01)
            else:
                values.append(values[-1] * 0.99)
        equity_curve = [{"total_value": v, "cash": v * 0.5} for v in values]

        metrics = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=[],
            initial_cash=initial,
        )
        report = metrics.calculate()

        assert abs(report.sharpe_ratio) < 1.0


class TestAllWinningTrades:
    def test_100pct_win_rate(self):
        equity_curve = [
            {"total_value": 100000.0 + i * 1000, "cash": 50000.0 + i * 500} for i in range(10)
        ]
        trade_log = [
            {
                "symbol": "AAPL",
                "side": "buy",
                "quantity": 100,
                "fill_price": 150.0,
                "realized_pnl": 500.0,
            },
            {
                "symbol": "AAPL",
                "side": "buy",
                "quantity": 100,
                "fill_price": 155.0,
                "realized_pnl": 300.0,
            },
            {
                "symbol": "AAPL",
                "side": "buy",
                "quantity": 100,
                "fill_price": 160.0,
                "realized_pnl": 200.0,
            },
        ]
        metrics = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=trade_log,
            initial_cash=100000.0,
        )
        report = metrics.calculate()

        assert report.win_rate == pytest.approx(100.0, abs=0.01)
        assert report.avg_winner > 0
        assert report.avg_loser == pytest.approx(0.0, abs=1e-6)


class TestDrawdownCurve:
    def test_drawdown_curve_calculated(self):
        initial = 100000.0
        values = [initial * (1.1**i) for i in range(10)]
        values[5] = initial * 0.9
        equity_curve = [{"total_value": v, "cash": v * 0.5} for v in values]

        metrics = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=[],
            initial_cash=initial,
        )
        report = metrics.calculate()

        assert len(report.drawdown_curve) == len(equity_curve)
        assert max(report.drawdown_curve) > 0


class TestCosts:
    def test_total_costs_and_taxes(self):
        equity_curve = [{"total_value": 100000.0 + i * 100, "cash": 50000.0} for i in range(5)]
        trade_log = [
            {
                "symbol": "AAPL",
                "quantity": 100,
                "fill_price": 150.0,
                "realized_pnl": 100.0,
                "cost_breakdown": {"total": 25.0, "tax_estimate": 15.0},
            },
            {
                "symbol": "AAPL",
                "quantity": 50,
                "fill_price": 155.0,
                "realized_pnl": 50.0,
                "cost_breakdown": {"total": 12.5, "tax_estimate": 7.5},
            },
        ]
        metrics = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=trade_log,
            initial_cash=100000.0,
        )
        report = metrics.calculate()

        assert report.total_costs == pytest.approx(37.5, abs=0.01)
        assert report.total_taxes == pytest.approx(22.5, abs=0.01)
        assert report.cost_drag_pct > 0


class TestEdgeCases:
    def test_no_trades_empty_trade_log(self):
        equity_curve = [{"total_value": 100000.0, "cash": 100000.0}]
        metrics = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=[],
            initial_cash=100000.0,
        )
        report = metrics.calculate()

        assert report.total_trades == 0
        assert report.win_rate == 0
        assert report.profit_factor == 0
        assert report.avg_trade_pnl == 0
        assert report.best_trade == 0
        assert report.worst_trade == 0

    def test_single_day(self):
        equity_curve = [{"total_value": 105000.0, "cash": 60000.0}]
        metrics = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=[],
            initial_cash=100000.0,
        )
        report = metrics.calculate()

        assert report.annualized_return_pct == 0.0
        assert report.max_drawdown_pct == 0.0

    def test_all_losing_trades(self):
        equity_curve = [
            {"total_value": 100000.0 - i * 500, "cash": 50000.0 - i * 250} for i in range(10)
        ]
        trade_log = [
            {"symbol": "AAPL", "realized_pnl": -100.0, "quantity": 100, "fill_price": 150.0},
            {"symbol": "AAPL", "realized_pnl": -200.0, "quantity": 100, "fill_price": 150.0},
            {"symbol": "AAPL", "realized_pnl": -50.0, "quantity": 100, "fill_price": 150.0},
        ]
        metrics = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=trade_log,
            initial_cash=100000.0,
        )
        report = metrics.calculate()

        assert report.win_rate == 0.0
        assert report.profit_factor == 0.0
        assert report.avg_winner == 0.0
        assert report.avg_loser < 0

    def test_empty_equity_curve(self):
        metrics = PerformanceMetrics(
            equity_curve=[],
            trade_log=[],
            initial_cash=100000.0,
        )
        report = metrics.calculate()

        assert report.total_return_pct == 0.0
        assert report.annualized_return_pct == 0.0
        assert report.max_drawdown_pct == 0.0
        assert report.sharpe_ratio == 0.0


class TestMetricsReport:
    def test_to_dict_serializable(self):
        equity_curve = [{"total_value": 110000.0, "cash": 60000.0}]
        metrics = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=[],
            initial_cash=100000.0,
        )
        report = metrics.calculate()
        result = report.to_dict()

        assert isinstance(result, dict)
        assert "total_return_pct" in result
        assert "sharpe_ratio" in result
        assert "max_drawdown_pct" in result
        assert "total_trades" in result


class TestStandaloneFunctions:
    def test_compute_sharpe_ratio(self):
        returns = [0.01, -0.005, 0.02, 0.015, -0.01]
        result = compute_sharpe_ratio(returns, risk_free_rate=0.05)
        assert isinstance(result, float)

    def test_compute_sharpe_ratio_empty(self):
        result = compute_sharpe_ratio([])
        assert result == 0.0

    def test_compute_max_drawdown(self):
        equity = [100.0, 110.0, 90.0, 105.0, 80.0, 95.0]
        result = compute_max_drawdown(equity)
        assert result > 0

    def test_compute_max_drawdown_empty(self):
        result = compute_max_drawdown([])
        assert result == 0.0

    def test_compute_cagr(self):
        result = compute_cagr(100000.0, 200000.0, 2.0)
        assert result == pytest.approx(41.42, abs=0.1)

    def test_compute_cagr_zero_years(self):
        result = compute_cagr(100000.0, 200000.0, 0.0)
        assert result == 0.0

    def test_compute_cagr_negative_start(self):
        result = compute_cagr(-100000.0, 200000.0, 2.0)
        assert result == 0.0


class TestSortino:
    def test_sortino_with_downside_returns(self):
        returns = [0.01, -0.02, 0.03, -0.01, 0.02]
        equity_curve = [
            {"total_value": 100000.0 + sum(returns[:i]) * 100000, "cash": 50000.0}
            for i in range(len(returns) + 1)
        ]
        metrics = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=[],
            initial_cash=100000.0,
            risk_free_rate=0.05,
        )
        report = metrics.calculate()

        assert isinstance(report.sortino_ratio, float)


class TestCalmar:
    def test_calmar_ratio(self):
        equity_curve = [{"total_value": 100000.0 + i * 1000 - (i // 10) * 500} for i in range(50)]
        metrics = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=[],
            initial_cash=100000.0,
        )
        report = metrics.calculate()

        if report.max_drawdown_pct > 0:
            assert isinstance(report.calmar_ratio, float)


class TestVolatility:
    def test_volatility_calculation(self):
        np.random.seed(42)
        returns = np.random.normal(0.001, 0.02, 100).tolist()
        equity_curve = [
            {"total_value": 100000.0 * (1 + sum(returns[:i]))} for i in range(len(returns) + 1)
        ]
        metrics = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=[],
            initial_cash=100000.0,
        )
        report = metrics.calculate()

        assert report.volatility_annual_pct > 0
        assert isinstance(report.volatility_annual_pct, float)


class TestConsecutiveStreaks:
    def test_max_consecutive_wins(self):
        trade_log = [
            {"symbol": "AAPL", "realized_pnl": 100.0, "quantity": 100, "fill_price": 150.0},
            {"symbol": "AAPL", "realized_pnl": 200.0, "quantity": 100, "fill_price": 150.0},
            {"symbol": "AAPL", "realized_pnl": -50.0, "quantity": 100, "fill_price": 150.0},
            {"symbol": "AAPL", "realized_pnl": 150.0, "quantity": 100, "fill_price": 150.0},
            {"symbol": "AAPL", "realized_pnl": 100.0, "quantity": 100, "fill_price": 150.0},
        ]
        equity_curve = [{"total_value": 100000.0 + i * 1000, "cash": 50000.0} for i in range(5)]
        metrics = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=trade_log,
            initial_cash=100000.0,
        )
        report = metrics.calculate()

        assert report.max_consecutive_wins == 2  # noqa: PLR2004
        assert report.max_consecutive_losses == 1


class TestProfitFactor:
    def test_profit_factor_calculation(self):
        trade_log = [
            {"symbol": "AAPL", "realized_pnl": 100.0, "quantity": 100, "fill_price": 150.0},
            {"symbol": "AAPL", "realized_pnl": 200.0, "quantity": 100, "fill_price": 150.0},
            {"symbol": "AAPL", "realized_pnl": -50.0, "quantity": 100, "fill_price": 150.0},
            {"symbol": "AAPL", "realized_pnl": 150.0, "quantity": 100, "fill_price": 150.0},
        ]
        equity_curve = [{"total_value": 100000.0 + i * 1000, "cash": 50000.0} for i in range(4)]
        metrics = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=trade_log,
            initial_cash=100000.0,
        )
        report = metrics.calculate()

        assert report.profit_factor == pytest.approx(9.0, abs=0.1)


class TestTurnoverAndExposure:
    def test_turnover_ratio(self):
        equity_curve = [{"total_value": 100000.0, "cash": 50000.0}] * 10
        trade_log = [
            {"symbol": "AAPL", "quantity": 100, "fill_price": 150.0, "realized_pnl": 100.0},
            {"symbol": "AAPL", "quantity": 100, "fill_price": 155.0, "realized_pnl": 100.0},
        ]
        metrics = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=trade_log,
            initial_cash=100000.0,
        )
        report = metrics.calculate()

        assert report.turnover_ratio > 0

    def test_exposure_pct(self):
        equity_curve = [{"total_value": 100000.0, "cash": 30000.0}] * 5
        metrics = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=[],
            initial_cash=100000.0,
        )
        report = metrics.calculate()

        assert report.exposure_pct == pytest.approx(70.0, abs=0.1)
