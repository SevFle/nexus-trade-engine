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
    """Project a raw manifest dict into the LLM-facing strategy summary.

    Uses ``or`` rather than the second positional default of :meth:`dict.get`
    so that manifest fields set to an explicit ``None`` (a YAML ``null``) are
    normalised to the documented empty defaults instead of leaking ``None``
    for description / symbols / parameters, which the MCP schema declares as
    string / array / object respectively.
    """
    return {
        "name": name,
        "version": manifest.get("version") or None,
        "description": manifest.get("description") or "",
        "author": manifest.get("author") or None,
        "symbols": manifest.get("symbols") or [],
        "timeframe": manifest.get("timeframe") or None,
        "parameters": manifest.get("parameters") or {},
    }


async def list_strategies(
    services: EngineServices,
    _principal: AuthPrincipal,
    _arguments: dict[str, Any],
) -> dict[str, Any]:
    """Enumerate every installed strategy as a list of summary dicts."""
    registry = services.plugin_registry
    strategies = [
        _summarize(name, registry.get_manifest(name) or {}) for name in registry.list_strategies()
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
    # NOTE: module_path (the on-disk code location) is intentionally *not*
    # surfaced here. Exposing it would leak absolute filesystem paths to LLM
    # clients, and the basename is constant (``strategy.py``) for every
    # strategy so it carries no useful identifying information either. The
    # strategy ``name`` above is the stable identifier callers need.
    return to_jsonable(detail)


__all__ = ["get_strategy_details", "list_strategies"]
