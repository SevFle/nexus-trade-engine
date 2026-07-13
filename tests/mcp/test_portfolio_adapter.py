"""Unit tests for the portfolio MCP adapters.

Covers the portfolio query surface exposed over MCP:

* :func:`get_portfolio_status` — portfolio summary (cash, value, return, P&L).
* :func:`get_positions` — list open positions (now incl. unrealized P&L).
* :func:`get_position` — single-position lookup by symbol (new).
* :func:`get_unrealized_pnl` — aggregated open P&L across the portfolio (new).
* :func:`get_orders` — trade history.

These tests pin the adapter contracts **without** a database or network: the
portfolio store is seeded with the real in-memory
:class:`~engine.core.portfolio.Portfolio` (so position math is exercised
end-to-end), and the ``MarketDataProvider`` / ``PluginRegistry`` are never
touched. They cover:

* the happy path for every query (correct fields + P&L arithmetic),
* an empty portfolio (zero-count / zero-total edge cases),
* single-position lookup by symbol — found, case-insensitive, and not-found,
* per-position unrealized P&L = ``(current_price - avg_cost) * quantity``,
* positions with no market mark degrading to a 0.0 open gain,
* argument validation (``portfolio_id`` and ``symbol``),
* the :func:`~engine.mcp.handlers.dispatch_tool` integration for each new
  route (routing, required-arg validation, error propagation).
"""

from __future__ import annotations

import pytest

from engine.mcp.adapters import EngineServices, PortfolioStore
from engine.mcp.adapters.portfolio_adapter import (
    get_orders,
    get_portfolio_status,
    get_position,
    get_positions,
    get_unrealized_pnl,
)
from engine.mcp.auth import AuthPrincipal
from engine.mcp.errors import MCPError, NotFoundError, ValidationError
from engine.mcp.handlers import dispatch_tool

# ── Shared principal ─────────────────────────────────────────────────────── #
PRINCIPAL = AuthPrincipal(user_id="quant-1", role="viewer", auth_method="jwt")


# ── Fixtures / helpers ──────────────────────────────────────────────────── #
def _services_with(
    *,
    starting_cash: float = 100_000.0,
) -> tuple[EngineServices, PortfolioStore]:
    """Build services backed by a fresh in-memory :class:`PortfolioStore`.

    Returns the store so tests can drive real positions through
    ``portfolio.open_position`` / ``update_prices`` and assert on the exact
    arithmetic the adapter surfaces. The store is the single source of truth,
    so the fakes here mirror exactly what an online deployment would observe.
    """
    store = PortfolioStore(default_capital=starting_cash)
    services = EngineServices.for_testing(portfolio_store=store)
    return services, store


def _seed_positions(
    store: PortfolioStore,
    *,
    buys: list[tuple[str, int, float]],
    prices: dict[str, float] | None = None,
) -> None:
    """Open a set of positions and mark their current prices."""
    portfolio = store.get("default")
    for symbol, qty, price in buys:
        portfolio.open_position(symbol, qty, price)
    if prices:
        portfolio.update_prices(prices)


# ── 1. get_portfolio_status ─────────────────────────────────────────────── #
async def test_get_portfolio_status_returns_summary_with_no_positions():
    """An empty portfolio still returns a valid, zeroed summary."""
    services, _store = _services_with()

    result = await get_portfolio_status(services, PRINCIPAL, {})

    assert result["portfolio_id"] == "default"
    assert result["cash"] == pytest.approx(100_000.0)
    assert result["total_value"] == pytest.approx(100_000.0)
    assert result["total_return_pct"] == pytest.approx(0.0)
    assert result["realized_pnl"] == pytest.approx(0.0)
    assert result["open_positions"] == 0
    assert "Cash" in result["summary"]


async def test_get_portfolio_status_reflects_open_positions():
    services, store = _services_with()
    _seed_positions(
        store,
        buys=[("AAPL", 100, 150.0)],
        prices={"AAPL": 175.0},
    )

    result = await get_portfolio_status(services, PRINCIPAL, {})

    # cash = 100_000 - (100 * 150) = 85_000
    assert result["cash"] == pytest.approx(85_000.0)
    # total_value = cash + (100 * 175) = 85_000 + 17_500 = 102_500
    assert result["total_value"] == pytest.approx(102_500.0)
    assert result["total_return_pct"] == pytest.approx(2.5)
    assert result["open_positions"] == 1


async def test_get_portfolio_status_rejects_non_string_portfolio_id():
    services, _store = _services_with()
    with pytest.raises(ValidationError) as exc_info:
        await get_portfolio_status(services, PRINCIPAL, {"portfolio_id": 123})
    assert "portfolio_id must be a string" in str(exc_info.value)


# ── 2. get_positions — list incl. unrealized P&L ────────────────────────── #
async def test_get_positions_empty_portfolio_returns_zero_count():
    services, _store = _services_with()
    result = await get_positions(services, PRINCIPAL, {})
    assert result == {
        "portfolio_id": "default",
        "count": 0,
        "positions": [],
    }


async def test_get_positions_happy_path_includes_unrealized_pnl():
    services, store = _services_with()
    _seed_positions(
        store,
        buys=[("AAPL", 100, 150.0), ("MSFT", 50, 200.0)],
        prices={"AAPL": 175.0, "MSFT": 200.0},
    )

    result = await get_positions(services, PRINCIPAL, {})

    assert result["portfolio_id"] == "default"
    assert result["count"] == 2

    by_symbol = {p["symbol"]: p for p in result["positions"]}
    aapl = by_symbol["AAPL"]
    assert aapl["quantity"] == 100
    assert aapl["avg_cost"] == pytest.approx(150.0)
    assert aapl["current_price"] == pytest.approx(175.0)
    assert aapl["market_value"] == pytest.approx(17_500.0)
    assert aapl["cost_basis"] == pytest.approx(15_000.0)
    # Unrealized P&L = (175 - 150) * 100 = 2500
    assert aapl["unrealized_pnl"] == pytest.approx(2500.0)
    # pct = (175/150 - 1) * 100 = 16.666...
    assert aapl["unrealized_pnl_pct"] == pytest.approx(16.66667, rel=1e-4)
    assert aapl["allocation_pct"] > 0

    # MSFT is flat (price == cost) → zero open gain.
    msft = by_symbol["MSFT"]
    assert msft["unrealized_pnl"] == pytest.approx(0.0)
    assert msft["unrealized_pnl_pct"] == pytest.approx(0.0)
    assert msft["cost_basis"] == pytest.approx(10_000.0)


async def test_get_positions_without_price_mark_reports_zero_unrealized_pnl():
    """A position with no ``current_price`` mark is valued at cost, so its
    open gain is 0.0 (never NaN, never cost-basis-minus-cost)."""
    services, store = _services_with()
    _seed_positions(store, buys=[("AAPL", 100, 150.0)])  # no update_prices call

    result = await get_positions(services, PRINCIPAL, {})

    aapl = result["positions"][0]
    assert aapl["current_price"] == pytest.approx(0.0)
    assert aapl["market_value"] == pytest.approx(15_000.0)  # falls back to avg_cost
    assert aapl["unrealized_pnl"] == pytest.approx(0.0)
    assert aapl["unrealized_pnl_pct"] == pytest.approx(0.0)


async def test_get_positions_negative_unrealized_pnl_when_underwater():
    services, store = _services_with()
    _seed_positions(
        store,
        buys=[("AAPL", 100, 150.0)],
        prices={"AAPL": 120.0},
    )

    result = await get_positions(services, PRINCIPAL, {})

    aapl = result["positions"][0]
    # (120 - 150) * 100 = -3000
    assert aapl["unrealized_pnl"] == pytest.approx(-3000.0)
    assert aapl["unrealized_pnl_pct"] == pytest.approx(-20.0)


# ── 3. get_position — single-symbol lookup ──────────────────────────────── #
async def test_get_position_happy_path_returns_single_position():
    services, store = _services_with()
    _seed_positions(
        store,
        buys=[("AAPL", 100, 150.0), ("MSFT", 50, 200.0)],
        prices={"AAPL": 175.0, "MSFT": 210.0},
    )

    result = await get_position(services, PRINCIPAL, {"symbol": "AAPL"})

    assert result["portfolio_id"] == "default"
    assert result["symbol"] == "AAPL"
    assert result["quantity"] == 100
    assert result["avg_cost"] == pytest.approx(150.0)
    assert result["current_price"] == pytest.approx(175.0)
    assert result["market_value"] == pytest.approx(17_500.0)
    assert result["cost_basis"] == pytest.approx(15_000.0)
    assert result["unrealized_pnl"] == pytest.approx(2500.0)
    assert result["unrealized_pnl_pct"] == pytest.approx(16.66667, rel=1e-4)
    assert result["allocation_pct"] > 0


async def test_get_position_symbol_is_case_insensitive():
    """Symbol matching is normalised to upper-case, so 'aapl' resolves."""
    services, store = _services_with()
    _seed_positions(store, buys=[("AAPL", 100, 150.0)], prices={"AAPL": 175.0})

    result = await get_position(services, PRINCIPAL, {"symbol": "aapl"})

    assert result["symbol"] == "AAPL"
    assert result["unrealized_pnl"] == pytest.approx(2500.0)


@pytest.mark.parametrize("symbol", [None, "", "   "], ids=["missing", "empty", "blank"])
async def test_get_position_requires_symbol(symbol):
    services, _store = _services_with()
    with pytest.raises(ValidationError) as exc_info:
        await get_position(services, PRINCIPAL, {"symbol": symbol})
    assert "symbol is required" in str(exc_info.value)


async def test_get_position_omitting_symbol_key_is_rejected():
    services, _store = _services_with()
    with pytest.raises(ValidationError):
        await get_position(services, PRINCIPAL, {})


async def test_get_position_unknown_symbol_raises_not_found():
    """Looking up a symbol not held raises NotFoundError (not ValidationError)."""
    services, store = _services_with()
    _seed_positions(store, buys=[("AAPL", 100, 150.0)], prices={"AAPL": 175.0})

    with pytest.raises(NotFoundError) as exc_info:
        await get_position(services, PRINCIPAL, {"symbol": "TSLA"})

    msg = str(exc_info.value)
    assert "TSLA" in msg
    assert "default" in msg


async def test_get_position_unknown_symbol_in_empty_portfolio_raises_not_found():
    """The empty-portfolio case still distinguishes not-found from invalid."""
    services, _store = _services_with()
    with pytest.raises(NotFoundError):
        await get_position(services, PRINCIPAL, {"symbol": "AAPL"})


async def test_get_position_respects_portfolio_id_argument():
    """A non-default portfolio_id is resolved from the store."""
    services, store = _services_with()
    store.seed("alpha", 50_000.0)
    portfolio = store.get("alpha")
    portfolio.open_position("GOOGL", 10, 100.0)
    portfolio.update_prices({"GOOGL": 130.0})

    result = await get_position(
        services, PRINCIPAL, {"portfolio_id": "alpha", "symbol": "googl"}
    )
    assert result["portfolio_id"] == "alpha"
    assert result["symbol"] == "GOOGL"
    assert result["unrealized_pnl"] == pytest.approx(300.0)


# ── 4. get_unrealized_pnl — aggregate open P&L ─────────────────────────── #
async def test_get_unrealized_pnl_empty_portfolio_is_all_zero():
    services, _store = _services_with()
    result = await get_unrealized_pnl(services, PRINCIPAL, {})

    assert result["portfolio_id"] == "default"
    assert result["total_unrealized_pnl"] == pytest.approx(0.0)
    assert result["total_unrealized_pnl_pct"] == pytest.approx(0.0)
    assert result["total_cost_basis"] == pytest.approx(0.0)
    assert result["total_market_value"] == pytest.approx(0.0)
    assert result["position_count"] == 0
    assert result["positions"] == []


async def test_get_unrealized_pnl_aggregates_across_positions():
    services, store = _services_with()
    _seed_positions(
        store,
        # AAPL: cost 15000, marked 17_500  → +2500
        # MSFT: cost 10_000, marked 10_500  → +500
        buys=[("AAPL", 100, 150.0), ("MSFT", 50, 200.0)],
        prices={"AAPL": 175.0, "MSFT": 210.0},
    )

    result = await get_unrealized_pnl(services, PRINCIPAL, {})

    assert result["position_count"] == 2
    assert result["total_cost_basis"] == pytest.approx(25_000.0)
    assert result["total_market_value"] == pytest.approx(28_000.0)
    assert result["total_unrealized_pnl"] == pytest.approx(3000.0)
    # pct = (28000 / 25000 - 1) * 100 = 12.0
    assert result["total_unrealized_pnl_pct"] == pytest.approx(12.0)

    by_symbol = {p["symbol"]: p for p in result["positions"]}
    assert by_symbol["AAPL"]["unrealized_pnl"] == pytest.approx(2500.0)
    assert by_symbol["MSFT"]["unrealized_pnl"] == pytest.approx(500.0)


async def test_get_unrealized_pnl_handles_mixed_winners_and_losers():
    services, store = _services_with()
    _seed_positions(
        store,
        # AAPL: +2500, MSFT: -1000 → net +1500
        buys=[("AAPL", 100, 150.0), ("MSFT", 100, 100.0)],
        prices={"AAPL": 175.0, "MSFT": 90.0},
    )

    result = await get_unrealized_pnl(services, PRINCIPAL, {})

    assert result["total_unrealized_pnl"] == pytest.approx(1500.0)
    assert result["total_cost_basis"] == pytest.approx(25_000.0)
    assert result["total_market_value"] == pytest.approx(26_500.0)


async def test_get_unrealized_pnl_unmarked_positions_contribute_zero():
    """Positions without a current-price mark contribute 0.0 to the total,
    so the aggregate never fabricates gains from absent data."""
    services, store = _services_with()
    _seed_positions(
        store,
        buys=[("AAPL", 100, 150.0), ("MSFT", 50, 200.0)],
        prices={"AAPL": 175.0},  # MSFT left unmarked
    )

    result = await get_unrealized_pnl(services, PRINCIPAL, {})

    by_symbol = {p["symbol"]: p for p in result["positions"]}
    assert by_symbol["AAPL"]["unrealized_pnl"] == pytest.approx(2500.0)
    assert by_symbol["MSFT"]["unrealized_pnl"] == pytest.approx(0.0)
    # Only AAPL's +2500 flows into the net total.
    assert result["total_unrealized_pnl"] == pytest.approx(2500.0)


async def test_get_unrealized_pnl_total_market_value_matches_snapshot():
    """``total_market_value`` must equal the snapshot's positions component
    (``total_value - cash``), so the aggregate stays consistent with
    :func:`get_portfolio_status` regardless of how per-position market values
    are computed. This pins the snapshot-derived derivation in place."""
    services, store = _services_with()
    _seed_positions(
        store,
        buys=[("AAPL", 100, 150.0), ("MSFT", 50, 200.0)],
        prices={"AAPL": 175.0},  # MSFT unmarked → valued at cost by the snapshot
    )

    result = await get_unrealized_pnl(services, PRINCIPAL, {})
    status = await get_portfolio_status(services, PRINCIPAL, {})

    expected_market_value = status["total_value"] - status["cash"]
    assert result["total_market_value"] == pytest.approx(expected_market_value)
    # MSFT valued at cost (50 * 200 = 10_000) + AAPL marked (100 * 175 = 17_500).
    assert result["total_market_value"] == pytest.approx(27_500.0)


# ── 5. get_orders — unchanged contract, still covered ───────────────────── #
async def test_get_orders_empty_portfolio():
    services, _store = _services_with()
    result = await get_orders(services, PRINCIPAL, {})
    assert result == {"portfolio_id": "default", "count": 0, "orders": []}


async def test_get_orders_returns_trade_history():
    services, store = _services_with()
    _seed_positions(store, buys=[("AAPL", 100, 150.0)])

    result = await get_orders(services, PRINCIPAL, {})
    assert result["count"] == 1
    order = result["orders"][0]
    assert order["symbol"] == "AAPL"
    assert order["side"] == "buy"
    assert order["quantity"] == 100
    assert order["price"] == pytest.approx(150.0)
    assert "timestamp" in order
    assert "lot_ids" in order


# ── 6. dispatch_tool integration ────────────────────────────────────────── #
async def test_dispatch_tool_routes_get_position():
    services, store = _services_with()
    _seed_positions(store, buys=[("AAPL", 100, 150.0)], prices={"AAPL": 175.0})

    out = await dispatch_tool("get_position", {"symbol": "AAPL"}, services, PRINCIPAL)

    assert out["symbol"] == "AAPL"
    assert out["unrealized_pnl"] == pytest.approx(2500.0)


async def test_dispatch_tool_get_position_missing_required_symbol():
    """``dispatch_tool`` validates the required ``symbol`` arg up front."""
    services, _store = _services_with()
    with pytest.raises(ValidationError) as exc_info:
        await dispatch_tool("get_position", {}, services, PRINCIPAL)
    assert "symbol" in str(exc_info.value)


async def test_dispatch_tool_get_position_not_found_propagates():
    services, _store = _services_with()
    with pytest.raises(MCPError) as exc_info:
        await dispatch_tool("get_position", {"symbol": "NOPE"}, services, PRINCIPAL)
    # Stays a NotFoundError — not re-wrapped into a generic EngineError.
    assert isinstance(exc_info.value, NotFoundError)


async def test_dispatch_tool_routes_get_unrealized_pnl():
    services, store = _services_with()
    _seed_positions(store, buys=[("AAPL", 100, 150.0)], prices={"AAPL": 175.0})

    out = await dispatch_tool("get_unrealized_pnl", {}, services, PRINCIPAL)

    assert out["position_count"] == 1
    assert out["total_unrealized_pnl"] == pytest.approx(2500.0)


async def test_dispatch_tool_routes_get_positions():
    services, store = _services_with()
    _seed_positions(store, buys=[("AAPL", 100, 150.0)], prices={"AAPL": 175.0})

    out = await dispatch_tool("get_positions", {}, services, PRINCIPAL)

    assert out["count"] == 1
    assert out["positions"][0]["unrealized_pnl"] == pytest.approx(2500.0)


async def test_dispatch_tool_unknown_portfolio_tool_rejected():
    services, _store = _services_with()
    with pytest.raises(ValidationError) as exc_info:
        await dispatch_tool("does_not_exist", {}, services, PRINCIPAL)
    assert "Unknown tool" in str(exc_info.value)


# ── 7. Tool catalog registration ────────────────────────────────────────── #
def test_new_portfolio_tools_are_in_catalog():
    """The new tools are advertised via tools/list and resolvable by name."""
    from engine.mcp.tool_definitions import TOOL_DEFINITIONS, TOOL_INDEX, get_tool

    names = {t.name for t in TOOL_DEFINITIONS}
    assert "get_position" in names
    assert "get_unrealized_pnl" in names

    assert get_tool("get_position") is TOOL_INDEX["get_position"]
    assert get_tool("get_unrealized_pnl") is TOOL_INDEX["get_unrealized_pnl"]


def test_new_portfolio_tool_definitions_are_read_only():
    """Portfolio query tools must be marked read-only / non-destructive."""
    from engine.mcp.tool_definitions import get_tool

    for name in ("get_position", "get_unrealized_pnl", "get_positions"):
        tool = get_tool(name)
        assert tool is not None
        assert tool.read_only is True
        assert tool.destructive is False


def test_get_position_tool_schema_requires_symbol():
    """The JSON schema for get_position declares ``symbol`` as required."""
    from engine.mcp.tool_definitions import get_tool

    tool = get_tool("get_position")
    assert "symbol" in tool.input_schema["required"]
    assert tool.input_schema["properties"]["symbol"]["minLength"] == 1
