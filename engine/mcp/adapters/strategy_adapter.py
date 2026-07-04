"""Strategy enumeration and inspection adapters.

The strategy catalog is sourced exclusively from the
:class:`~engine.plugins.registry.PluginRegistry` carried on
:class:`~engine.mcp.adapters.EngineServices`. The registry discovers and
caches the parsed ``manifest.yaml`` for every installed strategy at
construction time, so these adapters never import plugin code and never
re-scan the strategies directory on each call — the registry is the single
source of truth for installed-strategy metadata.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from engine.mcp.adapters import EngineServices, to_jsonable
from engine.mcp.errors import NotFoundError, ValidationError

if TYPE_CHECKING:
    from engine.mcp.auth import AuthPrincipal


def _summarize(name: str, manifest: dict[str, Any]) -> dict[str, Any]:
    """Project a raw manifest dict into the LLM-facing strategy summary."""
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
    _principal: AuthPrincipal,
    _arguments: dict[str, Any],
) -> dict[str, Any]:
    """Enumerate every installed strategy as a list of summary dicts."""
    registry = services.plugin_registry
    strategies = [
        _summarize(name, registry.get_manifest(name) or {})
        for name in registry.list_strategies()
    ]
    return to_jsonable({"count": len(strategies), "strategies": strategies})


async def get_strategy_details(
    services: EngineServices,
    _principal: AuthPrincipal,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Return full metadata for a single strategy by its registry identifier.

    The ``strategy_name`` argument is the identifier returned by
    :func:`list_strategies` (the strategy directory / manifest ``name``). The
    lookup goes through the :class:`PluginRegistry`, so the same registry the
    rest of the engine uses is the source of truth here. Unknown identifiers
    raise :class:`~engine.mcp.errors.NotFoundError`.
    """
    name = arguments.get("strategy_name")
    if not isinstance(name, str) or not name.strip():
        raise ValidationError("strategy_name is required")
    name = name.strip()

    registry = services.plugin_registry
    manifest = registry.get_manifest(name)
    if manifest is None:
        raise NotFoundError(f"Strategy not found: {name}")

    detail = _summarize(name, manifest)
    # module_path is bonus metadata (code location); it is None-safe.
    detail["module_path"] = registry.get_module_path(name)
    return to_jsonable(detail)


__all__ = ["get_strategy_details", "list_strategies"]
