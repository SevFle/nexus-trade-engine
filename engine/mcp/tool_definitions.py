"""Declarative MCP tool definitions.

Each :class:`ToolDefinition` bundles:

* ``name`` — the MCP tool identifier the assistant calls.
* ``description`` — an LM-readable summary (this is what the model reads to
  decide whether/when to call the tool, so it is written for clarity).
* ``input_schema`` — a JSON Schema (draft-07) dict used both for MCP
  ``inputSchema`` advertisement and for runtime argument validation.
* ``annotations`` — MCP :class:`ToolAnnotations` hints (read-only, etc.).
* ``required_role`` — the minimum RBAC role needed to invoke the tool. The
  default for every tool is the read-only ``viewer`` role; ``run_backtest``
  requires ``quant_dev`` because it is compute-intensive.

Schema fragments
----------------
The reusable JSON-Schema snippets (symbol, portfolio id, date range,
pagination) are produced by small factory functions rather than shared
module-level dicts. Two reasons:

* **No duplication** — every tool that takes a ``symbol`` property gets the
  same ``minLength``/``maxLength``/description via one call.
* **No aliasing** — returning a fresh dict per call avoids the latent bug
  where several tool schemas pointed at the *same* dict object, so a future
  in-place edit of one tool's fragment would silently mutate every other
  tool that aliased it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mcp import types

_DEFAULT_SYMBOL_DESCRIPTION = "Ticker / instrument symbol, e.g. 'AAPL'."


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    required_role: str = "viewer"
    read_only: bool = True
    destructive: bool = False
    idempotent: bool = True

    def to_mcp_tool(self) -> types.Tool:
        return types.Tool(
            name=self.name,
            description=self.description,
            inputSchema=self.input_schema,
            annotations=types.ToolAnnotations(
                readOnlyHint=self.read_only,
                destructiveHint=self.destructive,
                idempotentHint=self.idempotent,
                openWorldHint=True,
            ),
        )


# ── Reusable schema-fragment factories ──
# Each returns a NEW dict so tool schemas never alias one another.


def _symbol_prop(
    description: str | None = _DEFAULT_SYMBOL_DESCRIPTION,
) -> dict[str, Any]:
    """A ticker symbol property: non-empty, ≤16 chars.

    Pass ``description=None`` to omit the ``description`` key entirely
    (preserved for tools whose original schema carried none).
    """
    prop: dict[str, Any] = {
        "type": "string",
        "minLength": 1,
        "maxLength": 16,
    }
    if description is not None:
        prop["description"] = description
    return prop


def _portfolio_id_prop() -> dict[str, Any]:
    return {
        "type": "string",
        "default": "default",
        "description": "Portfolio identifier. Defaults to the in-memory default portfolio.",
    }


def _date_range_props() -> dict[str, dict[str, Any]]:
    """Inclusive ISO-8601 ``start_date`` / ``end_date`` pair."""
    return {
        "start_date": {
            "type": "string",
            "format": "date",
            "description": "Inclusive start date (ISO-8601, e.g. 2023-01-01).",
        },
        "end_date": {
            "type": "string",
            "format": "date",
            "description": "Inclusive end date (ISO-8601, e.g. 2024-01-01).",
        },
    }


def _pagination_props() -> dict[str, dict[str, Any]]:
    """``limit`` + opaque ``cursor`` for list-heavy tools."""
    return {
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 500,
            "default": 50,
            "description": "Maximum number of items to return in one page.",
        },
        "cursor": {
            "type": "string",
            "description": "Opaque cursor returned by a previous page (base64 offset).",
        },
    }


# ── Tool catalog ──

RUN_BACKTEST = ToolDefinition(
    name="run_backtest",
    description=(
        "Run a historical backtest of a strategy on a single symbol over a "
        "date range. Returns performance metrics (total return, Sharpe, max "
        "drawdown, win rate, cost drag), trade count, and final capital. "
        "This is compute-only and never places live orders. Requires the "
        "'quant_dev' role."
    ),
    required_role="quant_dev",
    read_only=True,
    idempotent=True,
    input_schema={
        "type": "object",
        "properties": {
            "strategy_name": {
                "type": "string",
                "description": "Name of an installed strategy (see list_strategies).",
            },
            "symbol": _symbol_prop(),
            **_date_range_props(),
            "initial_capital": {
                "type": "number",
                "exclusiveMinimum": 0,
                "default": 100000,
                "description": "Starting cash for the backtest (USD).",
            },
        },
        "required": ["strategy_name", "symbol", "start_date", "end_date"],
        "additionalProperties": False,
    },
)

GET_PORTFOLIO_STATUS = ToolDefinition(
    name="get_portfolio_status",
    description=(
        "Return a summary of a portfolio: cash, total market value, total "
        "return percentage, and realized P&L. Read-only."
    ),
    input_schema={
        "type": "object",
        "properties": {"portfolio_id": _portfolio_id_prop()},
        "additionalProperties": False,
    },
)

GET_POSITIONS = ToolDefinition(
    name="get_positions",
    description=(
        "List open positions in a portfolio with quantity, average cost, "
        "current price, market value, cost basis, unrealized P&L "
        "(absolute and percentage), and allocation weight. Read-only."
    ),
    input_schema={
        "type": "object",
        "properties": {"portfolio_id": _portfolio_id_prop()},
        "additionalProperties": False,
    },
)

GET_POSITION = ToolDefinition(
    name="get_position",
    description=(
        "Look up a single open position by symbol. Returns quantity, average "
        "cost, current price, market value, cost basis, unrealized P&L "
        "(absolute and percentage), and portfolio allocation weight. "
        "Raises a not-found error when the symbol is not held. Read-only."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "portfolio_id": _portfolio_id_prop(),
            "symbol": _symbol_prop(
                "Ticker / instrument symbol, e.g. 'AAPL'. Case-insensitive."
            ),
        },
        "required": ["symbol"],
        "additionalProperties": False,
    },
)

GET_UNREALIZED_PNL = ToolDefinition(
    name="get_unrealized_pnl",
    description=(
        "Aggregate the open (unrealized) profit & loss across a portfolio. "
        "Returns the net total unrealized P&L in absolute and percentage "
        "terms, plus a per-position breakdown. Useful for a quick 'how am "
        "I doing right now' snapshot. Read-only."
    ),
    input_schema={
        "type": "object",
        "properties": {"portfolio_id": _portfolio_id_prop()},
        "additionalProperties": False,
    },
)

GET_ORDERS = ToolDefinition(
    name="get_orders",
    description=(
        "Return the trade/order history for a portfolio (chronological). "
        "Results are paginated to stay within the assistant's context budget. "
        "Read-only."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "portfolio_id": _portfolio_id_prop(),
            **_pagination_props(),
        },
        "additionalProperties": False,
    },
)

LIST_STRATEGIES = ToolDefinition(
    name="list_strategies",
    description=(
        "Enumerate all installed trading strategies with their version, "
        "description, author, supported symbols, and default parameters. "
        "Read-only."
    ),
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
)

GET_STRATEGY_DETAILS = ToolDefinition(
    name="get_strategy_details",
    description=(
        "Return full metadata for a single strategy: description, version, "
        "author, default parameters, symbols, and timeframe. Read-only."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "strategy_name": {
                "type": "string",
                "description": "Strategy name as returned by list_strategies.",
            },
        },
        "required": ["strategy_name"],
        "additionalProperties": False,
    },
)

GET_MARKET_DATA = ToolDefinition(
    name="get_market_data",
    description=(
        "Fetch OHLCV market data (bars) for a symbol over a period. Returns "
        "timestamp, open, high, low, close, volume per bar. Results are "
        "paginated. Read-only."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "symbol": _symbol_prop(),
            "interval": {
                "type": "string",
                "enum": ["1m", "5m", "15m", "1h", "1d", "1wk", "1mo"],
                "default": "1d",
                "description": "Bar interval.",
            },
            "period": {
                "type": "string",
                "default": "1y",
                "description": "Lookback period, e.g. '1y', '6mo', '3mo', '1mo'.",
            },
            **_pagination_props(),
        },
        "required": ["symbol"],
        "additionalProperties": False,
    },
)

GET_COST_MODEL = ToolDefinition(
    name="get_cost_model",
    description=(
        "Estimate the transaction cost breakdown for a hypothetical trade "
        "(commission, spread, slippage, exchange fee, tax) using the engine "
        "cost model. Useful for cost-aware strategy decisions. Read-only."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "symbol": _symbol_prop(description=None),
            "quantity": {"type": "integer", "minimum": 1},
            "price": {"type": "number", "exclusiveMinimum": 0},
            "side": {"type": "string", "enum": ["buy", "sell"], "default": "buy"},
            "avg_volume": {
                "type": "integer",
                "minimum": 0,
                "default": 0,
                "description": "Average daily volume (for slippage estimate).",
            },
        },
        "required": ["symbol", "quantity", "price"],
        "additionalProperties": False,
    },
)

GET_PERFORMANCE_METRICS = ToolDefinition(
    name="get_performance_metrics",
    description=(
        "Compute performance metrics (total return, annualized return, "
        "Sharpe, Sortino, max drawdown, win rate, profit factor) from an "
        "equity curve and optional trade log. Read-only."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "equity_curve": {
                "type": "array",
                "minItems": 2,
                "items": {
                    "type": "object",
                    "properties": {
                        "timestamp": {"type": "string"},
                        "total_value": {"type": "number"},
                    },
                    "required": ["timestamp", "total_value"],
                },
                "description": "Chronological equity curve points.",
            },
            "initial_capital": {
                "type": "number",
                "exclusiveMinimum": 0,
                "default": 100000,
            },
        },
        "required": ["equity_curve"],
        "additionalProperties": False,
    },
)


TOOL_DEFINITIONS: list[ToolDefinition] = [
    RUN_BACKTEST,
    GET_PORTFOLIO_STATUS,
    GET_POSITIONS,
    GET_POSITION,
    GET_UNREALIZED_PNL,
    GET_ORDERS,
    LIST_STRATEGIES,
    GET_STRATEGY_DETAILS,
    GET_MARKET_DATA,
    GET_COST_MODEL,
    GET_PERFORMANCE_METRICS,
]

TOOL_INDEX: dict[str, ToolDefinition] = {t.name: t for t in TOOL_DEFINITIONS}


def get_tool(name: str) -> ToolDefinition | None:
    return TOOL_INDEX.get(name)


def mcp_tools() -> list[types.Tool]:
    return [t.to_mcp_tool() for t in TOOL_DEFINITIONS]


__all__ = [
    "TOOL_DEFINITIONS",
    "TOOL_INDEX",
    "ToolDefinition",
    "get_tool",
    "mcp_tools",
]
