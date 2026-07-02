"""Tests for :class:`engine.mcp.server.NexusMCPServer`.

Covers the four required areas:

* **Server initialization** — identity, handler registration, capabilities.
* **Tool listing** — the ``tools/list`` handler advertises ``list_strategies``
  with the right name / schema / annotations.
* **Strategy metadata response shape** — calling ``list_strategies`` against a
  populated registry returns the full expected metadata per strategy.
* **Empty registry edge case** — ``count == 0`` and an empty ``strategies``
  list when the registry has discovered nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from mcp import types
from pydantic import ValidationError

from engine.mcp.server import (
    LIST_STRATEGIES_DESCRIPTION,
    LIST_STRATEGIES_NAME,
    ListStrategiesInput,
    ListStrategiesOutput,
    NexusMCPServer,
    StrategyMetadata,
    list_strategies_input_schema,
    list_strategies_output_schema,
)
from engine.plugins.registry import PluginRegistry

# ── Fixtures / helpers ──


def _write_strategy(directory: Path, manifest: dict, *, with_code: bool = True) -> None:
    """Create a strategy directory with a ``manifest.yaml`` (and optional code)."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "manifest.yaml").open("w") as fh:
        yaml.safe_dump(manifest, fh)
    if with_code:
        (directory / "strategy.py").write_text("class Strategy:\n    pass\n")


@pytest.fixture
def empty_registry(tmp_path: Path) -> PluginRegistry:
    """A registry whose strategies directory contains no strategies."""
    return PluginRegistry(tmp_path / "strategies")


@pytest.fixture
def populated_registry(tmp_path: Path) -> PluginRegistry:
    """A registry discovering two well-formed strategies."""
    root = tmp_path / "strategies"
    _write_strategy(
        root / "momentum",
        {
            "version": "1.2.0",
            "description": "Trend-following momentum strategy.",
            "author": "Nexus Quant",
            "symbols": ["AAPL", "MSFT"],
            "timeframe": "1d",
            "parameters": {"lookback": 20, "threshold": 0.02},
        },
    )
    _write_strategy(
        root / "mean_reversion",
        {
            "version": "0.4.1",
            "description": "Mean-reversion on a z-score band.",
            # author intentionally omitted — should degrade to None, not raise
            "symbols": ["SPY"],
            "parameters": {"band": 2.0},
        },
    )
    return PluginRegistry(root)


@pytest.fixture
def server(empty_registry: PluginRegistry) -> NexusMCPServer:
    """A ``NexusMCPServer`` bound to an empty registry with explicit identity."""
    return NexusMCPServer(
        empty_registry,
        name="nexus-test-server",
        version="9.9.9",
        instructions="test instructions",
    )


# ── 1. Server initialization ──


class TestServerInitialization:
    def test_identity_propagated_to_wrapped_server(self, server):
        assert server.name == "nexus-test-server"
        assert server.version == "9.9.9"
        assert server.server.name == "nexus-test-server"
        assert server.server.version == "9.9.9"

    def test_registry_is_injected(self, populated_registry):
        server = NexusMCPServer(populated_registry)
        # The exact same instance is retained — no re-discovery happens.
        assert server.plugin_registry is populated_registry

    def test_defaults_pulled_from_settings_when_omitted(self, empty_registry):
        from engine.mcp.config import mcp_settings

        server = NexusMCPServer(empty_registry)
        assert server.name == mcp_settings.server_name
        assert server.version == mcp_settings.server_version

    def test_tool_names_advertises_exactly_one_tool(self, server):
        assert server.tool_names == [LIST_STRATEGIES_NAME]

    def test_required_handlers_registered_on_wrapped_server(self, server):
        # The mcp SDK stores request handlers keyed by request type. Both
        # tools/list and tools/call must be present for a functional server.
        handlers = server.server.request_handlers
        assert types.ListToolsRequest in handlers
        assert types.CallToolRequest in handlers

    def test_initialization_options_advertise_tools_capability(self, server):
        options = server.create_initialization_options()
        assert options.server_name == "nexus-test-server"
        assert options.server_version == "9.9.9"
        assert options.instructions == "test instructions"
        # Tools capability is set iff the list_tools handler is registered.
        assert options.capabilities.tools is not None
        # No prompts/resources bound in this minimal server.
        assert options.capabilities.prompts is None
        assert options.capabilities.resources is None

    def test_registry_auto_constructed_when_omitted(self, tmp_path, monkeypatch):
        # When no registry is given, the server builds one from strategies_dir.
        root = tmp_path / "strategies"
        _write_strategy(
            root / "solo",
            {"version": "0.1.0", "description": "solo strat"},
        )
        server = NexusMCPServer(strategies_dir=root)
        assert isinstance(server.plugin_registry, PluginRegistry)
        assert server.plugin_registry.list_strategies() == ["solo"]


# ── 2. Tool listing ──


class TestToolListing:
    def test_list_tools_returns_single_list_strategies_tool(self, server):
        tools = server.list_tools()
        assert len(tools) == 1
        tool = tools[0]
        assert isinstance(tool, types.Tool)
        assert tool.name == LIST_STRATEGIES_NAME

    def test_list_strategies_tool_metadata(self, server):
        tool = server.list_tools()[0]
        assert tool.description == LIST_STRATEGIES_DESCRIPTION
        # Read-only, non-destructive, idempotent, closed-world (catalog only).
        ann = tool.annotations
        assert ann is not None
        assert ann.readOnlyHint is True
        assert ann.destructiveHint is False
        assert ann.idempotentHint is True
        assert ann.openWorldHint is False

    def test_input_schema_is_no_arg_object(self, server):
        schema = server.list_tools()[0].inputSchema
        assert schema["type"] == "object"
        assert schema["properties"] == {}
        assert schema["additionalProperties"] is False

    def test_output_schema_derived_from_pydantic_model(self, server):
        schema = server.list_tools()[0].outputSchema
        assert schema is not None
        # Derived from ListStrategiesOutput → must declare the count/strategies
        # properties and the nested StrategyMetadata under $defs.
        assert schema["type"] == "object"
        assert "count" in schema["properties"]
        assert "strategies" in schema["properties"]
        assert "$defs" in schema
        assert "StrategyMetadata" in schema["$defs"]

    @pytest.mark.asyncio
    async def test_registered_list_tools_handler_unwraps_to_tool(self, server):
        server_result = await server.server.request_handlers[types.ListToolsRequest](None)
        # ServerResult is a RootModel; .root is the concrete result type.
        list_tools_result = server_result.root
        assert isinstance(list_tools_result, types.ListToolsResult)
        assert len(list_tools_result.tools) == 1
        assert list_tools_result.tools[0].name == LIST_STRATEGIES_NAME


# ── 3. Strategy metadata response shape ──


class TestListStrategiesResponseShape:
    @pytest.mark.asyncio
    async def test_count_matches_registry(self, populated_registry):
        server = NexusMCPServer(populated_registry)
        result = await server.dispatch(LIST_STRATEGIES_NAME, {})
        assert result["count"] == 2
        assert len(result["strategies"]) == 2

    @pytest.mark.asyncio
    async def test_each_strategy_has_expected_keys(self, populated_registry):
        result = await NexusMCPServer(populated_registry).dispatch(LIST_STRATEGIES_NAME, {})
        expected_keys = {
            "name",
            "version",
            "description",
            "author",
            "symbols",
            "timeframe",
            "parameters",
        }
        for entry in result["strategies"]:
            assert set(entry) == expected_keys

    @pytest.mark.asyncio
    async def test_strategy_field_values(self, populated_registry):
        result = await NexusMCPServer(populated_registry).dispatch(LIST_STRATEGIES_NAME, {})
        by_name = {s["name"]: s for s in result["strategies"]}

        momentum = by_name["momentum"]
        assert momentum["version"] == "1.2.0"
        assert momentum["description"] == "Trend-following momentum strategy."
        assert momentum["author"] == "Nexus Quant"
        assert momentum["symbols"] == ["AAPL", "MSFT"]
        assert momentum["timeframe"] == "1d"
        assert momentum["parameters"] == {"lookback": 20, "threshold": 0.02}

    @pytest.mark.asyncio
    async def test_missing_manifest_fields_degrade_to_defaults(self, populated_registry):
        result = await NexusMCPServer(populated_registry).dispatch(LIST_STRATEGIES_NAME, {})
        by_name = {s["name"]: s for s in result["strategies"]}

        mr = by_name["mean_reversion"]
        # author + timeframe were omitted from the manifest.yaml.
        assert mr["author"] is None
        assert mr["timeframe"] is None
        assert mr["description"] == "Mean-reversion on a z-score band."
        assert mr["symbols"] == ["SPY"]
        assert mr["parameters"] == {"band": 2.0}

    @pytest.mark.asyncio
    async def test_result_validates_against_output_model(self, populated_registry):
        result = await NexusMCPServer(populated_registry).dispatch(LIST_STRATEGIES_NAME, {})
        # The raw dict round-trips back through the output model cleanly.
        parsed = ListStrategiesOutput.model_validate(result)
        assert parsed.count == 2
        assert all(isinstance(s, StrategyMetadata) for s in parsed.strategies)
        assert {s.name for s in parsed.strategies} == {"momentum", "mean_reversion"}

    @pytest.mark.asyncio
    async def test_full_call_tool_handler_flow(self, populated_registry):
        # End-to-end through the SDK-registered call_tool handler: exercises
        # input validation, structured-content normalization, and output
        # schema validation in one shot.
        server = NexusMCPServer(populated_registry)
        request = types.CallToolRequest(
            params=types.CallToolRequestParams(name=LIST_STRATEGIES_NAME),
        )
        server_result = await server.server.request_handlers[types.CallToolRequest](request)
        call_result = server_result.root
        assert isinstance(call_result, types.CallToolResult)
        assert call_result.isError is False
        assert call_result.structuredContent is not None
        assert call_result.structuredContent["count"] == 2
        assert len(call_result.structuredContent["strategies"]) == 2
        # Unstructured text content mirrors the structured payload.
        assert call_result.content
        assert call_result.content[0].type == "text"


# ── 4. Empty registry edge case ──


class TestEmptyRegistry:
    @pytest.mark.asyncio
    async def test_empty_registry_returns_zero_count(self, empty_registry):
        result = await NexusMCPServer(empty_registry).dispatch(LIST_STRATEGIES_NAME, {})
        assert result["count"] == 0
        assert result["strategies"] == []

    @pytest.mark.asyncio
    async def test_empty_registry_validates_against_output_model(self, empty_registry):
        result = await NexusMCPServer(empty_registry).dispatch(LIST_STRATEGIES_NAME, {})
        parsed = ListStrategiesOutput.model_validate(result)
        assert parsed.count == 0
        assert parsed.strategies == []

    @pytest.mark.asyncio
    async def test_empty_registry_call_tool_handler_flow(self, empty_registry):
        server = NexusMCPServer(empty_registry)
        request = types.CallToolRequest(
            params=types.CallToolRequestParams(name=LIST_STRATEGIES_NAME),
        )
        server_result = await server.server.request_handlers[types.CallToolRequest](request)
        call_result = server_result.root
        assert call_result.isError is False
        assert call_result.structuredContent == {"count": 0, "strategies": []}


# ── Dispatch / schema helpers ──


class TestDispatchAndSchemas:
    @pytest.mark.asyncio
    async def test_unknown_tool_raises_value_error(self, server):
        with pytest.raises(ValueError, match="Unknown tool"):
            await server.dispatch("not_a_tool", {})

    @pytest.mark.asyncio
    async def test_list_strategies_ignores_arguments(self, populated_registry):
        # list_strategies takes no args; stray arguments must not break it.
        result = await NexusMCPServer(populated_registry).dispatch(
            LIST_STRATEGIES_NAME, {"unexpected": True}
        )
        assert result["count"] == 2

    def test_input_schema_helper_matches_tool(self, server):
        assert list_strategies_input_schema() == server.list_tools()[0].inputSchema

    def test_output_schema_helper_matches_tool(self, server):
        assert list_strategies_output_schema() == server.list_tools()[0].outputSchema

    def test_input_model_rejects_extra_fields(self):
        with pytest.raises(ValidationError):
            ListStrategiesInput.model_validate({"bogus": 1})

    def test_output_model_count_must_be_non_negative(self):
        with pytest.raises(ValidationError):
            ListStrategiesOutput(count=-1, strategies=[])
