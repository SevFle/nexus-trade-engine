from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.core.metrics import PerformanceMetrics


@dataclass
class BacktestConfig:
    strategy_name: str
    symbol: str
    start_date: str
    end_date: str
    initial_capital: float = 100_000.0


class BacktestRunner:
    def __init__(self, config: BacktestConfig) -> None:
        self.config = config

    async def run(self) -> dict:
        raise NotImplementedError


@dataclass
class BacktestSummary:
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

    @classmethod
    def from_metrics(cls, metrics: PerformanceMetrics) -> BacktestSummary:
        report = metrics.calculate()
        return cls(
            total_return_pct=report.total_return_pct,
            annualized_return_pct=report.annualized_return_pct,
            sharpe_ratio=report.sharpe_ratio,
            sortino_ratio=report.sortino_ratio,
            max_drawdown_pct=report.max_drawdown_pct,
            max_drawdown_duration_days=report.max_drawdown_duration_days,
            calmar_ratio=report.calmar_ratio,
            volatility_annual_pct=report.volatility_annual_pct,
            total_trades=report.total_trades,
            win_rate=report.win_rate,
            profit_factor=report.profit_factor,
            avg_trade_pnl=report.avg_trade_pnl,
            avg_winner=report.avg_winner,
            avg_loser=report.avg_loser,
            best_trade=report.best_trade,
            worst_trade=report.worst_trade,
            max_consecutive_wins=report.max_consecutive_wins,
            max_consecutive_losses=report.max_consecutive_losses,
            total_costs=report.total_costs,
            total_taxes=report.total_taxes,
            cost_drag_pct=report.cost_drag_pct,
            turnover_ratio=report.turnover_ratio,
            exposure_pct=report.exposure_pct,
        )
