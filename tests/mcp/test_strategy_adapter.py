"""Unit tests for the strategy MCP adapters.

Covers :func:`engine.mcp.adapters.strategy_adapter.list_strategies` and
:func:`engine.mcp.adapters.strategy_adapter.get_strategy_details`, plus the
:func:`~engine.mcp.handlers.dispatch_tool` integration for the
``get_strategy_details`` route.

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
}

MANIFEST_MEANREV: dict[str, Any] = {
    "name": "mean_reversion",
    "version": "0.4.1",
    "description": "Bollinger-band mean reversion.",
    "author": "nexus-team",
    "symbols": ["GOOGL"],
    "timeframe": "1h",
    "parameters": {"window": 14, "num_std": 2.0},
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


# ── 1b. list_strategies — marketplace hardening & per-entry resilience ──── #
@pytest.mark.parametrize(
    "value",
    ["not-a-dict", ["a", "b"], 42, ["marketplace"], ("tuple",)],
    ids=["string", "list", "int", "list-of-str", "tuple"],
)
async def test_list_strategies_marketplace_non_dict_degrades_to_empty_dict(value):
    """A malformed manifest storing a non-dict under ``marketplace`` must
    not leak that unexpected type into the JSON summary — it degrades to
    ``{}`` instead of being passed through (the bug a naive
    ``manifest.get("marketplace") or {}`` would let slip for truthy values)."""
    manifest = dict(MANIFEST_MOMENTUM)
    manifest["marketplace"] = value
    services = _make_services(_make_registry({"momentum": manifest}))

    result = await list_strategies(services, PRINCIPAL, {})

    assert result["count"] == 1
    entry = result["strategies"][0]
    assert entry["marketplace"] == {}
    # Everything else still projects correctly from the manifest.
    assert entry["name"] == "momentum"
    assert entry["version"] == "1.2.0"


async def test_list_strategies_marketplace_dict_is_passed_through():
    """A well-formed dict ``marketplace`` block is surfaced unchanged."""
    manifest = dict(MANIFEST_MOMENTUM)
    manifest["marketplace"] = {"category": "momentum", "featured": True}
    services = _make_services(_make_registry({"momentum": manifest}))

    result = await list_strategies(services, PRINCIPAL, {})

    assert result["strategies"][0]["marketplace"] == {
        "category": "momentum",
        "featured": True,
    }


async def test_list_strategies_missing_marketplace_defaults_to_empty_dict():
    """No ``marketplace`` key at all still resolves to ``{}``."""
    services = _make_services(_make_registry({"momentum": MANIFEST_MOMENTUM}))

    result = await list_strategies(services, PRINCIPAL, {})

    assert result["strategies"][0]["marketplace"] == {}


async def test_list_strategies_skips_entry_when_registry_get_manifest_raises():
    """If ``get_manifest`` raises for one entry, that entry is skipped and a
    warning is logged — the rest of the catalogue is still returned.

    This proves the per-strategy loop body is wrapped in try/except, so a
    single unhealthy plugin cannot poison the whole listing.
    """
    from structlog.testing import capture_logs

    def boom(_name: str):
        raise RuntimeError("registry blew up")

    # Two healthy entries plus one whose manifest lookup raises. Order is
    # preserved by list_strategies so we can assert exactly which survived.
    registry = MagicMock(name="PluginRegistry")
    registry.list_strategies.return_value = [
        "momentum",
        "broken",
        "mean_reversion",
    ]
    registry.get_manifest.side_effect = lambda name: (
        boom(name) if name == "broken" else
        {"momentum": MANIFEST_MOMENTUM, "mean_reversion": MANIFEST_MEANREV}.get(name)
    )
    services = _make_services(registry)

    with capture_logs() as cap_logs:
        result = await list_strategies(services, PRINCIPAL, {})

    # The healthy strategies still come through; the broken one is dropped.
    assert result["count"] == 2
    assert {s["name"] for s in result["strategies"]} == {
        "momentum",
        "mean_reversion",
    }
    # A warning was emitted for the failing entry, naming the strategy and
    # carrying the exception type/message for triage.
    warnings = [
        e for e in cap_logs
        if e.get("event") == "mcp.strategy_summary_failed"
        and e.get("log_level") == "warning"
    ]
    assert len(warnings) == 1
    assert warnings[0]["strategy"] == "broken"
    assert warnings[0]["error"] == "RuntimeError"
    assert "registry blew up" in warnings[0]["message"]


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
    # ``marketplace`` is absent from MANIFEST_MOMENTUM, so it degrades to
    # an empty dict (the same isinstance-guarded default as list_strategies).
    assert detail["marketplace"] == {}
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
    assert detail["marketplace"] == {}
    assert detail["module_path"] == "/strategies/bare/strategy.py"


# ── 3. get_strategy_details — validation & not-found ────────────────────── #
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


# ── 4. dispatch_tool integration ─────────────────────────────────────────── #
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


# ── 5. EngineServices.for_testing coherence ──────────────────────────────── #
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
