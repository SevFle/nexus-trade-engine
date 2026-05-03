from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pandas as pd
import structlog

from engine.core.cost_model import DefaultCostModel, TaxMethod
from engine.core.execution.backtest import BacktestBackend
from engine.core.metrics import PerformanceMetrics
from engine.core.order_manager import OrderManager
from engine.core.portfolio import Portfolio
from engine.core.risk_engine import RiskEngine
from engine.core.signal import Side
from engine.core.strategy_evaluator import StrategyEvaluator
from engine.data.market_state import MarketStateBuilder, ValidationError
from engine.plugins.manifest import StrategyManifest
from engine.plugins.sandbox import StrategySandbox

if TYPE_CHECKING:
    import uuid

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
    random_seed: int | None = 42


@dataclass
class BacktestResult:
    portfolio_id: uuid.UUID | None = None
    equity_curve: list[dict[str, Any]] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    final_capital: float = 0.0
    total_return_pct: float = 0.0


class BacktestRunner:
    """Orchestrates backtest execution using proper component wiring."""

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

    async def run(self) -> BacktestResult:  # noqa: PLR0912, PLR0915
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

        start = pd.Timestamp(self.config.start_date)
        end = pd.Timestamp(self.config.end_date)
        if df.index.tz is not None:
            start = start.tz_localize(df.index.tz)
            end = end.tz_localize(df.index.tz)
        mask = (df.index >= start) & (df.index <= end)
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

        portfolio = Portfolio(
            initial_cash=self.config.initial_capital,
            tax_method=TaxMethod.FIFO,
            portfolio_id=self.config.portfolio_id,
        )

        cost_model = DefaultCostModel()
        risk_engine = RiskEngine()
        backend = BacktestBackend(random_seed=self.config.random_seed)
        await backend.connect()

        order_manager = OrderManager(
            cost_model=cost_model,
            risk_engine=risk_engine,
            portfolio=portfolio,
        )
        order_manager.set_execution_backend(backend)

        manifest = StrategyManifest(
            id=self.config.strategy_name,
            name=self.config.strategy_name,
            version="0.1.0",
        )
        sandbox = StrategySandbox(self.strategy, manifest)

        result = BacktestResult(portfolio_id=self.config.portfolio_id)

        for ts in timestamps:
            try:
                market_state = self._builder.build_for_backtest(
                    all_data,
                    ts,
                    [self.config.symbol],
                )
            except ValidationError:
                logger.debug("backtest.warmup_skip", timestamp=str(ts))
                continue
            current_price = market_state.prices.get(self.config.symbol, 0.0)

            portfolio.update_prices(market_state.prices)

            snapshot = portfolio.snapshot()

            signals = await sandbox.safe_evaluate(snapshot, market_state, cost_model)

            for signal in signals:
                if signal.side == Side.HOLD:
                    continue
                if signal.symbol != self.config.symbol:
                    continue

                sell_avg_cost = None
                if signal.side == Side.SELL:
                    pos = portfolio.positions.get(signal.symbol)
                    if pos is not None:
                        sell_avg_cost = pos.avg_cost

                order = await order_manager.process_signal(
                    signal,
                    current_price,
                )

                if order.status.value != "filled":
                    continue

                trade_record: dict[str, Any] = {
                    "timestamp": ts,
                    "symbol": order.symbol,
                    "side": order.side.value,
                    "quantity": order.fill_quantity,
                    "fill_price": order.fill_price,
                    "cost_breakdown": order.cost_breakdown,
                }

                if order.side == Side.SELL:
                    avg_cost = sell_avg_cost if sell_avg_cost is not None else 0.0
                    realized_pnl = (order.fill_price - avg_cost) * order.fill_quantity
                    costs = order.cost_breakdown or {}
                    total_costs = costs.get("total", 0.0)
                    realized_pnl -= total_costs
                    trade_record["realized_pnl"] = realized_pnl
                else:
                    trade_record["realized_pnl"] = 0.0

                result.trades.append(trade_record)

            equity_point: dict[str, Any] = {
                "timestamp": ts,
                "total_value": portfolio.total_value,
                "cash": portfolio.cash,
            }
            result.equity_curve.append(equity_point)

        await backend.disconnect()

        result.final_capital = portfolio.total_value
        if self.config.initial_capital > 0:
            result.total_return_pct = (
                (result.final_capital - self.config.initial_capital)
                / self.config.initial_capital
                * 100
            )

        metrics = PerformanceMetrics(
            equity_curve=result.equity_curve,
            trade_log=result.trades,
            initial_cash=self.config.initial_capital,
        )
        report = metrics.calculate()
        result.metrics = report.to_dict()
        try:
            evaluation = StrategyEvaluator().evaluate(report).to_dict()
            result.metrics["evaluation"] = evaluation
        except Exception:
            logger.exception("backtest.evaluation_failed")

        total_trades = len(result.trades)
        closed_trades = len([t for t in result.trades if t.get("side") == "sell"])

        logger.info(
            "backtest.complete",
            bars=len(result.equity_curve),
            total_trades=total_trades,
            closed_trades=closed_trades,
            total_return_pct=round(result.total_return_pct, 2),
            final_capital=round(result.final_capital, 2),
            realized_pnl=round(portfolio.realized_pnl, 2),
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
