"""Portfolio inspection adapters (status, positions, orders).

These operate on the in-memory :class:`~engine.mcp.adapters.PortfolioStore`,
which wraps the engine's core :class:`~engine.core.portfolio.Portfolio`. They
expose the same data the REST ``/portfolio`` routes do, but without requiring
a database session — keeping the MCP server deployable as a standalone stdio
process.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from engine.mcp.adapters import EngineServices, to_jsonable
from engine.mcp.errors import ValidationError

if TYPE_CHECKING:
    from engine.mcp.auth import AuthPrincipal


def _resolve_portfolio(services: EngineServices, arguments: dict[str, Any]):
    portfolio_id = arguments.get("portfolio_id") or "default"
    if not isinstance(portfolio_id, str):
        raise ValidationError("portfolio_id must be a string")
    return services.portfolio_store.get(portfolio_id), portfolio_id


async def get_portfolio_status(
    services: EngineServices,
    _principal: AuthPrincipal,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    portfolio, portfolio_id = _resolve_portfolio(services, arguments)
    snapshot = portfolio.snapshot()
    return to_jsonable(
        {
            "portfolio_id": portfolio_id,
            "cash": snapshot.cash,
            "total_value": snapshot.total_value,
            "total_return_pct": snapshot.total_return_pct,
            "realized_pnl": snapshot.realized_pnl,
            "open_positions": len(snapshot.positions),
            "summary": snapshot.summary(),
        }
    )


async def get_positions(
    services: EngineServices,
    _principal: AuthPrincipal,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    portfolio, portfolio_id = _resolve_portfolio(services, arguments)
    snapshot = portfolio.snapshot()
    positions = []
    for symbol, pos in snapshot.positions.items():
        price = pos.get("current_price") or pos.get("avg_cost", 0)
        qty = pos.get("quantity", 0)
        market_value = qty * price
        positions.append(
            {
                "symbol": symbol,
                "quantity": qty,
                "avg_cost": pos.get("avg_cost", 0.0),
                "current_price": pos.get("current_price", 0.0),
                "market_value": market_value,
                "allocation_pct": snapshot.allocation_weight(symbol),
            }
        )
    return to_jsonable(
        {
            "portfolio_id": portfolio_id,
            "count": len(positions),
            "positions": positions,
        }
    )


async def get_orders(
    services: EngineServices,
    _principal: AuthPrincipal,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    portfolio, portfolio_id = _resolve_portfolio(services, arguments)
    orders = [
        {
            "timestamp": tr.timestamp,
            "side": tr.side,
            "symbol": tr.symbol,
            "quantity": tr.quantity,
            "price": tr.price,
            "cost": tr.cost,
            "tax": tr.tax,
            "lot_ids": list(tr.lot_ids),
        }
        for tr in portfolio.trade_history
    ]
    return to_jsonable(
        {
            "portfolio_id": portfolio_id,
            "count": len(orders),
            "orders": orders,
        }
    )


__all__ = ["get_orders", "get_portfolio_status", "get_positions"]
