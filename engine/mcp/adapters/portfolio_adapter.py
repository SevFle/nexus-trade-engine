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
from engine.mcp.errors import NotFoundError, ValidationError

if TYPE_CHECKING:
    from engine.mcp.auth import AuthPrincipal


def _resolve_portfolio(
    services: EngineServices,
    principal: AuthPrincipal,
    arguments: dict[str, Any],
):
    """Resolve and authorize the portfolio referenced by ``arguments``.

    Validation rules:

    * ``portfolio_id`` defaults to ``"default"`` when absent. We avoid the
      ``arguments.get('portfolio_id') or 'default'`` idiom because that would
      silently coerce a falsy-but-present value (e.g. ``""``) into the
      default, masking a malformed request.
    * A non-string or empty/whitespace ``portfolio_id`` raises
      :class:`ValidationError`.
    * The principal must be authorised (:meth:`PortfolioStore.assert_access`)
      *before* any portfolio data is read.
    * A portfolio that the store reports as missing raises
      :class:`ValidationError` rather than being silently auto-created.
    """
    portfolio_id = arguments.get("portfolio_id", "default")
    if not isinstance(portfolio_id, str):
        raise ValidationError("portfolio_id must be a string")
    if not portfolio_id.strip():
        raise ValidationError("portfolio_id must be a non-empty string")
    portfolio_id = portfolio_id.strip()

    services.portfolio_store.assert_access(principal, portfolio_id)

    portfolio = services.portfolio_store.find(portfolio_id)
    if portfolio is None:
        raise ValidationError(f"Portfolio {portfolio_id!r} not found")
    return portfolio, portfolio_id


def _unrealized_pnl(pos: dict[str, Any]) -> float:
    """Per-position unrealized P&L = ``(current_price - avg_cost) * quantity``.

    Returns ``0.0`` when there is no marked current price (the position is
    valued at cost, so the open gain is undefined) or when the position is
    flat/zero. This keeps the aggregate total consistent with the cost-basis
    valuation the snapshot already uses for ``total_value``.
    """
    qty = pos.get("quantity", 0) or 0
    avg_cost = pos.get("avg_cost", 0.0) or 0.0
    current_price = pos.get("current_price", 0.0) or 0.0
    if current_price <= 0 or qty <= 0:
        return 0.0
    return (current_price - avg_cost) * qty


def _unrealized_pnl_pct(pos: dict[str, Any]) -> float:
    """Open return percentage of a position: ``(price/cost - 1) * 100``.

    ``0.0`` when there is no cost basis (e.g. a fresh position priced at
    zero) or no market mark yet, mirroring the guard in
    :func:`_unrealized_pnl`.
    """
    avg_cost = pos.get("avg_cost", 0.0) or 0.0
    current_price = pos.get("current_price", 0.0) or 0.0
    if avg_cost <= 0 or current_price <= 0:
        return 0.0
    return ((current_price / avg_cost) - 1.0) * 100.0


def _position_row(
    symbol: str, pos: dict[str, Any], allocation_pct: float
) -> dict[str, Any]:
    """Canonical per-position projection shared by the list and detail tools.

    A position without a positive market mark is valued at cost (its
    ``market_value`` falls back to ``avg_cost``) while ``current_price`` is
    reported verbatim, so callers can always tell an unmarked position
    (``current_price == 0``) apart from one marked at cost.
    """
    qty = pos.get("quantity", 0) or 0
    avg_cost = pos.get("avg_cost", 0.0) or 0.0
    raw_price = pos.get("current_price")
    # An explicit None/positive check is required: a truthiness test would
    # treat a legitimately-marked price of 0.0 the same as a missing mark.
    price = raw_price if (raw_price is not None and raw_price > 0) else avg_cost
    return {
        "symbol": symbol,
        "quantity": qty,
        "avg_cost": avg_cost,
        "current_price": pos.get("current_price", 0.0),
        "market_value": qty * price,
        "cost_basis": qty * avg_cost,
        "unrealized_pnl": _unrealized_pnl(pos),
        "unrealized_pnl_pct": _unrealized_pnl_pct(pos),
        "allocation_pct": allocation_pct,
    }


async def get_portfolio_status(
    services: EngineServices,
    principal: AuthPrincipal,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    portfolio, portfolio_id = _resolve_portfolio(services, principal, arguments)
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
    principal: AuthPrincipal,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    portfolio, portfolio_id = _resolve_portfolio(services, principal, arguments)
    snapshot = portfolio.snapshot()
    positions = [
        _position_row(symbol, pos, snapshot.allocation_weight(symbol))
        for symbol, pos in snapshot.positions.items()
    ]
    return to_jsonable(
        {
            "portfolio_id": portfolio_id,
            "count": len(positions),
            "positions": positions,
        }
    )


async def get_position(
    services: EngineServices,
    principal: AuthPrincipal,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Return a single position looked up by symbol.

    The symbol is matched case-insensitively against the portfolio's open
    positions. An unknown symbol raises :class:`NotFoundError` so the
    assistant can distinguish "no such position" from a malformed request.
    """
    portfolio, portfolio_id = _resolve_portfolio(services, principal, arguments)
    symbol = arguments.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValidationError("symbol is required")
    symbol = symbol.strip().upper()

    snapshot = portfolio.snapshot()
    pos = snapshot.positions.get(symbol)
    if pos is None:
        raise NotFoundError(f"No position for {symbol} in portfolio {portfolio_id!r}")

    return to_jsonable(
        {
            "portfolio_id": portfolio_id,
            **_position_row(symbol, pos, snapshot.allocation_weight(symbol)),
        }
    )


async def get_unrealized_pnl(
    services: EngineServices,
    principal: AuthPrincipal,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Aggregate the open (unrealized) P&L across the whole portfolio.

    Returns the per-position breakdown plus the net total. Positions without
    a market mark contribute ``0.0`` so the total never double-counts gains
    that are not yet realised or marked.

    ``total_market_value`` is derived from the portfolio snapshot (rather than
    re-summed here) so it stays consistent with the ``total_value`` reported
    by :func:`get_portfolio_status` and the cost-basis valuation used for tax
    lots.
    """
    portfolio, portfolio_id = _resolve_portfolio(services, principal, arguments)
    snapshot = portfolio.snapshot()

    positions: list[dict[str, Any]] = []
    total_unrealized = 0.0
    total_cost_basis = 0.0
    for symbol, pos in snapshot.positions.items():
        pnl = _unrealized_pnl(pos)
        total_unrealized += pnl
        total_cost_basis += pos.get("quantity", 0) * (pos.get("avg_cost", 0.0) or 0.0)
        positions.append(_position_row(symbol, pos, 0.0))

    # Derive the aggregate market value from the snapshot so it is identical
    # to the positions component of ``snapshot.total_value``. Re-summing the
    # per-position ``market_value`` here would risk drift if the snapshot's
    # valuation ever changed.
    total_market_value = snapshot.total_value - snapshot.cash

    total_pct = (
        ((total_market_value / total_cost_basis) - 1.0) * 100.0
        if total_cost_basis > 0
        else 0.0
    )
    return to_jsonable(
        {
            "portfolio_id": portfolio_id,
            "total_unrealized_pnl": total_unrealized,
            "total_unrealized_pnl_pct": total_pct,
            "total_cost_basis": total_cost_basis,
            "total_market_value": total_market_value,
            "position_count": len(positions),
            "positions": positions,
        }
    )


async def get_orders(
    services: EngineServices,
    principal: AuthPrincipal,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    portfolio, portfolio_id = _resolve_portfolio(services, principal, arguments)
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


__all__ = [
    "get_orders",
    "get_portfolio_status",
    "get_position",
    "get_positions",
    "get_unrealized_pnl",
]
