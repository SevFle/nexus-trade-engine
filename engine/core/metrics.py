from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class CostBreakdown:
    total: float = 0.0
    tax_estimate: float = 0.0


@dataclass
class MetricsReport:
    total_return_pct: float
    annualized_return_pct: float
    sharpe_ratio: float
    sortino_ratio: float
    max_drawdown_pct: float
    max_drawdown_duration_days: int
    calmar_ratio: float
    volatility_annual_pct: float
    total_trades: int
    win_rate: float
    profit_factor: float
    avg_trade_pnl: float
    avg_winner: float
    avg_loser: float
    best_trade: float
    worst_trade: float
    max_consecutive_wins: int
    max_consecutive_losses: int
    total_costs: float
    total_taxes: float
    cost_drag_pct: float
    turnover_ratio: float
    exposure_pct: float
    equity_curve: list[dict] = field(default_factory=list)
    drawdown_curve: list[float] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_return_pct": self.total_return_pct,
            "annualized_return_pct": self.annualized_return_pct,
            "sharpe_ratio": self.sharpe_ratio,
            "sortino_ratio": self.sortino_ratio,
            "max_drawdown_pct": self.max_drawdown_pct,
            "max_drawdown_duration_days": self.max_drawdown_duration_days,
            "calmar_ratio": self.calmar_ratio,
            "volatility_annual_pct": self.volatility_annual_pct,
            "total_trades": self.total_trades,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "avg_trade_pnl": self.avg_trade_pnl,
            "avg_winner": self.avg_winner,
            "avg_loser": self.avg_loser,
            "best_trade": self.best_trade,
            "worst_trade": self.worst_trade,
            "max_consecutive_wins": self.max_consecutive_wins,
            "max_consecutive_losses": self.max_consecutive_losses,
            "total_costs": self.total_costs,
            "total_taxes": self.total_taxes,
            "cost_drag_pct": self.cost_drag_pct,
            "turnover_ratio": self.turnover_ratio,
            "exposure_pct": self.exposure_pct,
        }


class PerformanceMetrics:
    def __init__(
        self,
        equity_curve: list[dict],
        trade_log: list[dict],
        initial_cash: float,
        risk_free_rate: float = 0.05,
        trading_days_per_year: int = 252,
    ):
        self.equity_curve = equity_curve
        self.trade_log = trade_log
        self.initial_cash = initial_cash
        self.risk_free_rate = risk_free_rate
        self.trading_days_per_year = trading_days_per_year

    def calculate(self) -> MetricsReport:
        values = [point["total_value"] for point in self.equity_curve]
        n_days = len(values)

        final_value = values[-1] if values else self.initial_cash

        daily_returns = self._calculate_daily_returns(values)
        drawdown_curve = self._calculate_drawdown_curve(values)

        trade_pnls = [t.get("realized_pnl", 0.0) for t in self.trade_log]

        total_return_pct = self._total_return_pct(final_value)
        annualized_return_pct = self._annualized_return_pct(final_value, n_days)
        sharpe_ratio = self._sharpe_ratio(daily_returns)
        sortino_ratio = self._sortino_ratio(daily_returns)
        max_drawdown_pct = self._max_drawdown_pct(drawdown_curve)
        max_dd_duration = self._max_drawdown_duration(values)
        calmar_ratio = self._calmar_ratio(annualized_return_pct, max_drawdown_pct)
        volatility_annual_pct = self._volatility_annual_pct(daily_returns)

        total_trades = len(self.trade_log)
        win_rate = self._win_rate(trade_pnls)
        profit_factor = self._profit_factor(trade_pnls)
        avg_trade_pnl = self._mean_or_zero(trade_pnls)
        avg_winner = self._avg_winner(trade_pnls)
        avg_loser = self._avg_loser(trade_pnls)
        best_trade = max(trade_pnls) if trade_pnls else 0.0
        worst_trade = min(trade_pnls) if trade_pnls else 0.0
        max_consecutive_wins = self._max_consecutive(trade_pnls, positive=True)
        max_consecutive_losses = self._max_consecutive(trade_pnls, positive=False)

        total_costs = self._total_costs()
        total_taxes = self._total_taxes()
        cost_drag_pct = (total_costs / self.initial_cash) * 100 if self.initial_cash else 0.0
        turnover_ratio = self._turnover_ratio()
        exposure_pct = self._exposure_pct()

        return MetricsReport(
            total_return_pct=total_return_pct,
            annualized_return_pct=annualized_return_pct,
            sharpe_ratio=sharpe_ratio,
            sortino_ratio=sortino_ratio,
            max_drawdown_pct=max_drawdown_pct,
            max_drawdown_duration_days=max_dd_duration,
            calmar_ratio=calmar_ratio,
            volatility_annual_pct=volatility_annual_pct,
            total_trades=total_trades,
            win_rate=win_rate,
            profit_factor=profit_factor,
            avg_trade_pnl=avg_trade_pnl,
            avg_winner=avg_winner,
            avg_loser=avg_loser,
            best_trade=best_trade,
            worst_trade=worst_trade,
            max_consecutive_wins=max_consecutive_wins,
            max_consecutive_losses=max_consecutive_losses,
            total_costs=total_costs,
            total_taxes=total_taxes,
            cost_drag_pct=cost_drag_pct,
            turnover_ratio=turnover_ratio,
            exposure_pct=exposure_pct,
            equity_curve=self.equity_curve,
            drawdown_curve=drawdown_curve,
        )

    def _calculate_daily_returns(self, values: list[float]) -> list[float]:
        if len(values) < 2:  # noqa: PLR2004
            return []
        return [(values[i] - values[i - 1]) / values[i - 1] for i in range(1, len(values))]

    def _calculate_drawdown_curve(self, values: list[float]) -> list[float]:
        if not values:
            return []
        drawdowns = []
        peak = values[0]
        for v in values:
            peak = max(peak, v)
            dd = (peak - v) / peak if peak > 0 else 0.0
            drawdowns.append(dd)
        return drawdowns

    def _total_return_pct(self, final_value: float) -> float:
        return (
            ((final_value - self.initial_cash) / self.initial_cash) * 100
            if self.initial_cash
            else 0.0
        )

    def _annualized_return_pct(self, final_value: float, n_days: int) -> float:
        if n_days <= 1 or self.initial_cash <= 0:
            return 0.0
        years = n_days / self.trading_days_per_year
        return ((final_value / self.initial_cash) ** (1 / years) - 1) * 100 if years > 0 else 0.0

    def _sharpe_ratio(self, daily_returns: list[float]) -> float:
        if not daily_returns:
            return 0.0
        rf_daily = self.risk_free_rate / self.trading_days_per_year
        mean_ret = np.mean(daily_returns)
        std_ret = np.std(daily_returns, ddof=0)
        if std_ret == 0:
            return 0.0
        return (mean_ret - rf_daily) / std_ret * np.sqrt(self.trading_days_per_year)

    def _sortino_ratio(self, daily_returns: list[float]) -> float:
        if not daily_returns:
            return 0.0
        rf_daily = self.risk_free_rate / self.trading_days_per_year
        mean_ret = np.mean(daily_returns)
        downside_returns = [r for r in daily_returns if r < 0]
        if not downside_returns:
            return float("inf") if mean_ret > rf_daily else 0.0
        downside_std = np.std(downside_returns, ddof=0)
        if downside_std == 0:
            return 0.0
        return (mean_ret - rf_daily) / downside_std * np.sqrt(self.trading_days_per_year)

    def _max_drawdown_pct(self, drawdown_curve: list[float]) -> float:
        return max(drawdown_curve) * 100 if drawdown_curve else 0.0

    def _max_drawdown_duration(self, values: list[float]) -> int:
        if not values:
            return 0
        peak = values[0]
        peak_idx = 0
        max_duration = 0
        current_duration = 0
        in_drawdown = False
        drawdown_start = 0

        for i, v in enumerate(values):
            if v >= peak:
                if in_drawdown:
                    current_duration = i - drawdown_start
                    max_duration = max(max_duration, current_duration)
                    in_drawdown = False
                peak = v
                peak_idx = i
            elif not in_drawdown:
                drawdown_start = peak_idx
                in_drawdown = True
        if in_drawdown:
            current_duration = len(values) - drawdown_start
            max_duration = max(max_duration, current_duration)
        return max_duration

    def _calmar_ratio(self, annualized_return_pct: float, max_drawdown_pct: float) -> float:
        if max_drawdown_pct == 0:
            return float("inf") if annualized_return_pct > 0 else 0.0
        return annualized_return_pct / max_drawdown_pct

    def _volatility_annual_pct(self, daily_returns: list[float]) -> float:
        if not daily_returns:
            return 0.0
        return np.std(daily_returns, ddof=0) * np.sqrt(self.trading_days_per_year) * 100

    def _win_rate(self, trade_pnls: list[float]) -> float:
        if not trade_pnls:
            return 0.0
        winning = sum(1 for p in trade_pnls if p > 0)
        return (winning / len(trade_pnls)) * 100

    def _profit_factor(self, trade_pnls: list[float]) -> float:
        gains = sum(p for p in trade_pnls if p > 0)
        losses = abs(sum(p for p in trade_pnls if p < 0))
        if losses == 0:
            return float("inf") if gains > 0 else 0.0
        return gains / losses

    def _mean_or_zero(self, values: list[float]) -> float:
        return float(np.mean(values)) if values else 0.0

    def _avg_winner(self, trade_pnls: list[float]) -> float:
        winners = [p for p in trade_pnls if p > 0]
        return float(np.mean(winners)) if winners else 0.0

    def _avg_loser(self, trade_pnls: list[float]) -> float:
        losers = [p for p in trade_pnls if p < 0]
        return float(np.mean(losers)) if losers else 0.0

    def _max_consecutive(self, trade_pnls: list[float], positive: bool) -> int:
        if not trade_pnls:
            return 0
        max_streak = 0
        current_streak = 0
        for p in trade_pnls:
            if (positive and p > 0) or (not positive and p < 0):
                current_streak += 1
                max_streak = max(max_streak, current_streak)
            else:
                current_streak = 0
        return max_streak

    def _total_costs(self) -> float:
        total = 0.0
        for trade in self.trade_log:
            cost_breakdown = trade.get("cost_breakdown")
            if cost_breakdown:
                if isinstance(cost_breakdown, dict):
                    total += cost_breakdown.get("total", 0.0)
                elif hasattr(cost_breakdown, "total"):
                    total += cost_breakdown.total
        return total

    def _total_taxes(self) -> float:
        total = 0.0
        for trade in self.trade_log:
            cost_breakdown = trade.get("cost_breakdown")
            if cost_breakdown:
                if isinstance(cost_breakdown, dict):
                    total += cost_breakdown.get("tax_estimate", 0.0)
                elif hasattr(cost_breakdown, "tax_estimate"):
                    total += cost_breakdown.tax_estimate
        return total

    def _turnover_ratio(self) -> float:
        if not self.equity_curve:
            return 0.0
        trade_values = []
        for trade in self.trade_log:
            qty = trade.get("quantity", 0)
            price = trade.get("fill_price", 0)
            trade_values.append(abs(qty * price))
        if not trade_values:
            return 0.0
        total_traded = sum(trade_values)
        avg_portfolio_value = float(np.mean([p["total_value"] for p in self.equity_curve]))
        if avg_portfolio_value == 0:
            return 0.0
        return total_traded / avg_portfolio_value

    def _exposure_pct(self) -> float:
        if not self.equity_curve:
            return 0.0
        exposures = []
        for point in self.equity_curve:
            total_value = point.get("total_value", 0)
            cash = point.get("cash", total_value)
            invested = total_value - cash
            if total_value > 0:
                exposures.append(invested / total_value)
        return float(np.mean(exposures)) * 100 if exposures else 0.0


def compute_sharpe_ratio(returns: list[float], risk_free_rate: float = 0.0) -> float:
    if not returns:
        return 0.0
    trading_days = 252
    rf_daily = risk_free_rate / trading_days
    mean_ret = np.mean(returns)
    std_ret = np.std(returns, ddof=0)
    if std_ret == 0:
        return 0.0
    return (mean_ret - rf_daily) / std_ret * np.sqrt(trading_days)


def compute_max_drawdown(equity_curve: list[float]) -> float:
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for v in equity_curve:
        peak = max(peak, v)
        dd = (peak - v) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
    return max_dd * 100


def compute_cagr(start_value: float, end_value: float, years: float) -> float:
    if start_value <= 0 or years <= 0:
        return 0.0
    return ((end_value / start_value) ** (1 / years) - 1) * 100
