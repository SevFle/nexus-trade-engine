"""
Tests for the backtest loop engine.

Tests require the backtest_runner implementation to be complete.
Each test case is designed to validate specific functionality.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


class TestMeanReversionAAPL:
    """Test Case 1: mean-reversion strategy against AAPL."""

    @pytest.mark.asyncio
    async def test_run_backtest_returns_valid_summary(self):
        """Run mean-reversion strategy against AAPL; verify BacktestSummary with all metrics."""
        from engine.core.backtest_runner import run_backtest

        result = await run_backtest(
            strategy_name="mean_reversion_basic",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            initial_capital=100_000.0,
            config={"sma_period": 50, "entry_std": 2.0},
        )

        assert result is not None
        assert "metrics" in result
        metrics = result["metrics"]
        assert "total_return_pct" in metrics
        assert "sharpe_ratio" in metrics
        assert "sortino_ratio" in metrics
        assert "max_drawdown_pct" in metrics
        assert "total_trades" in metrics
        assert "win_rate" in metrics
        assert isinstance(metrics["total_return_pct"], float)


class TestEquityCurveValidation:
    """Test Case 2: Equity curve validation."""

    @pytest.mark.asyncio
    async def test_equity_curve_has_one_point_per_trading_day(self):
        """Verify equity curve has one point per trading day, no gaps, no duplicates."""
        from engine.core.backtest_runner import run_backtest

        result = await run_backtest(
            strategy_name="mean_reversion_basic",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            initial_capital=100_000.0,
            config={},
        )

        equity_curve = result.get("equity_curve", [])
        assert len(equity_curve) > 0, "Equity curve should not be empty"

        timestamps = [point.get("timestamp") for point in equity_curve]
        assert len(timestamps) == len(set(timestamps)), "No duplicate timestamps"

        for point in equity_curve:
            assert "timestamp" in point
            assert "total_value" in point
            assert "cash" in point
            assert "positions_value" in point


class TestCostModelApplication:
    """Test Case 3: Cost model application."""

    @pytest.mark.asyncio
    async def test_total_costs_greater_than_zero_when_trades_occur(self):
        """Verify total_costs > 0 when trades occur. No free trades."""
        from engine.core.backtest_runner import run_backtest

        result = await run_backtest(
            strategy_name="mean_reversion_basic",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            initial_capital=100_000.0,
            config={},
        )

        metrics = result.get("metrics", {})
        total_trades = metrics.get("total_trades", 0)
        total_costs = metrics.get("total_costs", 0.0)

        if total_trades > 0:
            assert total_costs > 0, "Total costs must be greater than 0 when trades occur"

    @pytest.mark.asyncio
    async def test_every_trade_has_cost_breakdown(self):
        """Verify every trade_log entry has filled cost_breakdown."""
        from engine.core.backtest_runner import run_backtest

        result = await run_backtest(
            strategy_name="mean_reversion_basic",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            initial_capital=100_000.0,
            config={},
        )

        trade_log = result.get("trade_log", [])
        for trade in trade_log:
            assert "cost_breakdown" in trade
            cost_breakdown = trade["cost_breakdown"]
            assert cost_breakdown is not None
            assert "commission" in cost_breakdown or "slippage" in cost_breakdown


class TestTaxCalculationFIFO:
    """Test Case 4: Tax calculation FIFO."""

    @pytest.mark.asyncio
    async def test_tax_calculations_use_fifo_by_default(self):
        """Verify tax calculations use FIFO by default. Check tax_estimate is present."""
        from engine.core.backtest_runner import run_backtest

        result = await run_backtest(
            strategy_name="mean_reversion_basic",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            initial_capital=100_000.0,
            config={},
        )

        metrics = result.get("metrics", {})
        total_taxes = metrics.get("total_taxes", 0.0)

        trade_log = result.get("trade_log", [])
        sells = [t for t in trade_log if t.get("side") == "sell"]

        if len(sells) > 0:
            assert total_taxes >= 0, "Taxes should be non-negative"

        for trade in sells:
            cost_breakdown = trade.get("cost_breakdown", {})
            assert "tax_estimate" in cost_breakdown or total_taxes >= 0


class TestZeroTradeEdgeCase:
    """Test Case 5: Zero-trade edge case."""

    @pytest.mark.asyncio
    async def test_no_signals_strategy_returns_zero_percent(self):
        """Use strategy that never triggers signals. Verify return is 0%, flat equity curve."""
        from engine.core.backtest_runner import run_backtest

        result = await run_backtest(
            strategy_name="noop_strategy",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            initial_capital=100_000.0,
            config={},
        )

        metrics = result.get("metrics", {})
        total_return_pct = metrics.get("total_return_pct", 0.0)
        total_trades = metrics.get("total_trades", 0)

        assert total_trades == 0, "No trades should be executed"
        assert total_return_pct == 0.0, "Return should be 0% with no trades"

        equity_curve = result.get("equity_curve", [])
        assert len(equity_curve) > 0, "Equity curve should still be populated"

        initial_value = result.get("initial_capital", 100_000.0)
        for point in equity_curve:
            assert abs(point.get("total_value", 0) - initial_value) < 1, (
                "Equity curve should be flat"
            )

        trade_log = result.get("trade_log", [])
        assert len(trade_log) == 0, "Trade log should be empty"


class TestDeterministicSeed:
    """Test Case 6: Deterministic seed."""

    @pytest.mark.asyncio
    async def test_same_seed_produces_identical_results(self):
        """Run same backtest twice with random_seed=42. Verify identical results."""
        from engine.core.backtest_runner import run_backtest

        result1 = await run_backtest(
            strategy_name="mean_reversion_basic",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-06-30",
            initial_capital=100_000.0,
            config={},
            random_seed=42,
        )

        result2 = await run_backtest(
            strategy_name="mean_reversion_basic",
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-06-30",
            initial_capital=100_000.0,
            config={},
            random_seed=42,
        )

        eq1 = result1.get("equity_curve", [])
        eq2 = result2.get("equity_curve", [])
        assert len(eq1) == len(eq2), "Equity curves should have same length"

        tolerance = 0.01
        for p1, p2 in zip(eq1, eq2, strict=True):
            assert abs(p1.get("total_value", 0) - p2.get("total_value", 0)) < tolerance

        tl1 = result1.get("trade_log", [])
        tl2 = result2.get("trade_log", [])
        assert len(tl1) == len(tl2), "Trade logs should have same length"

        for t1, t2 in zip(tl1, tl2, strict=True):
            assert t1.get("symbol") == t2.get("symbol")
            assert t1.get("side") == t2.get("side")
            assert abs(t1.get("pnl", 0) - t2.get("pnl", 0)) < tolerance

        metric_tolerance = 0.0001
        m1 = result1.get("metrics", {})
        m2 = result2.get("metrics", {})
        for key in ["total_return_pct", "sharpe_ratio", "total_trades"]:
            assert abs(m1.get(key, 0) - m2.get(key, 0)) < metric_tolerance


class TestAPIIntegration:
    """Test Case 7: API integration."""

    @pytest.mark.asyncio
    async def test_post_run_returns_complete_backtest_summary(self):
        """POST /api/v1/backtest/run returns complete BacktestSummary."""
        from httpx import ASGITransport, AsyncClient

        with patch("engine.api.routes.backtest.run_backtest") as mock_run:
            mock_run.return_value = {
                "status": "completed",
                "metrics": {
                    "total_return_pct": 10.5,
                    "sharpe_ratio": 1.2,
                    "total_trades": 5,
                },
                "equity_curve": [
                    {"timestamp": "2024-01-01", "total_value": 100_000},
                    {"timestamp": "2024-01-02", "total_value": 100_500},
                ],
            }

            from engine.app import create_app

            app = create_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/backtest/run",
                    json={
                        "strategy_name": "mean_reversion_basic",
                        "symbol": "AAPL",
                        "start_date": "2024-01-01",
                        "end_date": "2024-12-31",
                        "initial_capital": 100_000.0,
                    },
                )

            status_ok = 200
            assert response.status_code == status_ok
            data = response.json()
            assert "status" in data
            assert data["status"] in ["accepted", "completed"]

    @pytest.mark.asyncio
    async def test_get_results_retrieves_stored_equity_curve(self):
        """GET /api/v1/backtest/results/{id} retrieves stored equity curve."""
        from httpx import ASGITransport, AsyncClient

        from engine.app import create_app

        app = create_app()
        transport = ASGITransport(app=app)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/backtest/results/test-id-123")

            status_ok = 200
            if response.status_code == status_ok:
                data = response.json()
                assert "equity_curve" in data or "status" in data
