"""Nexus MCP server — transport-binding entry point.

This module is the missing runtime entry point for the MCP surface (see
``docs/known-limitations.md`` "MCP server cannot be started"). It instantiates
the official ``mcp.server`` :class:`~mcp.server.Server`, advertises tools, and
dispatches ``tools/call`` requests into the engine.

Scope of this module
--------------------
The full MCP surface (9 tools, auth/RBAC, rate limiting, cursor pagination,
result guards, resources) is already implemented and unit-tested in the
sibling modules (:mod:`engine.mcp.tool_definitions`, :mod:`engine.mcp.handlers`,
:mod:`engine.mcp.auth`, …). This entry point deliberately wires a **single**
tool — ``list_strategies`` — so it can be started and served today. The
``list_strategies`` tool queries the
:class:`~engine.plugins.registry.PluginRegistry` and returns the metadata for
every installed strategy. Binding the remaining tools/auth/rate-limiter
through the transport is tracked as follow-up work; one tool proves the server
can start, complete the MCP handshake, and answer a request.

Run it with::

    python -m engine.mcp.server            # stdio transport (default)
    NEXUS_MCP_TRANSPORT=stdio python -m engine.mcp.server
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import structlog
from mcp import types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from engine.mcp.config import mcp_settings
from engine.mcp.tool_definitions import LIST_STRATEGIES

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from engine.mcp.adapters import EngineServices
    from engine.plugins.registry import PluginRegistry

logger = structlog.get_logger()

SERVER_NAME = mcp_settings.server_name
SERVER_VERSION = mcp_settings.server_version

#: The single tool this minimal entry point advertises. Listed explicitly
#: (rather than via :func:`engine.mcp.tool_definitions.mcp_tools`) so the
#: minimal "server can start and serve" proof stays focused on one tool.
ADVERTISED_TOOLS: tuple[str, ...] = (LIST_STRATEGIES.name,)


# ── Tool business logic ──────────────────────────────────────────────────


def _summarize_strategy(name: str, manifest: dict[str, Any]) -> dict[str, Any]:
    """Project a raw strategy manifest onto the MCP-visible metadata shape."""
    return {
        "name": name,
        "version": manifest.get("version"),
        "description": manifest.get("description", ""),
        "author": manifest.get("author"),
        "symbols": manifest.get("symbols", []),
        "timeframe": manifest.get("timeframe"),
        "parameters": manifest.get("parameters", {}),
    }


def list_strategies_metadata(
    registry: PluginRegistry | None = None,
    strategies_dir: Path | None = None,
) -> dict[str, Any]:
    """Query the plugin registry and return metadata for every strategy.

    The authoritative list of installed strategy *names* comes from
    :meth:`PluginRegistry.list_strategies` — the registry is the source of
    truth for what is installed. Per-strategy metadata (version, description,
    author, symbols, timeframe, parameters) is read from each strategy's
    ``manifest.yaml`` via :func:`engine.plugins.registry.discover_strategies`,
    the same discovery routine that populates the registry, so the two never
    disagree.

    Parameters
    ----------
    registry:
        An existing :class:`PluginRegistry`. When omitted a fresh one is built
        from ``strategies_dir`` (or the engine default).
    strategies_dir:
        Directory holding ``<name>/manifest.yaml`` strategy manifests. Only
        used when ``registry`` is not supplied or to resolve manifests; passed
        straight through to :func:`discover_strategies`.

    Returns
    -------
    dict
        ``{"count": int, "strategies": list[dict]}`` — JSON-serialisable so it
        can flow straight into an MCP ``TextContent`` payload.
    """
    from engine.plugins.registry import PluginRegistry, discover_strategies

    reg = registry if registry is not None else PluginRegistry(strategies_dir)
    discovered = discover_strategies(strategies_dir)

    strategies = [
        _summarize_strategy(name, (discovered.get(name) or {}).get("manifest") or {})
        for name in reg.list_strategies()
    ]
    return {"count": len(strategies), "strategies": strategies}


# ── MCP request handlers (transport-agnostic) ────────────────────────────


async def handle_list_tools(
    _request: types.ListToolsRequest,
) -> types.ListToolsResult:
    """Advertise the tools this server can serve.

    Only :data:`LIST_STRATEGIES` is exposed by this minimal entry point; the
    full catalog lives in :mod:`engine.mcp.tool_definitions`.
    """
    return types.ListToolsResult(tools=[LIST_STRATEGIES.to_mcp_tool()])


async def handle_call_tool(
    name: str,
    _arguments: dict[str, Any] | None,
    *,
    services: EngineServices | None = None,
) -> list[types.TextContent]:
    """Dispatch a ``tools/call`` to the engine and return MCP content.

    Only ``list_strategies`` is handled here. Unknown tools raise
    :class:`ValueError`; the SDK converts unhandled exceptions into MCP error
    responses for the client.
    """
    if name != LIST_STRATEGIES.name:
        raise ValueError(f"Unknown tool: {name!r}")

    bound_services = services or _default_services()
    result = list_strategies_metadata(
        registry=bound_services.plugin_registry,
        strategies_dir=bound_services.strategies_dir,
    )
    logger.info("mcp_tool_called", tool=name, strategies=result["count"])
    return [
        types.TextContent(
            type="text",
            text=json.dumps(result, default=str, sort_keys=True),
        )
    ]


# ── Server factory ──────────────────────────────────────────────────────


def _default_services() -> EngineServices:
    """Build the default online :class:`EngineServices` (real registry)."""
    from engine.mcp.adapters import EngineServices

    return EngineServices()


def create_server(services: EngineServices | None = None) -> Server:
    """Build and return a configured :class:`mcp.server.Server`.

    The ``tools/call`` handler closes over ``services`` so callers (tests,
    scripts) can inject fakes; production callers leave it ``None`` to use the
    real engine registry. The returned server is ready to :meth:`Server.run`
    over any transport.
    """
    bound_services = services or _default_services()
    server: Server = Server(name=SERVER_NAME, version=SERVER_VERSION)

    @server.list_tools()
    async def _list_tools(request: types.ListToolsRequest) -> types.ListToolsResult:
        return await handle_list_tools(request)

    @server.call_tool()
    async def _call_tool(
        name: str, arguments: dict[str, Any] | None
    ) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
        return await handle_call_tool(name, arguments, services=bound_services)

    logger.info("mcp_server_created", tools=list(ADVERTISED_TOOLS))
    return server


# ── Transport entry points ──────────────────────────────────────────────


async def run_stdio(services: EngineServices | None = None) -> None:
    """Run the server over the stdio transport (default for local assistants).

    This is the transport Claude Desktop and other local MCP clients use: they
    spawn ``python -m engine.mcp.server`` and speak JSON-RPC over its
    stdin/stdout.
    """
    from mcp.server.stdio import stdio_server

    server = create_server(services)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name=SERVER_NAME,
                server_version=SERVER_VERSION,
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


async def run_http(services: EngineServices | None = None) -> None:  # pragma: no cover
    """Run the server over the HTTP (streamable-http/SSE) transport.

    Remote/SSE deployment is follow-up work; the stdio transport is the
    supported path today. Raised explicitly rather than silently misbehaving.
    """
    raise NotImplementedError(
        "HTTP/SSE transport binding is not implemented yet; use NEXUS_MCP_TRANSPORT=stdio."
    )


def main(_argv: Iterable[str] | None = None) -> None:
    """CLI entry point — ``python -m engine.mcp.server``.

    Selects the transport from :attr:`MCPServerSettings.transport`
    (``NEXUS_MCP_TRANSPORT`` env var; defaults to ``stdio``).
    """
    transport = mcp_settings.transport.lower()
    if transport == "http":
        asyncio.run(run_http())
    elif transport == "stdio":
        asyncio.run(run_stdio())
    else:
        raise ValueError(f"Unsupported NEXUS_MCP_TRANSPORT={transport!r}; use 'stdio' or 'http'.")


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = [
    "ADVERTISED_TOOLS",
    "SERVER_NAME",
    "SERVER_VERSION",
    "create_server",
    "handle_call_tool",
    "handle_list_tools",
    "list_strategies_metadata",
    "main",
    "run_http",
    "run_stdio",
]
