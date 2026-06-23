"""Static reference resources exposed via MCP ``resources/list`` & ``read``.

Resources give the assistant context without a tool call — the strategy
catalog, supported timeframes, symbol universe, and cost-model defaults. They
are read-only and cheap to compute.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mcp import types

if TYPE_CHECKING:
    from engine.mcp.adapters import EngineServices

# Canonical URIs.  Defined on a namespace so they resolve as *value* patterns
# (dotted names) inside ``match`` — bare names there are capture patterns.
class _Uris:
    STRATEGIES = "nexus://strategies/catalog"
    SYMBOLS = "nexus://symbols/list"
    TIMEFRAMES = "nexus://timeframes/list"
    RISK_PARAMS = "nexus://risk-parameters/ranges"
    COST_DEFAULTS = "nexus://cost-model/defaults"


URI_STRATEGIES = _Uris.STRATEGIES
URI_SYMBOLS = _Uris.SYMBOLS
URI_TIMEFRAMES = _Uris.TIMEFRAMES
URI_RISK_PARAMS = _Uris.RISK_PARAMS
URI_COST_DEFAULTS = _Uris.COST_DEFAULTS

_SUPPORTED_TIMEFRAMES = ["1m", "5m", "15m", "1h", "1d", "1wk", "1mo"]

_DEFAULT_SYMBOLS = [
    {"symbol": "AAPL", "name": "Apple Inc.", "asset_class": "equity"},
    {"symbol": "MSFT", "name": "Microsoft Corp.", "asset_class": "equity"},
    {"symbol": "GOOGL", "name": "Alphabet Inc.", "asset_class": "equity"},
    {"symbol": "AMZN", "name": "Amazon.com Inc.", "asset_class": "equity"},
    {"symbol": "SPY", "name": "SPDR S&P 500 ETF", "asset_class": "etf"},
]

_RISK_RANGES = {
    "max_position_pct": {"min": 0.0, "max": 100.0, "default": 10.0, "unit": "% of portfolio"},
    "max_drawdown_pct": {"min": 0.0, "max": 100.0, "default": 20.0, "unit": "%"},
    "stop_loss_pct": {"min": 0.0, "max": 50.0, "default": 5.0, "unit": "%"},
    "take_profit_pct": {"min": 0.0, "max": 200.0, "default": 15.0, "unit": "%"},
    "max_open_positions": {"min": 1, "max": 100, "default": 10, "unit": "count"},
}


@dataclass(frozen=True)
class ResourceDefinition:
    uri: str
    name: str
    description: str
    mime_type: str = "application/json"


RESOURCE_DEFINITIONS: list[ResourceDefinition] = [
    ResourceDefinition(
        URI_STRATEGIES,
        "Strategy Catalog",
        "Catalog of installed strategies with parameters and metadata.",
    ),
    ResourceDefinition(
        URI_SYMBOLS,
        "Symbol Universe",
        "Commonly traded symbols available for backtesting and market data.",
    ),
    ResourceDefinition(
        URI_TIMEFRAMES,
        "Supported Timeframes",
        "Bar intervals accepted by market-data and backtest tools.",
    ),
    ResourceDefinition(
        URI_RISK_PARAMS,
        "Risk Parameter Ranges",
        "Configurable risk parameter bounds (min/max/default).",
    ),
    ResourceDefinition(
        URI_COST_DEFAULTS,
        "Cost Model Defaults",
        "Default transaction-cost-model parameters used by the engine.",
    ),
]


def list_resources() -> list[types.Resource]:
    return [
        types.Resource(
            uri=types.AnyUrl(defn.uri),
            name=defn.name,
            description=defn.description,
            mimeType=defn.mime_type,
        )
        for defn in RESOURCE_DEFINITIONS
    ]


def _strategy_catalog(services: EngineServices) -> list[dict[str, Any]]:
    from engine.plugins.registry import discover_strategies

    discovered = discover_strategies(services.strategies_dir)
    catalog = []
    for name, entry in discovered.items():
        manifest = entry.get("manifest") or {}
        catalog.append(
            {
                "name": name,
                "version": manifest.get("version"),
                "description": manifest.get("description", ""),
                "author": manifest.get("author"),
                "symbols": manifest.get("symbols", []),
                "timeframe": manifest.get("timeframe"),
                "parameters": manifest.get("parameters", {}),
            }
        )
    return catalog


def _cost_defaults(services: EngineServices) -> dict[str, Any]:
    cm = services.cost_model
    return {
        "commission_per_trade": cm.commission_per_trade,
        "spread_bps": cm.spread_bps,
        "slippage_bps": cm.slippage_bps,
        "exchange_fee_per_share": cm.exchange_fee_per_share,
        "short_term_tax_rate": cm.short_term_tax_rate,
        "long_term_tax_rate": cm.long_term_tax_rate,
        "qualified_dividend_rate": cm.qualified_dividend_rate,
        "ordinary_dividend_rate": cm.ordinary_dividend_rate,
        "wash_sale_window_days": cm.wash_sale_window_days,
    }


def read_resource(uri: str, services: EngineServices) -> types.ReadResourceResult:
    """Return the contents of the resource at ``uri``.

    Raises :class:`ValueError` for unknown URIs so the server layer can map it
    to an MCP resource-not-found response.
    """
    match uri:
        case _Uris.STRATEGIES:
            payload: Any = {"strategies": _strategy_catalog(services)}
        case _Uris.SYMBOLS:
            payload = {"symbols": _DEFAULT_SYMBOLS}
        case _Uris.TIMEFRAMES:
            payload = {"timeframes": _SUPPORTED_TIMEFRAMES}
        case _Uris.RISK_PARAMS:
            payload = {"parameters": _RISK_RANGES}
        case _Uris.COST_DEFAULTS:
            payload = _cost_defaults(services)
        case _:
            raise ValueError(f"Unknown resource URI: {uri}")

    text = json.dumps(payload, default=str, indent=2)
    return types.ReadResourceResult(
        contents=[
            types.TextResourceContents(
                uri=types.AnyUrl(uri),
                mimeType="application/json",
                text=text,
            )
        ]
    )


__all__ = [
    "RESOURCE_DEFINITIONS",
    "URI_COST_DEFAULTS",
    "URI_RISK_PARAMS",
    "URI_STRATEGIES",
    "URI_SYMBOLS",
    "URI_TIMEFRAMES",
    "list_resources",
    "read_resource",
]
