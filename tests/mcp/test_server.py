"""Unit tests for :mod:`engine.mcp.server`.

These tests prove the minimal MCP server can (a) query the plugin registry for
strategy metadata, (b) advertise the ``list_strategies`` tool, (c) answer a
``tools/call`` with that metadata, and (d) do all three end-to-end over an
in-memory MCP client/server pair — i.e. the server really starts and serves.

All tests are hermetic: they write throwaway strategy manifests into a
``tmp_path`` strategies directory, so they never touch the real strategy
catalog or the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from mcp import types

from engine.mcp.server import (
    SERVER_NAME,
    create_server,
    handle_call_tool,
    handle_list_tools,
    list_strategies_metadata,
)

# ── Fixtures / helpers ───────────────────────────────────────────────────


def _write_strategy(directory: Path, manifest: dict) -> None:
    """Materialise a discoverable strategy: a manifest + a ``strategy.py``."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "manifest.yaml").open("w") as f:
        yaml.dump(manifest, f)
    (directory / "strategy.py").write_text("class Strategy:\n    pass\n")


@pytest.fixture
def strategies_dir(tmp_path: Path) -> Path:
    base = tmp_path / "strategies"
    _write_strategy(
        base / "momentum",
        {
            "name": "momentum",
            "version": "2.1.0",
            "description": "Trend-following momentum strategy.",
            "author": "Nexus",
            "symbols": ["AAPL", "MSFT"],
            "timeframe": "1d",
            "parameters": {"lookback": 20},
        },
    )
    _write_strategy(
        base / "mean_reversion",
        {
            "name": "mean_reversion",
            "version": "1.0.0",
            "description": "Mean-reversion strategy.",
        },
    )
    return base


def _services(strategies_dir: Path):
    """Build a hermetic EngineServices pinned to the tmp strategies dir."""
    from engine.mcp.adapters import EngineServices
    from engine.plugins.registry import PluginRegistry

    return EngineServices.for_testing(
        plugin_registry=PluginRegistry(strategies_dir),
        strategies_dir=strategies_dir,
    )


# ── Core business logic ─────────────────────────────────────────────────


class TestListStrategiesMetadata:
    def test_returns_one_entry_per_strategy(self, strategies_dir):
        result = list_strategies_metadata(strategies_dir=strategies_dir)

        assert result["count"] == 2
        names = {entry["name"] for entry in result["strategies"]}
        assert names == {"momentum", "mean_reversion"}

    def test_projects_manifest_fields(self, strategies_dir):
        result = list_strategies_metadata(strategies_dir=strategies_dir)

        momentum = next(s for s in result["strategies"] if s["name"] == "momentum")
        assert momentum["version"] == "2.1.0"
        assert momentum["description"] == "Trend-following momentum strategy."
        assert momentum["author"] == "Nexus"
        assert momentum["symbols"] == ["AAPL", "MSFT"]
        assert momentum["timeframe"] == "1d"
        assert momentum["parameters"] == {"lookback": 20}

    def test_defaults_when_manifest_omits_optional_fields(self, strategies_dir):
        result = list_strategies_metadata(strategies_dir=strategies_dir)

        mean_rev = next(s for s in result["strategies"] if s["name"] == "mean_reversion")
        assert mean_rev["version"] == "1.0.0"
        assert mean_rev["author"] is None
        assert mean_rev["symbols"] == []
        assert mean_rev["parameters"] == {}

    def test_empty_strategies_dir(self, tmp_path):
        result = list_strategies_metadata(strategies_dir=tmp_path / "empty")

        assert result == {"count": 0, "strategies": []}

    def test_names_come_from_plugin_registry(self, strategies_dir):
        """The registry is the authoritative enumerator of installed plugins."""
        from engine.plugins.registry import PluginRegistry

        registry = PluginRegistry(strategies_dir)
        result = list_strategies_metadata(registry=registry, strategies_dir=strategies_dir)

        assert result["count"] == len(registry.list_strategies())
        assert set(registry.list_strategies()) == {
            s["name"] for s in result["strategies"]
        }


# ── MCP request handlers (called directly) ──────────────────────────────


class TestHandleListTools:
    async def test_advertises_single_list_strategies_tool(self):
        result = await handle_list_tools(None)  # request is unused by the handler

        assert isinstance(result, types.ListToolsResult)
        assert len(result.tools) == 1
        tool = result.tools[0]
        assert tool.name == "list_strategies"
        assert tool.description  # LLM-readable description is present
        # The advertised input schema matches the declarative definition.
        assert tool.inputSchema == {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
        # read-only, non-destructive hint is surfaced to assistants.
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False


class TestHandleCallTool:
    async def test_returns_strategy_metadata_as_text_content(self, strategies_dir):
        content = await handle_call_tool("list_strategies", {}, services=_services(strategies_dir))

        assert len(content) == 1
        assert isinstance(content[0], types.TextContent)

        payload = json.loads(content[0].text)
        assert payload["count"] == 2
        assert {s["name"] for s in payload["strategies"]} == {
            "momentum",
            "mean_reversion",
        }
        # The payload is valid JSON (i.e. already serialised).
        assert isinstance(content[0].text, str)

    async def test_accepts_missing_arguments(self, strategies_dir):
        # MCP clients may omit arguments for a no-arg tool.
        content = await handle_call_tool(
            "list_strategies", None, services=_services(strategies_dir)
        )

        assert json.loads(content[0].text)["count"] == 2

    async def test_unknown_tool_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown tool"):
            await handle_call_tool("does_not_exist", {})


# ── Server factory + end-to-end over an in-memory MCP client ────────────


class TestCreateServer:
    def test_returns_named_server(self, strategies_dir):
        server = create_server(_services(strategies_dir))

        assert server.name == SERVER_NAME

    async def test_end_to_start_and_serve(self, strategies_dir):
        """The server starts, completes the handshake, and serves a tool call."""
        from mcp.shared.memory import create_connected_server_and_client_session

        server = create_server(_services(strategies_dir))

        async with create_connected_server_and_client_session(server) as session:
            # tools/list
            tools_result = await session.list_tools()
            assert [t.name for t in tools_result.tools] == ["list_strategies"]

            # tools/call
            call_result = await session.call_tool("list_strategies", {})
            assert call_result.isError is False
            assert call_result.content, "expected at least one content block"

            payload = json.loads(call_result.content[0].text)
            assert payload["count"] == 2
            assert {s["name"] for s in payload["strategies"]} == {
                "momentum",
                "mean_reversion",
            }

    async def test_unknown_tool_returns_mcp_error(self, strategies_dir):
        from mcp.shared.memory import create_connected_server_and_client_session

        server = create_server(_services(strategies_dir))

        async with create_connected_server_and_client_session(server) as session:
            result = await session.call_tool("nope", {})

            assert result.isError is True
