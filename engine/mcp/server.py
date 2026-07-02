"""Transport-binding entry point: :class:`NexusMCPServer`.

This module is the glue that turns Nexus's MCP *primitives* (the adapters,
``dispatch_tool`` router, auth, pagination) into a runnable MCP server. It
wraps the ``mcp`` SDK's low-level :class:`~mcp.server.lowlevel.Server` and
binds Nexus engine capabilities to it by registering ``tools/list`` and
``tools/call`` handlers.

Scope note
----------
This module currently binds **exactly one** tool — ``list_strategies`` — which
enumerates installed strategies by reading the
:class:`~engine.plugins.registry.PluginRegistry`. Additional tools (portfolio,
backtest, market data) land in follow-up cycles; until then the server is
intentionally minimal but fully functional and self-describing.

The Pydantic models :class:`ListStrategiesInput` /
:class:`StrategyMetadata` / :class:`ListStrategiesOutput` are the single
source of truth for the tool's input and output schemas, and the tool's MCP
``inputSchema`` / ``outputSchema`` are derived directly from them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mcp import types
from mcp.server.lowlevel import Server
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from engine.plugins.registry import PluginRegistry

# ── Tool identity ──

LIST_STRATEGIES_NAME = "list_strategies"
LIST_STRATEGIES_DESCRIPTION = (
    "Enumerate all installed trading strategies with their version, "
    "description, author, supported symbols, timeframe, and default "
    "parameters. Read-only; takes no arguments."
)


# ── Pydantic input/output schemas ──


class ListStrategiesInput(BaseModel):
    """Input schema for the ``list_strategies`` tool.

    The tool takes no arguments. The empty model makes the contract explicit
    and rejects any caller-supplied properties via ``extra="forbid"``.
    """

    model_config = ConfigDict(extra="forbid")


class StrategyMetadata(BaseModel):
    """Metadata describing a single installed strategy."""

    name: str
    version: str | None = None
    description: str = ""
    author: str | None = None
    symbols: list[str] = Field(default_factory=list)
    timeframe: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)


class ListStrategiesOutput(BaseModel):
    """Structured output of the ``list_strategies`` tool."""

    count: int = Field(ge=0, description="Number of strategies returned.")
    strategies: list[StrategyMetadata] = Field(default_factory=list)


# ── Schema derivation helpers ──

# A minimal, dependency-free JSON Schema for a no-argument tool. We hand-write
# it (rather than ``ListStrategiesInput.model_json_schema()``) so it stays free
# of pydantic-specific noise (``title``) — exactly what MCP clients expect.
_LIST_STRATEGIES_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


def list_strategies_input_schema() -> dict[str, Any]:
    """Return the JSON Schema for the ``list_strategies`` input."""
    return _LIST_STRATEGIES_INPUT_SCHEMA


def list_strategies_output_schema() -> dict[str, Any]:
    """Return the JSON Schema for the ``list_strategies`` output.

    Derived from :class:`ListStrategiesOutput` so the model is the single
    source of truth. ``$defs`` for the nested :class:`StrategyMetadata` are
    preserved — JSON Schema validators (and the MCP SDK) resolve them.
    """
    return ListStrategiesOutput.model_json_schema()


def _build_list_strategies_tool() -> types.Tool:
    """Build the MCP :class:`~mcp.types.Tool` for ``list_strategies``."""
    return types.Tool(
        name=LIST_STRATEGIES_NAME,
        description=LIST_STRATEGIES_DESCRIPTION,
        inputSchema=list_strategies_input_schema(),
        outputSchema=list_strategies_output_schema(),
        annotations=types.ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )


class NexusMCPServer:
    """Nexus MCP server bound to the ``list_strategies`` tool.

    Wraps the ``mcp`` SDK :class:`~mcp.server.lowlevel.Server`, registering the
    ``tools/list`` and ``tools/call`` handlers needed to advertise and execute
    the single ``list_strategies`` tool. Strategy data is sourced from the
    injected (or auto-constructed) :class:`PluginRegistry`.

    The registry is *injectable*: tests pass a registry pointed at a temporary
    strategies directory; production deployments let the default factory
    discover the real strategies tree.
    """

    def __init__(
        self,
        plugin_registry: PluginRegistry | None = None,
        strategies_dir: Path | None = None,
        *,
        name: str | None = None,
        version: str | None = None,
        instructions: str | None = None,
    ) -> None:
        from engine.mcp.config import mcp_settings
        from engine.plugins.registry import PluginRegistry as _PluginRegistry

        # If a registry is supplied, ``strategies_dir`` is ignored — the
        # registry already encapsulates its own discovery root.
        self.plugin_registry: PluginRegistry = (
            plugin_registry if plugin_registry is not None else _PluginRegistry(strategies_dir)
        )

        self._server: Server[Any, Any] = Server(
            name or mcp_settings.server_name,
            version or mcp_settings.server_version,
            instructions or mcp_settings.instructions,
        )
        self._register_handlers()

    # ── public surface ──

    @property
    def server(self) -> Server[Any, Any]:
        """The wrapped ``mcp`` low-level server (used to bind a transport)."""
        return self._server

    @property
    def name(self) -> str:
        return self._server.name

    @property
    def version(self) -> str | None:
        return self._server.version

    @property
    def tool_names(self) -> list[str]:
        """Names of the tools this server binds (introspectable for tests)."""
        return [LIST_STRATEGIES_NAME]

    def create_initialization_options(self) -> Any:
        """Build :class:`~mcp.server.models.InitializationOptions` for handshake.

        Returns an options object whose capabilities advertise tool support
        (because the ``tools/list`` handler is registered).
        """
        return self._server.create_initialization_options()

    def list_tools(self) -> list[types.Tool]:
        """Return the MCP tool definitions this server advertises."""
        return [_build_list_strategies_tool()]

    # ── handler wiring ──

    def _register_handlers(self) -> None:
        server = self._server

        @server.list_tools()
        async def _list_tools() -> list[types.Tool]:
            return self.list_tools()

        @server.call_tool()
        async def _call_tool(
            tool_name: str, arguments: Mapping[str, Any] | None
        ) -> dict[str, Any]:
            return await self.dispatch(tool_name, dict(arguments or {}))

    # ── tool execution ──

    async def dispatch(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Execute a bound tool by name, returning its structured output.

        Unknown tool names raise :class:`ValueError`. The single bound tool,
        ``list_strategies``, takes no arguments and ignores anything passed.
        """
        if name == LIST_STRATEGIES_NAME:
            return await self._list_strategies(arguments)
        raise ValueError(f"Unknown tool: {name!r}")

    async def _list_strategies(self, _arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Read strategy metadata from the registry and return validated output.

        Returns the :class:`ListStrategiesOutput` payload as a plain dict so
        the MCP SDK can serialize it into ``structuredContent`` and validate
        it against the tool's declared ``outputSchema``.
        """
        metadata = self.plugin_registry.strategy_metadata()
        strategies = [StrategyMetadata(**entry) for entry in metadata]
        output = ListStrategiesOutput(count=len(strategies), strategies=strategies)
        return output.model_dump()


__all__ = [
    "LIST_STRATEGIES_DESCRIPTION",
    "LIST_STRATEGIES_NAME",
    "ListStrategiesInput",
    "ListStrategiesOutput",
    "NexusMCPServer",
    "StrategyMetadata",
    "list_strategies_input_schema",
    "list_strategies_output_schema",
]
