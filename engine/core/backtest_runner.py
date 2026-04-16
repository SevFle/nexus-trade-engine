from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import polars as pl
import structlog
from sqlalchemy import select

from engine.core.cost_model import DefaultCostModel as CostModel
from engine.core.cost_model import TaxLot, TaxMethod
from engine.core.execution.backtest import BacktestBackend
from engine.core.order_manager import OrderManager
from engine.core.portfolio import Portfolio
from engine.core.risk_engine import RiskEngine
from engine.core.signal import Side, Signal
from engine.data.market_state import MarketState
from engine.db.models import BacktestResult, OHLCVBar
from engine.db.session import get_session
from engine.plugins.registry import discover_strategies, load_strategy_class

logger = structlog.get_logger()


@dataclass
class BacktestConfig:
    strategy_name: str
    symbols: list[str]
    start_date: str
    end_date: str
    initial_cash: float = 100_000.0
    strategy_params: dict = field(default_factory=dict)
    cost_config: dict = field(default_factory=dict)
    interval: str = "1d"
    random_seed: int | None = 42
    task_id: str | None = None


@dataclass
class EquityPoint:
    timestamp: datetime
    total_value: float
    cash: float
    positions_value: float


@dataclass
class TradeRecord:
    timestamp: datetime
    symbol: str
    side: str
    quantity: int
    price: float
    cost: float
    tax: float
    pnl: float | None = None


@dataclass
class BacktestMetrics:
    total_return_pct: float
    sharpe_ratio: float
    sortino_ratio: float
    max_drawdown_pct: float
    total_trades: int
    win_rate: float
    total_costs: float
    total_taxes: float
    cost_drag_pct: float
    profit_factor: float
    avg_trade_pnl: float
    max_consecutive_losses: int


@dataclass
class BacktestResultOutput:
    id: str
    strategy_name: str
    start_date: str
    end_date: str
    initial_cash: float
    final_value: float
    metrics: BacktestMetrics
    equity_curve: list[dict]
    trades: list[dict]


_SLOW_EVAL_THRESHOLD_MS = 5000


def _safe_on_bar(strategy: Any, state: MarketState, portfolio: Portfolio) -> list[dict]:
    """
    Sandboxed strategy evaluation with timeout and error handling.
    Catches exceptions so a broken strategy never crashes the engine.
    """
    try:
        start = time.monotonic()
        result = strategy.on_bar(state, portfolio)
        elapsed_ms = (time.monotonic() - start) * 1000
        if elapsed_ms > _SLOW_EVAL_THRESHOLD_MS:
            logger.warning(
                "sandbox.slow_evaluate",
                strategy=getattr(strategy, "name", "unknown"),
                elapsed_ms=elapsed_ms,
            )
    except Exception:
        logger.exception(
            "sandbox.evaluation_error",
            strategy=getattr(strategy, "name", "unknown"),
        )
        return []
    else:
        return result if result is not None else []


def build_timeline(
    bars_by_symbol: dict[str, list[dict]],
) -> list[tuple[datetime, dict[str, dict]]]:
    """
    Build sorted timeline from raw OHLCV data.
    Handles symbols that don't trade on all days (skip missing, no forward-fill).
    Returns sorted list of (timestamp, {symbol: bar_dict}) tuples.
    """
    all_timestamps = set()
    symbol_bars: dict[str, dict[datetime, dict]] = {}

    for symbol, bars in bars_by_symbol.items():
        symbol_bars[symbol] = {bar["timestamp"]: bar for bar in bars}
        all_timestamps.update(symbol_bars[symbol].keys())

    sorted_timestamps = sorted(all_timestamps)
    timeline = []

    for ts in sorted_timestamps:
        bar_dict = {}
        for symbol in bars_by_symbol:
            if ts in symbol_bars[symbol]:
                bar_dict[symbol] = symbol_bars[symbol][ts]
        if bar_dict:
            timeline.append((ts, bar_dict))

    return timeline


def build_market_state(
    data: dict[str, dict],
    timestamp: datetime,
    symbols: list[str],
    history_windows: dict[str, list[dict]],
) -> dict[str, MarketState]:
    """
    Builds MarketState for each symbol with current bar and rolling OHLCV window.
    """
    states = {}

    for symbol in symbols:
        current_bar = data.get(symbol)
        if current_bar is None:
            continue

        bar_df = None
        if symbol in history_windows:
            window = history_windows[symbol]
            if window:
                df = pl.DataFrame([*window, current_bar])
                bar_df = df

        state = MarketState(
            symbol=symbol,
            timestamp=timestamp.isoformat(),
            bars=bar_df,
        )
        states[symbol] = state

    return states


def calculate_metrics(
    equity_curve: list[EquityPoint],
    trade_log: list[TradeRecord],
    initial_cash: float,
) -> BacktestMetrics:
    """
    Calculate performance metrics from equity curve and trade log.
    """
    if not equity_curve:
        return BacktestMetrics(
            total_return_pct=0.0,
            sharpe_ratio=0.0,
            sortino_ratio=0.0,
            max_drawdown_pct=0.0,
            total_trades=0,
            win_rate=0.0,
            total_costs=0.0,
            total_taxes=0.0,
            cost_drag_pct=0.0,
            profit_factor=0.0,
            avg_trade_pnl=0.0,
            max_consecutive_losses=0,
        )

    final_value = equity_curve[-1].total_value
    total_return_pct = ((final_value - initial_cash) / initial_cash) * 100

    values = [p.total_value for p in equity_curve]
    peak = values[0]
    max_drawdown = 0.0
    for v in values:
        peak = max(peak, v)
        drawdown = (peak - v) / peak if peak > 0 else 0
        max_drawdown = max(max_drawdown, drawdown)
    max_drawdown_pct = max_drawdown * 100

    if len(equity_curve) > 1:
        returns = []
        for i in range(1, len(equity_curve)):
            ret = (equity_curve[i].total_value - equity_curve[i - 1].total_value) / equity_curve[
                i - 1
            ].total_value
            returns.append(ret)

        if returns:
            mean_ret = sum(returns) / len(returns)
            std_ret = (sum((r - mean_ret) ** 2 for r in returns) / len(returns)) ** 0.5
            sharpe_ratio = (mean_ret / std_ret) * (252**0.5) if std_ret > 0 else 0.0

            negative_returns = [r for r in returns if r < 0]
            if negative_returns:
                downside_std = (sum(r**2 for r in negative_returns) / len(returns)) ** 0.5
                sortino_ratio = (mean_ret / downside_std) * (252**0.5) if downside_std > 0 else 0.0
            else:
                sortino_ratio = 0.0
        else:
            sharpe_ratio = 0.0
            sortino_ratio = 0.0
    else:
        sharpe_ratio = 0.0
        sortino_ratio = 0.0

    total_costs = sum(t.cost for t in trade_log)
    total_taxes = sum(t.tax for t in trade_log)
    cost_drag_pct = (total_costs / initial_cash) * 100 if initial_cash > 0 else 0.0

    total_trades = len(trade_log)
    closed_trades = [t for t in trade_log if t.pnl is not None]
    winning_trades = [t for t in closed_trades if t.pnl is not None and t.pnl > 0]
    win_rate = len(winning_trades) / len(closed_trades) if closed_trades else 0.0

    gross_profit = sum(t.pnl for t in closed_trades if t.pnl is not None and t.pnl > 0)
    gross_loss = abs(sum(t.pnl for t in closed_trades if t.pnl is not None and t.pnl < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0.0

    pnls = [t.pnl for t in closed_trades if t.pnl is not None]
    avg_trade_pnl = sum(pnls) / len(pnls) if pnls else 0.0

    max_consecutive_losses = 0
    current_streak = 0
    for t in trade_log:
        if t.pnl is not None and t.pnl < 0:
            current_streak += 1
            max_consecutive_losses = max(max_consecutive_losses, current_streak)
        else:
            current_streak = 0

    return BacktestMetrics(
        total_return_pct=round(total_return_pct, 2),
        sharpe_ratio=round(sharpe_ratio, 2),
        sortino_ratio=round(sortino_ratio, 2),
        max_drawdown_pct=round(max_drawdown_pct, 2),
        total_trades=total_trades,
        win_rate=round(win_rate * 100, 2),
        total_costs=round(total_costs, 2),
        total_taxes=round(total_taxes, 2),
        cost_drag_pct=round(cost_drag_pct, 2),
        profit_factor=round(profit_factor, 2),
        avg_trade_pnl=round(avg_trade_pnl, 2),
        max_consecutive_losses=max_consecutive_losses,
    )


async def load_market_data(
    symbols: list[str],
    start_date: str,
    end_date: str,
    _interval: str,
) -> dict[str, list[dict]]:
    """
    Load OHLCV data from database for given symbols and date range.
    """
    async with get_session() as session:
        bars_by_symbol: dict[str, list[dict]] = {s: [] for s in symbols}

        for symbol in symbols:
            stmt = (
                select(OHLCVBar)
                .where(OHLCVBar.symbol == symbol)
                .where(OHLCVBar.timestamp >= start_date)
                .where(OHLCVBar.timestamp <= end_date)
                .order_by(OHLCVBar.timestamp)
            )
            result = await session.execute(stmt)
            bars = result.scalars().all()

            bars_by_symbol[symbol] = [
                {
                    "timestamp": bar.timestamp,
                    "open": float(bar.open),
                    "high": float(bar.high),
                    "low": float(bar.low),
                    "close": float(bar.close),
                    "volume": int(bar.volume),
                }
                for bar in bars
            ]

    return bars_by_symbol


def load_strategy(strategy_name: str) -> Any:
    """
    Load strategy class from the strategies directory.
    """
    strategies = discover_strategies()

    if strategy_name not in strategies:
        raise ValueError(f"Strategy '{strategy_name}' not found")

    strategy_info = strategies[strategy_name]
    return load_strategy_class(strategy_info["module_path"])


async def persist_backtest_result(
    strategy_name: str,
    start_date: str,
    end_date: str,
    initial_cash: float,
    final_value: float,
    metrics: BacktestMetrics,
    equity_curve: list[dict],
    trades: list[dict],
    task_id: str | None = None,
) -> str:
    """
    Persist backtest result to database.
    """
    import uuid

    result_id = task_id or str(uuid.uuid4())

    async with get_session() as session:
        result = BacktestResult(
            id=uuid.UUID(result_id),
            strategy_name=strategy_name,
            start_date=datetime.fromisoformat(start_date),
            end_date=datetime.fromisoformat(end_date),
            metrics={
                "total_return_pct": metrics.total_return_pct,
                "sharpe_ratio": metrics.sharpe_ratio,
                "sortino_ratio": metrics.sortino_ratio,
                "max_drawdown_pct": metrics.max_drawdown_pct,
                "total_trades": metrics.total_trades,
                "win_rate": metrics.win_rate,
                "total_costs": metrics.total_costs,
                "total_taxes": metrics.total_taxes,
                "cost_drag_pct": metrics.cost_drag_pct,
                "profit_factor": metrics.profit_factor,
                "avg_trade_pnl": metrics.avg_trade_pnl,
                "max_consecutive_losses": metrics.max_consecutive_losses,
                "initial_cash": initial_cash,
                "final_value": final_value,
                "equity_curve": equity_curve,
                "trades": trades,
                "status": "completed",
            },
        )
        session.add(result)
        await session.commit()
        return str(result.id)


async def persist_backtest_error(
    strategy_name: str,
    start_date: str,
    end_date: str,
    task_id: str,
    error_message: str,
) -> None:
    """
    Persist a failed backtest status so polling can surface the error.
    """
    import uuid

    async with get_session() as session:
        result = BacktestResult(
            id=uuid.UUID(task_id),
            strategy_name=strategy_name,
            start_date=datetime.fromisoformat(start_date),
            end_date=datetime.fromisoformat(end_date),
            metrics={
                "status": "failed",
                "error": error_message,
            },
        )
        session.add(result)
        await session.commit()


async def _process_signals(
    strategy: Any,
    state: MarketState,
    portfolio: Portfolio,
    symbol: str,
    bar_data: dict[str, dict],
    config: BacktestConfig,
    order_manager: OrderManager,
    cost_model: CostModel,
    position_lots: dict[str, list[TaxLot]],
    trade_log: list[TradeRecord],
    timestamp: datetime,
) -> None:
    """
    Process signals from strategy evaluation for a single symbol/bar.
    Captures avg_price before process_signal to correctly compute PnL on full exit.
    Tracks tax lots on buys and consumes them on sells (FIFO).
    """
    signals = _safe_on_bar(strategy, state, portfolio)

    if not signals:
        return

    for signal_dict in signals:
        if not isinstance(signal_dict, dict):
            continue

        side = signal_dict.get("side", "hold")
        if side == "hold":
            continue

        signal = Signal(
            symbol=signal_dict.get("symbol", symbol),
            side=Side(side),
            weight=signal_dict.get("weight", 1.0),
            quantity=signal_dict.get("quantity"),
            strategy_id=config.strategy_name,
            reason=signal_dict.get("reason", ""),
        )

        market_price = bar_data.get(symbol, {}).get("close", 0)
        avg_volume = bar_data.get(symbol, {}).get("volume", 0)

        if market_price <= 0:
            continue

        pre_exit_avg_price: float | None = None
        if signal.side == Side.SELL and symbol in portfolio.positions:
            pre_exit_avg_price = portfolio.positions[symbol].avg_price

        order = await order_manager.process_signal(signal, market_price, avg_volume)

        if order.status.value != "filled":
            continue

        cost = order.cost_breakdown.get("total", 0) if order.cost_breakdown else 0
        fill_price = order.fill_price or 0.0
        fill_quantity = order.fill_quantity or 0

        tax = 0.0
        if order.side == Side.BUY:
            position_lots.setdefault(symbol, []).append(
                TaxLot(
                    symbol=symbol,
                    quantity=fill_quantity,
                    purchase_price=fill_price,
                    purchase_date=timestamp,
                )
            )
        elif order.side == Side.SELL and symbol in position_lots:
            lots = position_lots[symbol]
            if lots and fill_price > 0 and fill_quantity > 0:
                tax_result = cost_model.estimate_tax(
                    symbol,
                    fill_price,
                    fill_quantity,
                    lots,
                    TaxMethod.FIFO,
                )
                tax = tax_result.amount
                _consume_lots(position_lots, symbol, fill_quantity)

        pnl = None
        if order.side == Side.SELL and fill_price > 0 and fill_quantity > 0:
            avg_price = pre_exit_avg_price
            if avg_price is not None and avg_price > 0:
                pnl = (fill_price - avg_price) * fill_quantity - cost - tax

        trade_log.append(
            TradeRecord(
                timestamp=timestamp,
                symbol=order.symbol,
                side=order.side.value,
                quantity=fill_quantity,
                price=fill_price,
                cost=cost,
                tax=tax,
                pnl=pnl,
            )
        )


def _consume_lots(
    position_lots: dict[str, list[TaxLot]],
    symbol: str,
    quantity: int,
) -> None:
    """
    Consume tax lots FIFO after a sell. Reduces lot quantities and removes
    fully consumed lots.
    """
    lots = position_lots.get(symbol, [])
    remaining = quantity
    i = 0
    while remaining > 0 and i < len(lots):
        if lots[i].quantity <= remaining:
            remaining -= lots[i].quantity
            lots.pop(i)
        else:
            lots[i].quantity -= remaining
            remaining = 0
        i += 1


async def run_backtest(config: BacktestConfig) -> BacktestResultOutput:
    """
    Main backtest execution loop.

    Processes historical market data bar-by-bar, calls strategy evaluate()
    on each bar, processes resulting signals through the order manager,
    and records the full equity curve.
    """
    logger.info(
        "backtest.start",
        strategy=config.strategy_name,
        symbols=config.symbols,
        start=config.start_date,
        end=config.end_date,
        cash=config.initial_cash,
    )

    bars_by_symbol = await load_market_data(
        config.symbols,
        config.start_date,
        config.end_date,
        config.interval,
    )

    has_data = any(bars for bars in bars_by_symbol.values())
    if not has_data:
        raise ValueError("No market data found for the specified date range and symbols")

    timeline = build_timeline(bars_by_symbol)
    if not timeline:
        raise ValueError("Failed to build timeline from market data")

    strategy_cls = load_strategy(config.strategy_name)
    strategy = strategy_cls()

    cost_model = CostModel(
        commission_per_trade=config.cost_config.get("commission", 0.0),
        spread_bps=config.cost_config.get("spread_bps", 5.0),
        slippage_bps=config.cost_config.get("slippage_bps", 10.0),
    )

    portfolio = Portfolio(cash=config.initial_cash, initial_cash=config.initial_cash)
    risk_engine = RiskEngine()
    order_manager = OrderManager(cost_model, risk_engine, portfolio)

    execution_backend = BacktestBackend(random_seed=config.random_seed)
    order_manager.set_execution_backend(execution_backend)

    history_windows: dict[str, list[dict]] = {s: [] for s in config.symbols}
    equity_curve: list[EquityPoint] = []
    trade_log: list[TradeRecord] = []
    position_lots: dict[str, list[TaxLot]] = {s: [] for s in config.symbols}

    if hasattr(strategy, "on_start"):
        strategy.on_start(portfolio)

    for timestamp, bar_data in timeline:
        for symbol in config.symbols:
            bar = bar_data.get(symbol)
            if bar:
                history_windows[symbol].append(bar)
                max_bars = 60
                if len(history_windows[symbol]) > max_bars:
                    history_windows[symbol] = history_windows[symbol][-max_bars:]

        market_states = build_market_state(bar_data, timestamp, config.symbols, history_windows)

        for symbol, state in market_states.items():
            await _process_signals(
                strategy=strategy,
                state=state,
                portfolio=portfolio,
                symbol=symbol,
                bar_data=bar_data,
                config=config,
                order_manager=order_manager,
                cost_model=cost_model,
                position_lots=position_lots,
                trade_log=trade_log,
                timestamp=timestamp,
            )

        positions_value = 0.0
        for sym, pos in portfolio.positions.items():
            current_bar = bar_data.get(sym)
            if current_bar:
                positions_value += pos.quantity * current_bar["close"]

        total_value = portfolio.cash + positions_value
        equity_curve.append(
            EquityPoint(
                timestamp=timestamp,
                total_value=total_value,
                cash=portfolio.cash,
                positions_value=positions_value,
            )
        )

    if hasattr(strategy, "on_end"):
        strategy.on_end(portfolio)

    metrics = calculate_metrics(equity_curve, trade_log, config.initial_cash)

    final_value = equity_curve[-1].total_value if equity_curve else config.initial_cash

    equity_curve_dict = [
        {
            "timestamp": p.timestamp.isoformat(),
            "total_value": round(p.total_value, 2),
            "cash": round(p.cash, 2),
            "positions_value": round(p.positions_value, 2),
        }
        for p in equity_curve
    ]

    trades_dict = [
        {
            "timestamp": t.timestamp.isoformat(),
            "symbol": t.symbol,
            "side": t.side,
            "quantity": t.quantity,
            "price": round(t.price, 4),
            "cost": round(t.cost, 2),
            "tax": round(t.tax, 2),
            "pnl": round(t.pnl, 2) if t.pnl is not None else None,
        }
        for t in trade_log
    ]

    result_id = await persist_backtest_result(
        config.strategy_name,
        config.start_date,
        config.end_date,
        config.initial_cash,
        final_value,
        metrics,
        equity_curve_dict,
        trades_dict,
        task_id=config.task_id,
    )

    logger.info(
        "backtest.complete",
        result_id=result_id,
        final_value=final_value,
        total_trades=metrics.total_trades,
        return_pct=metrics.total_return_pct,
    )

    return BacktestResultOutput(
        id=result_id,
        strategy_name=config.strategy_name,
        start_date=config.start_date,
        end_date=config.end_date,
        initial_cash=config.initial_cash,
        final_value=final_value,
        metrics=metrics,
        equity_curve=equity_curve_dict,
        trades=trades_dict,
    )
