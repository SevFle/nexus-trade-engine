"""Unit tests for the strategy MCP adapters.

Covers :func:`engine.mcp.adapters.strategy_adapter.list_strategies`,
:func:`engine.mcp.adapters.strategy_adapter.search_strategies`, and
:func:`engine.mcp.adapters.strategy_adapter.get_strategy_details`, plus the
:func:`~engine.mcp.handlers.dispatch_tool` integration for the
``get_strategy_details`` and ``search_strategies`` routes.

The :class:`~engine.plugins.registry.PluginRegistry` is mocked everywhere so
no manifest files, plugin code, or disk I/O are required. This pins the
adapter contract:

* ``list_strategies`` summaries one entry per registered strategy.
* ``get_strategy_details`` returns name / description / version / parameters
  (plus author, symbols, timeframe, module_path) for a known strategy and
  consults the registry — making the registry the single lookup source.
* unknown strategy identifiers raise :class:`NotFoundError`.
* missing / empty ``strategy_name`` raises :class:`ValidationError` without
  consulting the registry.
* :func:`dispatch_tool` routes, validates required args, and propagates the
  not-found error unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from engine.mcp.adapters import EngineServices
from engine.mcp.adapters.strategy_adapter import (
    get_strategy_details,
    list_strategies,
    search_strategies,
)
from engine.mcp.auth import AuthPrincipal
from engine.mcp.errors import MCPError, NotFoundError, ValidationError
from engine.mcp.handlers import dispatch_tool

# ── Shared constants ─────────────────────────────────────────────────────── #
PRINCIPAL = AuthPrincipal(user_id="quant-1", role="viewer", auth_method="jwt")

MANIFEST_MOMENTUM: dict[str, Any] = {
    "name": "momentum",
    "version": "1.2.0",
    "description": "Trend-following momentum strategy.",
    "author": "nexus-team",
    "symbols": ["AAPL", "MSFT"],
    "timeframe": "1d",
    "parameters": {"lookback": 20, "threshold": 0.02},
    # Filterable metadata (top-level form):
    "tags": ["momentum", "trend-following"],
    "risk_level": "medium",
    "asset_class": "equity",
}

MANIFEST_MEANREV: dict[str, Any] = {
    "name": "mean_reversion",
    "version": "0.4.1",
    "description": "Bollinger-band mean reversion.",
    "author": "nexus-team",
    "symbols": ["GOOGL"],
    "timeframe": "1h",
    "parameters": {"window": 14, "num_std": 2.0},
    # Filterable metadata stored under ``marketplace`` (alternate schema):
    "marketplace": {
        "tags": ["mean-reversion", "low-frequency"],
        "risk_level": "low",
        "preferred_assets": ["US equities"],
    },
}

MANIFEST_CRYPTO: dict[str, Any] = {
    "name": "crypto_breakout",
    "version": "0.2.0",
    "description": "Breakout strategy for cryptocurrency pairs.",
    "author": "nexus-team",
    "symbols": ["BTC/USD"],
    "timeframe": "1h",
    "parameters": {"breakout_window": 24},
    # Filterable metadata (top-level, multi-asset list form):
    "tags": ["momentum", "breakout", "high-frequency"],
    "risk_level": "high",
    "asset_classes": ["crypto", "forex"],
}

# Convenience: every fixture, in a stable order, for "returns all" assertions.
ALL_FIXTURES: dict[str, dict[str, Any]] = {
    "momentum": MANIFEST_MOMENTUM,
    "mean_reversion": MANIFEST_MEANREV,
    "crypto_breakout": MANIFEST_CRYPTO,
}


# ── Helpers ──────────────────────────────────────────────────────────────── #
def _make_registry(strategies: dict[str, dict[str, Any]]) -> MagicMock:
    """Build a mock registry whose ``get_manifest``/``get_module_path`` map
    over ``strategies`` (name → manifest). ``list_strategies`` returns the
    keys in insertion order."""
    spec = MagicMock(name="PluginRegistry")
    spec.list_strategies.return_value = list(strategies)
    spec.get_manifest.side_effect = strategies.get
    spec.get_module_path.side_effect = (
        lambda name: f"/strategies/{name}/strategy.py" if name in strategies else None
    )
    return spec


def _make_services(registry: MagicMock | None = None) -> EngineServices:
    return EngineServices(
        plugin_registry=registry if registry is not None else _make_registry({}),
        strategies_dir=Path("/nonexistent"),
    )


# ── 1. list_strategies ──────────────────────────────────────────────────── #
async def test_list_strategies_summarises_each_registered_strategy():
    registry = _make_registry(
        {"momentum": MANIFEST_MOMENTUM, "mean_reversion": MANIFEST_MEANREV}
    )
    services = _make_services(registry)

    result = await list_strategies(services, PRINCIPAL, {})

    assert result["count"] == 2
    names = {s["name"] for s in result["strategies"]}
    assert names == {"momentum", "mean_reversion"}
    # Each summary carries the documented metadata fields.
    momentum = next(s for s in result["strategies"] if s["name"] == "momentum")
    assert momentum["version"] == "1.2.0"
    assert momentum["description"] == "Trend-following momentum strategy."
    assert momentum["author"] == "nexus-team"
    assert momentum["symbols"] == ["AAPL", "MSFT"]
    assert momentum["timeframe"] == "1d"
    assert momentum["parameters"] == {"lookback": 20, "threshold": 0.02}
    # The list summary intentionally omits the code path.
    assert "module_path" not in momentum


async def test_list_strategies_empty_registry_returns_empty_list():
    services = _make_services(_make_registry({}))
    result = await list_strategies(services, PRINCIPAL, {})
    assert result == {"count": 0, "strategies": []}


async def test_list_strategies_uses_registry_not_strategies_dir():
    """The adapter must source the catalog from the injected registry."""
    registry = _make_registry({"momentum": MANIFEST_MOMENTUM})
    # strategies_dir points nowhere — it must be ignored in favour of the
    # registry, proving there is no latent discover_strategies() call.
    services = _make_services(registry)

    await list_strategies(services, PRINCIPAL, {})

    registry.list_strategies.assert_called_once_with()
    registry.get_manifest.assert_called_once_with("momentum")


# ── 2. get_strategy_details — happy path ────────────────────────────────── #
async def test_get_strategy_details_returns_full_metadata():
    registry = _make_registry({"momentum": MANIFEST_MOMENTUM})
    services = _make_services(registry)

    detail = await get_strategy_details(
        services, PRINCIPAL, {"strategy_name": "momentum"}
    )

    # Required by the task: name, description, version, parameters.
    assert detail["name"] == "momentum"
    assert detail["description"] == "Trend-following momentum strategy."
    assert detail["version"] == "1.2.0"
    assert detail["parameters"] == {"lookback": 20, "threshold": 0.02}
    # Extra metadata projected from the manifest.
    assert detail["author"] == "nexus-team"
    assert detail["symbols"] == ["AAPL", "MSFT"]
    assert detail["timeframe"] == "1d"
    # The per-strategy detail additionally surfaces the code location.
    assert detail["module_path"] == "/strategies/momentum/strategy.py"

    # The lookup went through the registry — single source of truth.
    registry.get_manifest.assert_called_once_with("momentum")
    registry.get_module_path.assert_called_once_with("momentum")


async def test_get_strategy_details_handles_minimal_manifest():
    """A manifest missing optional fields degrades gracefully (defaults)."""
    registry = _make_registry({"bare": {"name": "bare", "version": "0.1.0"}})
    services = _make_services(registry)

    detail = await get_strategy_details(services, PRINCIPAL, {"strategy_name": "bare"})

    assert detail["name"] == "bare"
    assert detail["version"] == "0.1.0"
    assert detail["description"] == ""
    assert detail["author"] is None
    assert detail["symbols"] == []
    assert detail["timeframe"] is None
    assert detail["parameters"] == {}
    assert detail["module_path"] == "/strategies/bare/strategy.py"


# ── 3. search_strategies — no filter returns all ──────────────────────── #
async def test_search_no_filter_returns_every_strategy():
    registry = _make_registry(ALL_FIXTURES)
    services = _make_services(registry)

    result = await search_strategies(services, PRINCIPAL, {})

    assert result["count"] == 3
    assert {s["name"] for s in result["strategies"]} == set(ALL_FIXTURES)
    # Summaries carry the filterable attributes too.
    crypto = next(s for s in result["strategies"] if s["name"] == "crypto_breakout")
    assert crypto["tags"] == ["momentum", "breakout", "high-frequency"]
    assert crypto["risk_level"] == "high"
    assert crypto["asset_class"] == ["crypto", "forex"]


async def test_search_empty_registry_returns_empty_list():
    services = _make_services(_make_registry({}))
    result = await search_strategies(services, PRINCIPAL, {})
    assert result == {"count": 0, "strategies": []}


# ── 4. search_strategies — individual filters ──────────────────────────── #
async def test_search_query_matches_name_substring_case_insensitive():
    registry = _make_registry(ALL_FIXTURES)
    services = _make_services(registry)

    result = await search_strategies(services, PRINCIPAL, {"query": "MOMENTUM"})

    # Only the momentum strategy has "momentum" in its name/description.
    assert result["count"] == 1
    assert result["strategies"][0]["name"] == "momentum"


async def test_search_query_matches_description_substring():
    """query is a free-text search hitting name AND description."""
    registry = _make_registry(ALL_FIXTURES)
    services = _make_services(registry)

    result = await search_strategies(services, PRINCIPAL, {"query": "strategy"})

    # 'strategy' appears in momentum and crypto descriptions, but NOT in the
    # mean_reversion description ("Bollinger-band mean reversion.").
    names = {s["name"] for s in result["strategies"]}
    assert names == {"momentum", "crypto_breakout"}


async def test_search_tags_requires_all_requested_tags():
    registry = _make_registry(ALL_FIXTURES)
    services = _make_services(registry)

    # Both momentum and crypto_breakout are tagged 'momentum'.
    result = await search_strategies(services, PRINCIPAL, {"tags": ["momentum"]})
    assert {s["name"] for s in result["strategies"]} == {"momentum", "crypto_breakout"}

    # Requiring two tags narrows to the single strategy holding both (AND).
    result = await search_strategies(
        services, PRINCIPAL, {"tags": ["momentum", "breakout"]}
    )
    assert [s["name"] for s in result["strategies"]] == ["crypto_breakout"]


async def test_search_tags_case_insensitive_and_blank_dropped():
    registry = _make_registry(ALL_FIXTURES)
    services = _make_services(registry)

    result = await search_strategies(
        services, PRINCIPAL, {"tags": ["TREND-FOLLOWING", "  "]}
    )
    assert [s["name"] for s in result["strategies"]] == ["momentum"]


async def test_search_tags_reads_marketplace_nested_tags():
    """mean_reversion stores tags under ``marketplace.tags`` — verify the
    extractor reads that location, not just top-level ``tags``."""
    registry = _make_registry(ALL_FIXTURES)
    services = _make_services(registry)

    result = await search_strategies(
        services, PRINCIPAL, {"tags": ["low-frequency"]}
    )
    assert [s["name"] for s in result["strategies"]] == ["mean_reversion"]


async def test_search_risk_level_exact_case_insensitive_match():
    registry = _make_registry(ALL_FIXTURES)
    services = _make_services(registry)

    high = await search_strategies(services, PRINCIPAL, {"risk_level": "HIGH"})
    assert [s["name"] for s in high["strategies"]] == ["crypto_breakout"]

    low = await search_strategies(services, PRINCIPAL, {"risk_level": "low"})
    assert [s["name"] for s in low["strategies"]] == ["mean_reversion"]


async def test_search_asset_class_substring_match():
    registry = _make_registry(ALL_FIXTURES)
    services = _make_services(registry)

    # 'equity' matches both 'equity' (momentum) and 'US equities' (mean_reversion).
    equities = await search_strategies(
        services, PRINCIPAL, {"asset_class": "equity"}
    )
    assert {s["name"] for s in equities["strategies"]} == {
        "momentum",
        "mean_reversion",
    }

    # 'forex' only matches crypto_breakout's multi-asset list.
    forex = await search_strategies(services, PRINCIPAL, {"asset_class": "FOREX"})
    assert [s["name"] for s in forex["strategies"]] == ["crypto_breakout"]


# ── 5. search_strategies — combined filters & no-match ────────────────── #
async def test_search_combined_filters_are_anded():
    registry = _make_registry(ALL_FIXTURES)
    services = _make_services(registry)

    # tag 'momentum' (momentum + crypto) intersected with risk 'high' → crypto.
    result = await search_strategies(
        services,
        PRINCIPAL,
        {"tags": ["momentum"], "risk_level": "high"},
    )
    assert [s["name"] for s in result["strategies"]] == ["crypto_breakout"]


async def test_search_combined_query_and_asset_class():
    registry = _make_registry(ALL_FIXTURES)
    services = _make_services(registry)

    result = await search_strategies(
        services,
        PRINCIPAL,
        {"query": "reversion", "asset_class": "equity"},
    )
    assert [s["name"] for s in result["strategies"]] == ["mean_reversion"]


async def test_search_no_match_returns_empty():
    registry = _make_registry(ALL_FIXTURES)
    services = _make_services(registry)

    result = await search_strategies(services, PRINCIPAL, {"query": "zzznope"})
    assert result == {"count": 0, "strategies": []}


async def test_search_filters_that_mutually_exclude_return_empty():
    registry = _make_registry(ALL_FIXTURES)
    services = _make_services(registry)

    # No equity strategy is 'high' risk.
    result = await search_strategies(
        services, PRINCIPAL, {"asset_class": "equity", "risk_level": "high"}
    )
    assert result["count"] == 0
    assert result["strategies"] == []


async def test_search_unknown_tag_returns_empty_without_error():
    registry = _make_registry(ALL_FIXTURES)
    services = _make_services(registry)

    result = await search_strategies(services, PRINCIPAL, {"tags": ["nonexistent"]})
    assert result == {"count": 0, "strategies": []}


# ── 6. search_strategies — argument validation ───────────────────────── #
@pytest.mark.parametrize(
    "arguments",
    [
        {"query": 123},
        {"tags": "momentum"},
        {"tags": ["ok", 5]},
        {"risk_level": ["high"]},
        {"asset_class": {"a": 1}},
    ],
    ids=[
        "query-not-string",
        "tags-not-list",
        "tags-has-non-string",
        "risk_level-not-string",
        "asset_class-not-string",
    ],
)
async def test_search_rejects_wrong_argument_types(arguments):
    services = _make_services(_make_registry(ALL_FIXTURES))
    with pytest.raises(ValidationError):
        await search_strategies(services, PRINCIPAL, arguments)


async def test_search_blank_filters_behave_as_no_filter():
    """Empty/blank strings and empty tag list are no-ops (return all)."""
    registry = _make_registry(ALL_FIXTURES)
    services = _make_services(registry)

    result = await search_strategies(
        services,
        PRINCIPAL,
        {"query": "   ", "tags": [], "risk_level": "", "asset_class": "  "},
    )
    assert result["count"] == 3


async def test_search_uses_registry_as_single_source_of_truth():
    """search_strategies must enumerate via the injected registry only —
    no latent discover_strategies() against strategies_dir."""
    registry = _make_registry(ALL_FIXTURES)
    services = _make_services(registry)

    await search_strategies(services, PRINCIPAL, {"query": "crypto"})

    registry.list_strategies.assert_called_once_with()
    # get_manifest consulted once per registered strategy.
    assert registry.get_manifest.call_count == len(ALL_FIXTURES)


# ── 7. get_strategy_details — validation & not-found ────────────────────── #
@pytest.mark.parametrize(
    "name",
    [None, "", "   "],
    ids=["missing", "empty-string", "blank-string"],
)
async def test_get_strategy_details_requires_strategy_name(name):
    registry = _make_registry({"momentum": MANIFEST_MOMENTUM})
    services = _make_services(registry)

    with pytest.raises(ValidationError) as exc_info:
        await get_strategy_details(
            services, PRINCIPAL, {"strategy_name": name}
        )

    assert "strategy_name is required" in str(exc_info.value)
    # Validation short-circuits before the registry is consulted.
    registry.get_manifest.assert_not_called()


async def test_get_strategy_details_requires_strategy_name_key():
    """Omitting the key entirely (no strategy_name at all) is also rejected."""
    registry = _make_registry({"momentum": MANIFEST_MOMENTUM})
    services = _make_services(registry)

    with pytest.raises(ValidationError):
        await get_strategy_details(services, PRINCIPAL, {})

    registry.get_manifest.assert_not_called()


async def test_get_strategy_details_unknown_strategy_raises_not_found():
    registry = _make_registry({"momentum": MANIFEST_MOMENTUM})
    services = _make_services(registry)

    with pytest.raises(NotFoundError) as exc_info:
        await get_strategy_details(
            services, PRINCIPAL, {"strategy_name": "nope"}
        )

    assert str(exc_info.value) == "Strategy not found: nope"
    registry.get_manifest.assert_called_once_with("nope")
    # module_path must never be queried when the manifest lookup failed.
    registry.get_module_path.assert_not_called()


# ── 8. dispatch_tool integration ─────────────────────────────────────────── #
async def test_dispatch_tool_routes_get_strategy_details():
    registry = _make_registry({"momentum": MANIFEST_MOMENTUM})
    services = _make_services(registry)

    out = await dispatch_tool(
        "get_strategy_details", {"strategy_name": "momentum"}, services, PRINCIPAL
    )

    assert out["name"] == "momentum"
    assert out["version"] == "1.2.0"
    assert out["parameters"] == {"lookback": 20, "threshold": 0.02}
    registry.get_manifest.assert_called_once_with("momentum")


async def test_dispatch_tool_get_strategy_details_not_found_propagates():
    """A NotFoundError propagates through dispatch_tool unchanged."""
    registry = _make_registry({"momentum": MANIFEST_MOMENTUM})
    services = _make_services(registry)

    with pytest.raises(MCPError) as exc_info:
        await dispatch_tool(
            "get_strategy_details", {"strategy_name": "ghost"}, services, PRINCIPAL
        )
    # Stays a NotFoundError — not re-wrapped into a generic EngineError.
    assert isinstance(exc_info.value, NotFoundError)


async def test_dispatch_tool_get_strategy_details_missing_required_arg():
    """dispatch_tool validates required args before the adapter runs."""
    registry = _make_registry({"momentum": MANIFEST_MOMENTUM})
    services = _make_services(registry)

    with pytest.raises(ValidationError) as exc_info:
        await dispatch_tool("get_strategy_details", {}, services, PRINCIPAL)

    assert "strategy_name" in str(exc_info.value)
    registry.get_manifest.assert_not_called()


async def test_dispatch_tool_unknown_tool_rejected():
    services = _make_services(_make_registry({"momentum": MANIFEST_MOMENTUM}))
    with pytest.raises(ValidationError) as exc_info:
        await dispatch_tool("does_not_exist", {}, services, PRINCIPAL)
    assert "Unknown tool" in str(exc_info.value)


async def test_dispatch_tool_routes_list_strategies():
    registry = _make_registry(
        {"momentum": MANIFEST_MOMENTUM, "mean_reversion": MANIFEST_MEANREV}
    )
    services = _make_services(registry)

    out = await dispatch_tool("list_strategies", {}, services, PRINCIPAL)

    assert out["count"] == 2
    assert {s["name"] for s in out["strategies"]} == {"momentum", "mean_reversion"}


async def test_dispatch_tool_routes_search_strategies():
    registry = _make_registry(ALL_FIXTURES)
    services = _make_services(registry)

    out = await dispatch_tool(
        "search_strategies", {"risk_level": "high"}, services, PRINCIPAL
    )

    assert out["count"] == 1
    assert out["strategies"][0]["name"] == "crypto_breakout"
    # Pagination metadata is attached by the dispatcher (list-heavy tool).
    assert out["total"] == 1
    assert "next_cursor" in out


async def test_dispatch_tool_search_strategies_no_filter_paginates_all():
    registry = _make_registry(ALL_FIXTURES)
    services = _make_services(registry)

    out = await dispatch_tool("search_strategies", {}, services, PRINCIPAL)

    assert out["total"] == 3
    assert {s["name"] for s in out["strategies"]} == set(ALL_FIXTURES)


async def test_dispatch_tool_search_strategies_unknown_tool_rejected():
    services = _make_services(_make_registry(ALL_FIXTURES))
    with pytest.raises(ValidationError) as exc_info:
        await dispatch_tool("search_strategys", {}, services, PRINCIPAL)
    assert "Unknown tool" in str(exc_info.value)


# ── 9. EngineServices.for_testing coherence ──────────────────────────────── #
def test_for_testing_builds_registry_from_strategies_dir(tmp_path: Path):
    """A temp strategies_dir (with no injected registry) drives the catalog.

    Guarantees the registry and strategies_dir never disagree, which is what
    makes the adapter's registry-only lookup correct in hermetic tests.
    """
    import yaml

    strat_dir = tmp_path / "strategies" / "alpha"
    strat_dir.mkdir(parents=True)
    (strat_dir / "manifest.yaml").write_text(yaml.dump({"name": "alpha", "version": "9.9"}))
    (strat_dir / "strategy.py").write_text("class Strategy: pass\n")

    services = EngineServices.for_testing(strategies_dir=tmp_path / "strategies")

    assert services.plugin_registry.list_strategies() == ["alpha"]
    assert services.plugin_registry.get_manifest("alpha") == {"name": "alpha", "version": "9.9"}
    assert services.plugin_registry.get_module_path("alpha").endswith("alpha/strategy.py")
