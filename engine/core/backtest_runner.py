from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pandas as pd
import structlog

from engine.core.portfolio import Portfolio
from engine.data.market_state import MarketStateBuilder

if TYPE_CHECKING:
    import uuid

    from engine.core.metrics import PerformanceMetrics
    from engine.data.feeds import MarketDataProvider
    from engine.plugins.sdk import BaseStrategy

logger = structlog.get_logger()


@dataclass
class BacktestConfig:
    strategy_name: str
    symbol: str
    start_date: str
    end_date: str
    portfolio_id: uuid.UUID | None = None
    initial_capital: float = 100_000.0
    min_bars: int = 50
    debug: bool = False


@dataclass
class BacktestResult:
    portfolio_id: uuid.UUID | None = None
    equity_curve: list[dict[str, Any]] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    final_capital: float = 0.0
    total_return_pct: float = 0.0


class BacktestRunner:
    """Orchestrates backtest execution using MarketStateBuilder."""

    def __init__(
        self,
        config: BacktestConfig,
        strategy: BaseStrategy | None = None,
        provider: MarketDataProvider | None = None,
    ) -> None:
        self.config = config
        self.strategy = strategy
        self.provider = provider
        self._builder = MarketStateBuilder(min_bars=config.min_bars, debug=config.debug)

    async def run(self) -> BacktestResult:
        if self.provider is None:
            raise RuntimeError("No data provider configured")
        if self.strategy is None:
            raise RuntimeError("No strategy configured")

        df = await self.provider.get_ohlcv(
            self.config.symbol,
            period="max",
            interval="1d",
        )
        if df.empty:
            raise RuntimeError(f"No OHLCV data returned for {self.config.symbol}")

        mask = (df.index >= pd.Timestamp(self.config.start_date)) & (
            df.index <= pd.Timestamp(self.config.end_date)
        )
        df = df.loc[mask]
        if df.empty:
            raise RuntimeError(
                f"No data in range {self.config.start_date} to {self.config.end_date}"
            )

        all_data = {self.config.symbol: df}
        timestamps = df.index.tolist()

        logger.info(
            "backtest.start",
            symbol=self.config.symbol,
            bars=len(timestamps),
            start=str(timestamps[0]),
            end=str(timestamps[-1]),
        )

        result = BacktestResult(portfolio_id=self.config.portfolio_id)

        for ts in timestamps:
            market_state = self._builder.build_for_backtest(
                all_data,
                ts,
                [self.config.symbol],
            )
            sdk_state = market_state.to_sdk_state()

            portfolio = Portfolio(initial_cash=self.config.initial_capital)
            signals = self.strategy.on_bar(sdk_state, portfolio)
            result.trades.extend(signals)

            result.equity_curve.append(
                {
                    "timestamp": ts,
                    "price": market_state.prices.get(self.config.symbol, 0.0),
                }
            )

        if result.equity_curve:
            result.final_capital = result.equity_curve[-1].get("price", 0.0) * 100
            first_price = result.equity_curve[0].get("price", 0.0)
            if first_price > 0:
                result.total_return_pct = (
                    (result.equity_curve[-1].get("price", 0.0) - first_price) / first_price * 100
                )

        logger.info(
            "backtest.complete",
            bars=len(result.equity_curve),
            trades=len(result.trades),
            total_return_pct=round(result.total_return_pct, 2),
        )

        return result


@dataclass
class BacktestSummary:
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
            max_drawdown_recovery_days=report.max_drawdown_recovery_days,
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
