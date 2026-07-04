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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mcp import types


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


# ── Reusable schema fragments ──

_PAGINATION_PROPS: dict[str, Any] = {
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

_PORTFOLIO_ID_PROP: dict[str, Any] = {
    "type": "string",
    "default": "default",
    "description": "Portfolio identifier. Defaults to the in-memory default portfolio.",
}

_DATE_RANGE_PROPS: dict[str, Any] = {
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
            "symbol": {
                "type": "string",
                "minLength": 1,
                "maxLength": 16,
                "description": "Ticker / instrument symbol, e.g. 'AAPL'.",
            },
            "start_date": _DATE_RANGE_PROPS["start_date"],
            "end_date": _DATE_RANGE_PROPS["end_date"],
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
        "properties": {"portfolio_id": _PORTFOLIO_ID_PROP},
        "additionalProperties": False,
    },
)

GET_POSITIONS = ToolDefinition(
    name="get_positions",
    description=(
        "List open positions in a portfolio with quantity, average cost, "
        "current price, market value, and allocation weight. Read-only."
    ),
    input_schema={
        "type": "object",
        "properties": {"portfolio_id": _PORTFOLIO_ID_PROP},
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
            "portfolio_id": _PORTFOLIO_ID_PROP,
            **_PAGINATION_PROPS,
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

SEARCH_STRATEGIES = ToolDefinition(
    name="search_strategies",
    description=(
        "Search the installed-strategy catalog with filters: a free-text "
        "query (matched against name and description), tags (all must be "
        "present), risk_level (e.g. low/medium/high), and asset_class "
        "(e.g. equity/crypto/forex). All filters are optional and "
        "AND-combined; with none supplied every installed strategy is "
        "returned. Use this to narrow list_strategies by attribute. "
        "Read-only."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Case-insensitive substring matched against the strategy "
                    "name and description."
                ),
            },
            "tags": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "description": (
                    "Strategies must contain ALL of these tags "
                    "(case-insensitive). e.g. ['momentum', 'trend-following']."
                ),
            },
            "risk_level": {
                "type": "string",
                "description": (
                    "Exact, case-insensitive risk level, e.g. 'low', "
                    "'medium', or 'high'."
                ),
            },
            "asset_class": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Case-insensitive substring matched against the strategy's "
                    "asset classes, e.g. 'equity' (matches 'US equities'), "
                    "'crypto', 'forex'."
                ),
            },
            **_PAGINATION_PROPS,
        },
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
            "symbol": {
                "type": "string",
                "minLength": 1,
                "maxLength": 16,
                "description": "Ticker / instrument symbol, e.g. 'AAPL'.",
            },
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
            **_PAGINATION_PROPS,
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
            "symbol": {
                "type": "string",
                "minLength": 1,
                "maxLength": 16,
            },
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
    GET_ORDERS,
    LIST_STRATEGIES,
    GET_STRATEGY_DETAILS,
    SEARCH_STRATEGIES,
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
