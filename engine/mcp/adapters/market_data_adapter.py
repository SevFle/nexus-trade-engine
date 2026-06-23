"""Market data, cost model, and performance metrics adapters.

* :func:`get_market_data` — fetches OHLCV bars via the configured
  :class:`~engine.data.feeds.MarketDataProvider` and returns normalised bars.
* :func:`get_cost_model` — estimates a cost breakdown for a hypothetical
  trade using the engine :class:`~engine.core.cost_model.DefaultCostModel`.
* :func:`get_performance_metrics` — computes standard performance metrics
  from an equity curve using the engine
  :class:`~engine.core.metrics.PerformanceMetrics`.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from engine.mcp.adapters import EngineServices, to_jsonable
from engine.mcp.errors import EngineError, ValidationError

if TYPE_CHECKING:
    from engine.mcp.auth import AuthPrincipal


def _safe_float(value: object) -> float:
    if value is None:
        raise ValueError("None is not a finite float")
    f = float(value)
    if not math.isfinite(f):
        raise ValueError("non-finite float")
    return f


async def get_market_data(
    services: EngineServices,
    principal: AuthPrincipal,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    symbol = arguments.get("symbol")
    if not symbol:
        raise ValidationError("symbol is required")
    interval = arguments.get("interval", "1d")
    period = arguments.get("period", "1y")

    provider = services.market_data_provider_factory()
    if provider is None:
        raise EngineError("No market-data provider configured")

    try:
        df = await provider.get_ohlcv(symbol, period=period, interval=interval)
    except Exception as exc:
        raise EngineError(
            f"Market data fetch failed: {exc.__class__.__name__}",
        ) from exc

    if df is None or df.empty:
        return to_jsonable({"symbol": symbol, "interval": interval, "period": period, "bars": []})

    bars = []
    for ts, row in df.iterrows():
        try:
            bars.append(
                {
                    "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                    "open": _safe_float(row.get("open")),
                    "high": _safe_float(row.get("high")),
                    "low": _safe_float(row.get("low")),
                    "close": _safe_float(row.get("close")),
                    "volume": _safe_float(row.get("volume")),
                }
            )
        except (ValueError, TypeError, KeyError):
            # Drop NaN/None bars rather than emit invalid JSON.
            continue
    return to_jsonable(
        {
            "symbol": symbol,
            "interval": interval,
            "period": period,
            "bars": bars,
        }
    )


async def get_cost_model(
    services: EngineServices,
    principal: AuthPrincipal,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    symbol = arguments.get("symbol")
    quantity = arguments.get("quantity")
    price = arguments.get("price")
    if not symbol or quantity is None or price is None:
        raise ValidationError("symbol, quantity, and price are required")
    try:
        qty = int(quantity)
        px = float(price)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"Invalid numeric argument: {exc}") from exc
    if qty < 1:
        raise ValidationError("quantity must be >= 1")
    if px <= 0:
        raise ValidationError("price must be > 0")
    side = arguments.get("side", "buy")
    avg_volume = int(arguments.get("avg_volume", 0) or 0)

    breakdown = services.cost_model.estimate_total(
        symbol=symbol, quantity=qty, price=px, side=side, avg_volume=avg_volume
    )
    cost_pct = services.cost_model.estimate_pct(symbol=symbol, price=px, side=side)
    notional = qty * px

    return to_jsonable(
        {
            "symbol": symbol,
            "quantity": qty,
            "price": px,
            "side": side,
            "notional": notional,
            "cost_pct_of_notional": cost_pct * 100.0,
            "breakdown": breakdown.as_dict(),
            "total_cost": breakdown.total.amount,
            "total_cost_excluding_tax": breakdown.total_without_tax.amount,
        }
    )


async def get_performance_metrics(
    services: EngineServices,
    principal: AuthPrincipal,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    equity_curve = arguments.get("equity_curve")
    if not equity_curve or not isinstance(equity_curve, list) or len(equity_curve) < 2:
        raise ValidationError("equity_curve must contain at least 2 points")
    initial_capital = float(arguments.get("initial_capital", 100_000.0))

    from engine.core.metrics import PerformanceMetrics

    try:
        metrics = PerformanceMetrics(
            equity_curve=list(equity_curve),
            trade_log=[],
            initial_cash=initial_capital,
        )
        report = metrics.calculate()
    except Exception as exc:
        raise EngineError(f"Metrics computation failed: {exc.__class__.__name__}") from exc

    return to_jsonable(
        {
            "initial_capital": initial_capital,
            "data_points": len(equity_curve),
            "metrics": report.to_dict(),
        }
    )


__all__ = ["get_cost_model", "get_market_data", "get_performance_metrics"]
