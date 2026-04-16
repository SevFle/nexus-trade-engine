from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class CostBreakdown:
    total: float = 0.0
    tax_estimate: float = 0.0


@dataclass
class RollingWindowMetrics:
    window_days: int
    sharpe_ratio: float
    sortino_ratio: float | None
    volatility_annual_pct: float
    max_drawdown_pct: float


@dataclass
class MetricsReport:
    total_return_pct: float
    annualized_return_pct: float
    sharpe_ratio: float
    sortino_ratio: float | None
    max_drawdown_pct: float
    max_drawdown_duration_days: int
    max_drawdown_recovery_days: int | None
    calmar_ratio: float | None
    volatility_annual_pct: float
    total_trades: int
    win_rate: float
    profit_factor: float | None
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
    rolling_metrics: list[RollingWindowMetrics] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_return_pct": self.total_return_pct,
            "annualized_return_pct": self.annualized_return_pct,
            "sharpe_ratio": self.sharpe_ratio,
            "sortino_ratio": self.sortino_ratio,
            "max_drawdown_pct": self.max_drawdown_pct,
            "max_drawdown_duration_days": self.max_drawdown_duration_days,
            "max_drawdown_recovery_days": self.max_drawdown_recovery_days,
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
            "rolling_metrics": [
                {
                    "window_days": rm.window_days,
                    "sharpe_ratio": rm.sharpe_ratio,
                    "sortino_ratio": rm.sortino_ratio,
                    "volatility_annual_pct": rm.volatility_annual_pct,
                    "max_drawdown_pct": rm.max_drawdown_pct,
                }
                for rm in self.rolling_metrics
            ],
        }


class PerformanceMetrics:
    def __init__(
        self,
        equity_curve: list[dict],
        trade_log: list[dict],
        initial_cash: float,
        risk_free_rate: float = 0.05,
        trading_days_per_year: int = 252,
        rolling_windows: list[int] | None = None,
    ):
        self.equity_curve = equity_curve
        self.trade_log = trade_log
        self.initial_cash = initial_cash
        self.risk_free_rate = risk_free_rate
        self.trading_days_per_year = trading_days_per_year
        self.rolling_windows = rolling_windows or []

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
        max_dd_recovery = self._max_drawdown_recovery(values, drawdown_curve)
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

        rolling_metrics = self._rolling_window_metrics(daily_returns, values)

        return MetricsReport(
            total_return_pct=total_return_pct,
            annualized_return_pct=annualized_return_pct,
            sharpe_ratio=sharpe_ratio,
            sortino_ratio=sortino_ratio,
            max_drawdown_pct=max_drawdown_pct,
            max_drawdown_duration_days=max_dd_duration,
            max_drawdown_recovery_days=max_dd_recovery,
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
            rolling_metrics=rolling_metrics,
        )

    def _calculate_daily_returns(self, values: list[float]) -> list[float]:
        if len(values) < 2:  # noqa: PLR2004
            return []
        returns = []
        for i in range(1, len(values)):
            prev = values[i - 1]
            if prev == 0:
                returns.append(0.0)
            else:
                returns.append((values[i] - prev) / prev)
        return returns

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
        mean_ret = float(np.mean(daily_returns))
        std_ret = float(np.std(daily_returns, ddof=1))
        if std_ret == 0:
            return 0.0
        return (mean_ret - rf_daily) / std_ret * float(np.sqrt(self.trading_days_per_year))

    def _sortino_ratio(self, daily_returns: list[float]) -> float | None:
        if not daily_returns:
            return 0.0
        rf_daily = self.risk_free_rate / self.trading_days_per_year
        mean_ret = float(np.mean(daily_returns))
        downside_diff_sq = [(min(r - rf_daily, 0.0)) ** 2 for r in daily_returns]
        downside_dev = float(np.sqrt(np.mean(downside_diff_sq)))
        if downside_dev == 0:
            return None if mean_ret > rf_daily else 0.0
        return (mean_ret - rf_daily) / downside_dev * float(np.sqrt(self.trading_days_per_year))

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

    def _max_drawdown_recovery(
        self, values: list[float], drawdown_curve: list[float]
    ) -> int | None:
        if not values or not drawdown_curve:
            return None

        max_dd_idx = int(np.argmax(drawdown_curve))
        max_dd = drawdown_curve[max_dd_idx]
        if max_dd == 0:
            return 0

        pre_dd_peak = values[0]
        for i in range(max_dd_idx + 1):
            pre_dd_peak = max(pre_dd_peak, values[i])

        for i in range(max_dd_idx + 1, len(values)):
            if values[i] >= pre_dd_peak:
                return i - max_dd_idx

        return None

    def _calmar_ratio(self, annualized_return_pct: float, max_drawdown_pct: float) -> float | None:
        if max_drawdown_pct == 0:
            return None if annualized_return_pct > 0 else 0.0
        return annualized_return_pct / max_drawdown_pct

    def _volatility_annual_pct(self, daily_returns: list[float]) -> float:
        if not daily_returns:
            return 0.0
        return (
            float(np.std(daily_returns, ddof=1)) * float(np.sqrt(self.trading_days_per_year)) * 100
        )

    def _win_rate(self, trade_pnls: list[float]) -> float:
        if not trade_pnls:
            return 0.0
        winning = sum(1 for p in trade_pnls if p > 0)
        return (winning / len(trade_pnls)) * 100

    def _profit_factor(self, trade_pnls: list[float]) -> float | None:
        gains = sum(p for p in trade_pnls if p > 0)
        losses = abs(sum(p for p in trade_pnls if p < 0))
        if losses == 0:
            return None if gains > 0 else 0.0
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

    def _rolling_window_metrics(
        self, daily_returns: list[float], values: list[float]
    ) -> list[RollingWindowMetrics]:
        if not self.rolling_windows or not daily_returns:
            return []
        results = []
        for window in self.rolling_windows:
            if window > len(daily_returns):
                continue
            window_returns = daily_returns[-window:]
            window_values = values[-(window + 1) :] if len(values) >= window + 1 else values

            rf_daily = self.risk_free_rate / self.trading_days_per_year
            mean_ret = float(np.mean(window_returns))
            std_ret = float(np.std(window_returns, ddof=1))
            sharpe = (
                (mean_ret - rf_daily) / std_ret * float(np.sqrt(self.trading_days_per_year))
                if std_ret != 0
                else 0.0
            )

            downside_diff_sq = [(min(r - rf_daily, 0.0)) ** 2 for r in window_returns]
            downside_dev = float(np.sqrt(np.mean(downside_diff_sq)))
            sortino = (
                (mean_ret - rf_daily) / downside_dev * float(np.sqrt(self.trading_days_per_year))
                if downside_dev != 0
                else (None if mean_ret > rf_daily else 0.0)
            )

            vol = (
                float(np.std(window_returns, ddof=1))
                * float(np.sqrt(self.trading_days_per_year))
                * 100
            )

            dd_curve = self._calculate_drawdown_curve(window_values)
            max_dd = max(dd_curve) * 100 if dd_curve else 0.0

            results.append(
                RollingWindowMetrics(
                    window_days=window,
                    sharpe_ratio=sharpe,
                    sortino_ratio=sortino,
                    volatility_annual_pct=vol,
                    max_drawdown_pct=max_dd,
                )
            )
        return results


def compute_sharpe_ratio(
    returns: list[float],
    risk_free_rate: float = 0.0,
    trading_days_per_year: int = 252,
) -> float:
    if not returns:
        return 0.0
    rf_daily = risk_free_rate / trading_days_per_year
    mean_ret = float(np.mean(returns))
    std_ret = float(np.std(returns, ddof=1))
    if std_ret == 0:
        return 0.0
    return (mean_ret - rf_daily) / std_ret * float(np.sqrt(trading_days_per_year))


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
