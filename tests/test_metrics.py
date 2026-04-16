import json

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
            risk_free_rate=0.0,
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
        assert report.profit_factor == 0.0
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

    def test_to_dict_json_serializable(self):
        equity_curve = [{"total_value": 110000.0, "cash": 60000.0}]
        metrics = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=[],
            initial_cash=100000.0,
        )
        report = metrics.calculate()
        result = report.to_dict()
        serialized = json.dumps(result)
        assert isinstance(serialized, str)

    def test_to_dict_json_serializable_with_inf_fields(self):
        equity_curve = [
            {"total_value": 100000.0 + i * 1000, "cash": 50000.0 + i * 500} for i in range(10)
        ]
        trade_log = [
            {"symbol": "AAPL", "realized_pnl": 500.0, "quantity": 100, "fill_price": 150.0},
        ]
        metrics = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=trade_log,
            initial_cash=100000.0,
        )
        report = metrics.calculate()
        result = report.to_dict()
        serialized = json.dumps(result)
        assert isinstance(serialized, str)
        parsed = json.loads(serialized)
        for field_name in ("sortino_ratio", "calmar_ratio", "profit_factor"):
            val = parsed[field_name]
            assert val is None or isinstance(val, (int, float))


class TestStandaloneFunctions:
    def test_compute_sharpe_ratio(self):
        returns = [0.01, -0.005, 0.02, 0.015, -0.01]
        result = compute_sharpe_ratio(returns, risk_free_rate=0.05)
        assert isinstance(result, float)

    def test_compute_sharpe_ratio_empty(self):
        result = compute_sharpe_ratio([])
        assert result == 0.0

    def test_compute_sharpe_ratio_custom_trading_days(self):
        returns = [0.01, -0.005, 0.02, 0.015, -0.01]
        result_252 = compute_sharpe_ratio(returns, risk_free_rate=0.05, trading_days_per_year=252)
        result_365 = compute_sharpe_ratio(returns, risk_free_rate=0.05, trading_days_per_year=365)
        assert isinstance(result_252, float)
        assert isinstance(result_365, float)
        assert result_252 != result_365

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

    def test_sortino_all_positive_returns_returns_none(self):
        equity_curve = [{"total_value": 100000.0 * (1.01**i), "cash": 50000.0} for i in range(50)]
        metrics = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=[],
            initial_cash=100000.0,
            risk_free_rate=0.0,
        )
        report = metrics.calculate()
        assert report.sortino_ratio is None

    def test_sortino_no_downside_json_safe(self):
        equity_curve = [{"total_value": 100000.0 * (1.01**i), "cash": 50000.0} for i in range(50)]
        metrics = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=[],
            initial_cash=100000.0,
        )
        report = metrics.calculate()
        result = report.to_dict()
        serialized = json.dumps(result)
        assert isinstance(serialized, str)


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

    def test_calmar_zero_drawdown_returns_none(self):
        equity_curve = [{"total_value": 100000.0 * (1.01**i), "cash": 50000.0} for i in range(50)]
        metrics = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=[],
            initial_cash=100000.0,
        )
        report = metrics.calculate()
        assert report.calmar_ratio is None

    def test_calmar_zero_drawdown_zero_return(self):
        equity_curve = [{"total_value": 100000.0, "cash": 100000.0}] * 10
        metrics = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=[],
            initial_cash=100000.0,
        )
        report = metrics.calculate()
        assert report.calmar_ratio == 0.0


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

    def test_profit_factor_no_losses_returns_none(self):
        trade_log = [
            {"symbol": "AAPL", "realized_pnl": 100.0, "quantity": 100, "fill_price": 150.0},
            {"symbol": "AAPL", "realized_pnl": 200.0, "quantity": 100, "fill_price": 150.0},
        ]
        equity_curve = [{"total_value": 100000.0 + i * 1000, "cash": 50000.0} for i in range(2)]
        metrics = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=trade_log,
            initial_cash=100000.0,
        )
        report = metrics.calculate()
        assert report.profit_factor is None
        result = report.to_dict()
        serialized = json.dumps(result)
        assert isinstance(serialized, str)

    def test_profit_factor_no_gains_returns_zero(self):
        trade_log = [
            {"symbol": "AAPL", "realized_pnl": -100.0, "quantity": 100, "fill_price": 150.0},
        ]
        equity_curve = [{"total_value": 99000.0, "cash": 50000.0}]
        metrics = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=trade_log,
            initial_cash=100000.0,
        )
        report = metrics.calculate()
        assert report.profit_factor == 0.0


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


class TestDivByZeroGuard:
    def test_zero_equity_no_crash(self):
        values = [100000.0, 50000.0, 0.0, 50000.0, 100000.0]
        equity_curve = [{"total_value": v, "cash": v * 0.5} for v in values]
        metrics = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=[],
            initial_cash=100000.0,
        )
        report = metrics.calculate()
        assert isinstance(report.sharpe_ratio, float)
        assert len(report.drawdown_curve) == len(values)

    def test_zero_equity_daily_return_is_zero(self):
        values = [100000.0, 0.0, 50000.0]
        equity_curve = [{"total_value": v, "cash": v * 0.5} for v in values]
        metrics = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=[],
            initial_cash=100000.0,
        )
        report = metrics.calculate()
        assert isinstance(report.sharpe_ratio, float)


class TestMaxDrawdownRecovery:
    def test_recovery_period_tracked(self):
        values = [100000.0, 110000.0, 80000.0, 90000.0, 110000.0, 120000.0]
        equity_curve = [{"total_value": v, "cash": v * 0.5} for v in values]
        metrics = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=[],
            initial_cash=100000.0,
        )
        report = metrics.calculate()
        assert report.max_drawdown_recovery_days is not None
        assert report.max_drawdown_recovery_days > 0

    def test_no_drawdown_recovery_is_zero(self):
        values = [100000.0, 110000.0, 120000.0, 130000.0]
        equity_curve = [{"total_value": v, "cash": v * 0.5} for v in values]
        metrics = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=[],
            initial_cash=100000.0,
        )
        report = metrics.calculate()
        assert report.max_drawdown_recovery_days == 0

    def test_unrecovered_drawdown_returns_none(self):
        values = [100000.0, 110000.0, 80000.0, 85000.0, 90000.0]
        equity_curve = [{"total_value": v, "cash": v * 0.5} for v in values]
        metrics = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=[],
            initial_cash=100000.0,
        )
        report = metrics.calculate()
        assert report.max_drawdown_recovery_days is None


class TestAnnualizationFactor:
    def test_custom_trading_days_per_year(self):
        equity_curve = [{"total_value": 100000.0 * (1.01**i), "cash": 50000.0} for i in range(100)]
        metrics_252 = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=[],
            initial_cash=100000.0,
            trading_days_per_year=252,
        )
        metrics_365 = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=[],
            initial_cash=100000.0,
            trading_days_per_year=365,
        )
        report_252 = metrics_252.calculate()
        report_365 = metrics_365.calculate()

        assert report_252.sharpe_ratio != report_365.sharpe_ratio
        assert report_252.volatility_annual_pct != report_365.volatility_annual_pct


class TestRollingWindowMetrics:
    def test_rolling_window_calculated(self):
        np.random.seed(42)
        returns = np.random.normal(0.001, 0.02, 100).tolist()
        equity_curve = [
            {"total_value": 100000.0 * (1 + sum(returns[:i])), "cash": 50000.0}
            for i in range(len(returns) + 1)
        ]
        metrics = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=[],
            initial_cash=100000.0,
            rolling_windows=[20, 60],
        )
        report = metrics.calculate()

        assert len(report.rolling_metrics) == 2  # noqa: PLR2004
        assert report.rolling_metrics[0].window_days == 20  # noqa: PLR2004
        assert report.rolling_metrics[1].window_days == 60  # noqa: PLR2004
        for rm in report.rolling_metrics:
            assert isinstance(rm.sharpe_ratio, float)
            assert isinstance(rm.volatility_annual_pct, float)
            assert isinstance(rm.max_drawdown_pct, float)

    def test_rolling_window_skips_too_large(self):
        equity_curve = [{"total_value": 100000.0 + i * 100, "cash": 50000.0} for i in range(10)]
        metrics = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=[],
            initial_cash=100000.0,
            rolling_windows=[100],
        )
        report = metrics.calculate()
        assert len(report.rolling_metrics) == 0

    def test_rolling_window_no_windows_returns_empty(self):
        equity_curve = [{"total_value": 100000.0, "cash": 50000.0}] * 10
        metrics = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=[],
            initial_cash=100000.0,
        )
        report = metrics.calculate()
        assert report.rolling_metrics == []

    def test_rolling_window_json_serializable(self):
        np.random.seed(42)
        returns = np.random.normal(0.001, 0.02, 100).tolist()
        equity_curve = [
            {"total_value": 100000.0 * (1 + sum(returns[:i])), "cash": 50000.0}
            for i in range(len(returns) + 1)
        ]
        metrics = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=[],
            initial_cash=100000.0,
            rolling_windows=[20],
        )
        report = metrics.calculate()
        result = report.to_dict()
        serialized = json.dumps(result)
        assert isinstance(serialized, str)
        parsed = json.loads(serialized)
        assert len(parsed["rolling_metrics"]) == 1

    def test_rolling_window_all_positive_sortino_none(self):
        equity_curve = [{"total_value": 100000.0 * (1.01**i), "cash": 50000.0} for i in range(50)]
        metrics = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=[],
            initial_cash=100000.0,
            rolling_windows=[20],
            risk_free_rate=0.0,
        )
        report = metrics.calculate()
        assert len(report.rolling_metrics) == 1
        assert report.rolling_metrics[0].sortino_ratio is None


class TestJsonSerialization:
    def test_all_fields_json_safe(self):
        equity_curve = [
            {"total_value": 100000.0 + i * 1000, "cash": 50000.0 + i * 500} for i in range(50)
        ]
        trade_log = [
            {"symbol": "AAPL", "realized_pnl": 500.0, "quantity": 100, "fill_price": 150.0},
        ]
        metrics = PerformanceMetrics(
            equity_curve=equity_curve,
            trade_log=trade_log,
            initial_cash=100000.0,
        )
        report = metrics.calculate()
        result = report.to_dict()
        serialized = json.dumps(result)
        parsed = json.loads(serialized)

        for key in ("sortino_ratio", "calmar_ratio", "profit_factor"):
            val = parsed[key]
            assert val is None or isinstance(val, (int, float)), f"{key} = {val!r} not JSON-safe"
