"""Strategy enumeration adapters.

Uses :func:`engine.plugins.registry.discover_strategies`, which reads
``manifest.yaml`` files from the strategies directory — so the MCP server can
enumerate the strategy catalog without instantiating any plugin code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from engine.mcp.adapters import EngineServices, to_jsonable
from engine.mcp.errors import NotFoundError

if TYPE_CHECKING:
    from engine.mcp.auth import AuthPrincipal


def _discover(services: EngineServices) -> dict[str, dict[str, Any]]:
    from engine.plugins.registry import discover_strategies

    return discover_strategies(services.strategies_dir)


def _summarize(name: str, manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "version": manifest.get("version"),
        "description": manifest.get("description", ""),
        "author": manifest.get("author"),
        "symbols": manifest.get("symbols", []),
        "timeframe": manifest.get("timeframe"),
        "parameters": manifest.get("parameters", {}),
    }


async def list_strategies(
    services: EngineServices,
    principal: AuthPrincipal,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    discovered = _discover(services)
    strategies = [_summarize(name, entry.get("manifest") or {}) for name, entry in discovered.items()]
    return to_jsonable({"count": len(strategies), "strategies": strategies})


async def get_strategy_details(
    services: EngineServices,
    principal: AuthPrincipal,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    name = arguments.get("strategy_name")
    if not name:
        from engine.mcp.errors import ValidationError

        raise ValidationError("strategy_name is required")
    discovered = _discover(services)
    entry = discovered.get(name)
    if entry is None:
        raise NotFoundError(f"Strategy not found: {name}")
    manifest = entry.get("manifest") or {}
    detail = _summarize(name, manifest)
    detail["module_path"] = entry.get("module_path")
    return to_jsonable(detail)


__all__ = ["get_strategy_details", "list_strategies"]
