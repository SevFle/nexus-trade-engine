"""Backtest execution adapter.

Translates an MCP ``run_backtest`` call into a
:class:`~engine.core.backtest_runner.BacktestRunner` invocation and folds the
result into a compact, LLM-friendly summary. Progress notifications are
emitted around the run (start + completion) when a reporter is supplied.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from engine.core.backtest_runner import BacktestConfig, BacktestRunner
from engine.mcp.adapters import EngineServices, to_jsonable
from engine.mcp.config import mcp_settings
from engine.mcp.errors import EngineError, NotFoundError, ValidationError

if TYPE_CHECKING:
    from engine.mcp.auth import AuthPrincipal
    from engine.mcp.progress import ProgressReporter


async def run_backtest(
    services: EngineServices,
    _principal: AuthPrincipal,
    arguments: dict[str, Any],
    progress: ProgressReporter | None = None,
) -> dict[str, Any]:
    strategy_name = arguments.get("strategy_name")
    symbol = arguments.get("symbol")
    start_date = arguments.get("start_date")
    end_date = arguments.get("end_date")
    initial_capital = float(arguments.get("initial_capital", 100_000.0))

    if not strategy_name:
        raise ValidationError("strategy_name is required")
    if not symbol:
        raise ValidationError("symbol is required")
    if not start_date or not end_date:
        raise ValidationError("start_date and end_date are required")
    if initial_capital <= 0:
        raise ValidationError("initial_capital must be positive")

    strategy = services.plugin_registry.load_strategy(strategy_name)
    if strategy is None:
        raise NotFoundError(f"Strategy not found: {strategy_name}")

    provider = services.market_data_provider_factory()
    if provider is None:
        raise EngineError("No market-data provider configured")

    config = BacktestConfig(
        strategy_name=strategy_name,
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
        initial_capital=initial_capital,
    )
    runner = BacktestRunner(config=config, strategy=strategy, provider=provider)

    if progress is not None:
        await progress.report(
            0, total=100, message=f"Starting backtest {strategy_name} on {symbol}"
        )

    try:
        result = await runner.run()
    except Exception as exc:
        raise EngineError(
            f"Backtest execution failed: {exc.__class__.__name__}",
        ) from exc

    if progress is not None:
        await progress.report(100, total=100, message="Backtest complete")

    metrics = dict(result.metrics or {})
    summary: dict[str, Any] = {
        "strategy_name": strategy_name,
        "symbol": symbol,
        "start_date": start_date,
        "end_date": end_date,
        "initial_capital": initial_capital,
        "final_capital": result.final_capital,
        "total_return_pct": result.total_return_pct,
        "total_trades": len(result.trades),
        "equity_points": len(result.equity_curve),
        "metrics": {
            k: v
            for k, v in metrics.items()
            if k != "evaluation" and not isinstance(v, list)
        },
        "evaluation": metrics.get("evaluation"),
    }
    # Guard against pathological response sizes: never embed the full equity
    # curve / trade log in the summary — callers can page through them via
    # the dedicated read tools if needed.
    summary["equity_curve_truncated"] = mcp_settings.result_token_budget
    return to_jsonable(summary)


__all__ = ["run_backtest"]
