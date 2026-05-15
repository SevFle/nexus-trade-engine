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

_SENTINEL = object()


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
    symbols: list[str] | None = None
    strategy_params: dict[str, Any] = field(default_factory=dict)
    cost_config: dict[str, Any] = field(default_factory=dict)
    interval: str = "1d"


@dataclass
class BacktestResult:
    portfolio_id: uuid.UUID | None = None
    equity_curve: list[dict[str, Any]] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    final_capital: float = 0.0
    total_return_pct: float = 0.0


def build_timeline(
    data: dict[str, pd.DataFrame],
) -> list[tuple[pd.Timestamp, dict[str, dict[str, Any]]]]:
    """Build sorted timeline of ``(timestamp, {symbol: bar_dict})`` tuples.

    Takes raw DataFrames from the data provider and produces a sorted list
    of all unique timestamps.  Symbols missing at a given timestamp are
    simply absent from that entry (no forward-fill).
    """
    all_timestamps: set[pd.Timestamp] = set()
    for df in data.values():
        all_timestamps.update(df.index.tolist())

    timeline: list[tuple[pd.Timestamp, dict[str, dict[str, Any]]]] = []
    for ts in sorted(all_timestamps):
        bars_at_t: dict[str, dict[str, Any]] = {}
        for symbol, df in data.items():
            if ts not in df.index:
                continue
            row = df.loc[ts]
            bars_at_t[symbol] = {
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": int(row["volume"]),
            }
        if bars_at_t:
            timeline.append((ts, bars_at_t))

    return timeline


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

        symbols = self.config.symbols or [self.config.symbol]
        active_symbols: list[str] = []

        all_data: dict[str, pd.DataFrame] = {}
        for sym in symbols:
            df = await self.provider.get_ohlcv(
                sym,
                period="max",
                interval=self.config.interval,
            )
            if df.empty:
                if len(symbols) == 1:
                    raise RuntimeError(f"No OHLCV data returned for {sym}")
                logger.warning("backtest.no_data_for_symbol", symbol=sym)
                continue

            start = pd.Timestamp(self.config.start_date)
            end = pd.Timestamp(self.config.end_date)
            if df.index.tz is not None:
                start = start.tz_localize(df.index.tz)
                end = end.tz_localize(df.index.tz)
            mask = (df.index >= start) & (df.index <= end)
            df = df.loc[mask]
            if df.empty:
                if len(symbols) == 1:
                    raise RuntimeError(
                        f"No data in range {self.config.start_date} to {self.config.end_date}"
                    )
                logger.warning("backtest.no_data_in_range", symbol=sym)
                continue

            all_data[sym] = df
            active_symbols.append(sym)

        if not all_data:
            raise RuntimeError("No OHLCV data for any symbol")

        timeline = build_timeline(all_data)

        logger.info(
            "backtest.start",
            symbols=active_symbols,
            bars=len(timeline),
            start=str(timeline[0][0]) if timeline else "",
            end=str(timeline[-1][0]) if timeline else "",
        )

        self._apply_strategy_params()

        portfolio = Portfolio(
            initial_cash=self.config.initial_capital,
            tax_method=TaxMethod.FIFO,
            portfolio_id=self.config.portfolio_id,
        )

        cost_model = DefaultCostModel(**self.config.cost_config)
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

        for ts, bars_at_t in timeline:
            try:
                market_state = self._builder.build_for_backtest(
                    all_data,
                    ts,
                    active_symbols,
                )
            except ValidationError:
                logger.debug("backtest.warmup_skip", timestamp=str(ts))
                continue

            prices = {sym: bar["close"] for sym, bar in bars_at_t.items()}
            portfolio.update_prices(prices)

            snapshot = portfolio.snapshot()

            signals = await sandbox.safe_evaluate(snapshot, market_state, cost_model)

            for signal in signals:
                if signal.side == Side.HOLD:
                    continue
                if signal.symbol not in active_symbols:
                    continue

                price = prices.get(signal.symbol, 0)
                volume = bars_at_t.get(signal.symbol, {}).get("volume", 0)

                sell_avg_cost = None
                if signal.side == Side.SELL:
                    pos = portfolio.positions.get(signal.symbol)
                    if pos is not None:
                        sell_avg_cost = pos.avg_cost

                order = await order_manager.process_signal(
                    signal,
                    price,
                    volume,
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

    def _apply_strategy_params(self) -> None:
        if not self.config.strategy_params or self.strategy is None:
            return
        for key, value in self.config.strategy_params.items():
            existing = getattr(self.strategy, key, _SENTINEL)
            if existing is _SENTINEL or callable(existing):
                continue
            setattr(self.strategy, key, value)


async def run_backtest(config: BacktestConfig) -> BacktestResult:
    """Standalone backtest entry point.

    Loads the strategy from the plugin registry, obtains a data provider,
    wires all components, and runs the full backtest loop.
    """
    from engine.data.feeds import get_data_provider  # noqa: PLC0415
    from engine.plugins.registry import PluginRegistry  # noqa: PLC0415

    provider = get_data_provider("yahoo")
    registry = PluginRegistry()
    strategy = registry.load_strategy(config.strategy_name)
    if strategy is None:
        raise ValueError(f"Strategy not found: {config.strategy_name}")

    runner = BacktestRunner(config=config, strategy=strategy, provider=provider)
    return await runner.run()


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
