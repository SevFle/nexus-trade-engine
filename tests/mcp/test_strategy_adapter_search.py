"""Targeted tests for the recently changed asset-class matching logic and the
marketplace dict guard in :mod:`engine.mcp.adapters.strategy_adapter`.

These tests pin the behaviour that was broken before the fix:

* ``search_strategies(asset_class="equity")`` must match a strategy filed under
  the *plural* ``US equities`` — a plain substring test misses this because
  ``equity`` is not a substring of ``equities``.
* combined ``query`` + ``asset_class`` filters are AND-combined (a strategy must
  satisfy both), so ``{"query": "reversion", "asset_class": "equity"}`` returns
  ``mean_reversion`` rather than ``[]``.
* a malformed (non-dict) ``marketplace`` manifest field must not crash the
  extractor with an ``AttributeError`` on ``.get()`` — the high-severity guard
  coerces it to an empty dict.

The :class:`~engine.plugins.registry.PluginRegistry` is mocked so no manifest
files, plugin code, or disk I/O are required.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from engine.mcp.adapters import EngineServices
from engine.mcp.adapters.strategy_adapter import (
    _asset_class_matches,
    _extract_filter_attributes,
    _singularize,
    search_strategies,
)
from engine.mcp.auth import AuthPrincipal

PRINCIPAL = AuthPrincipal(user_id="quant-1", role="viewer", auth_method="jwt")

MANIFEST_MOMENTUM: dict[str, Any] = {
    "name": "momentum",
    "version": "1.2.0",
    "description": "Trend-following momentum strategy.",
    "tags": ["momentum", "trend-following"],
    "risk_level": "medium",
    "asset_class": "equity",
}

MANIFEST_MEANREV: dict[str, Any] = {
    "name": "mean_reversion",
    "version": "0.4.1",
    "description": "Bollinger-band mean reversion.",
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
    "tags": ["momentum", "breakout", "high-frequency"],
    "risk_level": "high",
    "asset_classes": ["crypto", "forex"],
}

ALL_FIXTURES: dict[str, dict[str, Any]] = {
    "momentum": MANIFEST_MOMENTUM,
    "mean_reversion": MANIFEST_MEANREV,
    "crypto_breakout": MANIFEST_CRYPTO,
}


# ── Helpers ──────────────────────────────────────────────────────────────── #
def _make_registry(strategies: dict[str, dict[str, Any]]) -> MagicMock:
    spec = MagicMock(name="PluginRegistry")
    spec.list_strategies.return_value = list(strategies)
    spec.get_manifest.side_effect = strategies.get
    return spec


def _make_services(registry: MagicMock) -> EngineServices:
    return EngineServices(
        plugin_registry=registry,
        strategies_dir=Path("/nonexistent"),
    )


# ══════════════════════════════════════════════════════════════════════════ #
# 1. _singularize — light plural normalization                              #
# ══════════════════════════════════════════════════════════════════════════ #
@pytest.mark.parametrize(
    ("plural", "singular"),
    [
        ("equities", "equity"),
        ("stocks", "stock"),
        ("boxes", "box"),
        ("categories", "category"),
        ("bonds", "bond"),
        ("currencies", "currency"),
    ],
    ids=["ies->y", "s", "es", "ies-category", "s-bond", "ies-currency"],
)
def test_singularize_strips_plural_suffixes(plural: str, singular: str):
    assert _singularize(plural) == singular


@pytest.mark.parametrize(
    "word",
    ["equity", "crypto", "forex", "bond", "category"],
    ids=["equity", "crypto", "forex", "bond", "category-singular"],
)
def test_singularize_passes_already_singular_through(word: str):
    assert _singularize(word) == word


@pytest.mark.parametrize(
    "word",
    ["us", "fx", "is", "ss"],
    ids=["us", "fx", "is", "ss"],
)
def test_singularize_leaves_short_words_alone(word: str):
    """Short words are never over-stripped (would corrupt tokens like ``us``)."""
    assert _singularize(word) == word


@pytest.mark.parametrize(
    ("word", "expected"),
    [("class", "class"), ("glass", "glass"), ("loss", "loss"), ("address", "address")],
    ids=["class", "glass", "loss", "address"],
)
def test_singularize_does_not_strip_double_s(word: str, expected: str):
    """Double-``s`` endings are not plural markers — they must be preserved."""
    assert _singularize(word) == expected


def test_singularize_is_case_insensitive_and_strips_whitespace():
    assert _singularize("  Equities  ") == "equity"
    assert _singularize("STOCKS") == "stock"


# ══════════════════════════════════════════════════════════════════════════ #
# 2. _asset_class_matches — the core matching primitive                     #
# ══════════════════════════════════════════════════════════════════════════ #
def test_asset_class_matches_exact_substring():
    assert _asset_class_matches("crypto", ["crypto"]) is True
    assert _asset_class_matches("forex", ["crypto", "forex"]) is True


def test_asset_class_matches_substring_within_token():
    """``crypt`` is a substring of ``crypto`` so it matches."""
    assert _asset_class_matches("crypt", ["crypto"]) is True


def test_asset_class_matches_singular_query_to_plural_asset():
    """The regression under test: ``equity`` must match ``US equities``."""
    assert _asset_class_matches("equity", ["us equities"]) is True


def test_asset_class_matches_plural_query_to_singular_asset():
    """Symmetric: a plural query matches a singular asset class."""
    assert _asset_class_matches("equities", ["equity"]) is True


def test_asset_class_matches_case_insensitive():
    assert _asset_class_matches("EQUITY", ["US Equities"]) is True
    assert _asset_class_matches("Equity", ["  Equity  "]) is True


def test_asset_class_matches_punctuation_delimited_tokens():
    """``fx`` is a token of ``fx/crypto`` — it must match."""
    assert _asset_class_matches("fx", ["fx/crypto"]) is True
    assert _asset_class_matches("crypto", ["fx/crypto"]) is True


def test_asset_class_matches_prefix_variant():
    """``crypt`` matches ``cryptocurrency`` via prefix (and substring)."""
    assert _asset_class_matches("crypt", ["cryptocurrency"]) is True


def test_asset_class_matches_blank_query_returns_true():
    """An empty query is a no-op filter (matches everything)."""
    assert _asset_class_matches("", ["equity"]) is True
    assert _asset_class_matches("   ", ["equity"]) is True
    assert _asset_class_matches("", []) is True


def test_asset_class_matches_no_overlap_returns_false():
    assert _asset_class_matches("bond", ["equity"]) is False
    assert _asset_class_matches("equity", ["crypto", "forex"]) is False


def test_asset_class_matches_empty_asset_list_returns_false():
    assert _asset_class_matches("equity", []) is False


def test_asset_class_matches_skips_blank_asset_entries():
    """Blank/whitespace asset strings are skipped, not treated as matches."""
    assert _asset_class_matches("equity", ["", "  "]) is False
    # A real entry after blanks still matches.
    assert _asset_class_matches("equity", ["", "equity"]) is True


def test_asset_class_matches_coerces_non_string_assets():
    """Asset entries are stringified before comparison."""
    assert _asset_class_matches("123", [123]) is True
    assert _asset_class_matches("equity", [None, "equity"]) is True


# ══════════════════════════════════════════════════════════════════════════ #
# 3. _extract_filter_attributes — high-severity marketplace dict guard       #
# ══════════════════════════════════════════════════════════════════════════ #
def test_extract_filter_attributes_marketplace_none_does_not_crash():
    """The original (pre-fix) ``or {}`` handled None; it must still work."""
    attrs = _extract_filter_attributes({"name": "x"})
    assert attrs == {"tags": [], "risk_level": None, "asset_class": []}


@pytest.mark.parametrize(
    "bad_marketplace",
    [
        ["not", "a", "dict"],
        "a string is not a dict",
        42,
        [("nested", "tuple")],
        [("set_member",)],
        True,
    ],
    ids=["list", "string", "int", "list-of-tuples", "set", "bool"],
)
def test_extract_filter_attributes_marketplace_non_dict_is_guarded(bad_marketplace):
    """The high-severity fix: a truthy non-dict ``marketplace`` must not raise
    ``AttributeError`` on ``.get()``. It is coerced to an empty dict instead."""
    attrs = _extract_filter_attributes({"name": "x", "marketplace": bad_marketplace})
    # No exception, and the extractor degrades to empty filter fields.
    assert attrs == {"tags": [], "risk_level": None, "asset_class": []}


def test_extract_filter_attributes_valid_marketplace_dict_still_read():
    """Regression guard: a well-formed ``marketplace`` dict is still parsed
    (the guard must not break the happy path)."""
    manifest = {
        "name": "mean_reversion",
        "marketplace": {
            "tags": ["mean-reversion"],
            "risk_level": "low",
            "preferred_assets": ["US equities"],
        },
    }
    attrs = _extract_filter_attributes(manifest)
    assert attrs["tags"] == ["mean-reversion"]
    assert attrs["risk_level"] == "low"
    assert attrs["asset_class"] == ["us equities"]


def test_extract_filter_attributes_top_level_precedence_over_marketplace():
    """When the same field exists at top level and under marketplace, the
    top-level value wins — confirming the precedence order is unchanged."""
    manifest = {
        "tags": ["top-level"],
        "risk_level": "high",
        "asset_class": "equity",
        "marketplace": {
            "tags": ["nested"],
            "risk_level": "low",
            "preferred_assets": ["crypto"],
        },
    }
    attrs = _extract_filter_attributes(manifest)
    assert attrs["tags"] == ["top-level"]
    assert attrs["risk_level"] == "high"
    assert attrs["asset_class"] == ["equity"]


# ══════════════════════════════════════════════════════════════════════════ #
# 4. search_strategies — end-to-end integration of the fixes                #
# ══════════════════════════════════════════════════════════════════════════ #
async def test_search_asset_class_singular_matches_plural_us_equities():
    """The primary regression: ``equity`` matches both ``equity`` (momentum)
    and ``US equities`` (mean_reversion)."""
    registry = _make_registry(ALL_FIXTURES)
    services = _make_services(registry)

    result = await search_strategies(services, PRINCIPAL, {"asset_class": "equity"})

    assert {s["name"] for s in result["strategies"]} == {
        "momentum",
        "mean_reversion",
    }


async def test_search_combined_query_and_asset_class_anded():
    """AND logic: ``query='reversion'`` AND ``asset_class='equity'`` must yield
    exactly ``mean_reversion`` — not ``[]`` (the pre-fix bug)."""
    registry = _make_registry(ALL_FIXTURES)
    services = _make_services(registry)

    result = await search_strategies(
        services,
        PRINCIPAL,
        {"query": "reversion", "asset_class": "equity"},
    )

    assert [s["name"] for s in result["strategies"]] == ["mean_reversion"]


async def test_search_combined_query_and_asset_class_mutual_exclusion():
    """AND logic still excludes when filters conflict: ``reversion`` query does
    not match the ``forex``/``crypto`` asset class of crypto_breakout."""
    registry = _make_registry(ALL_FIXTURES)
    services = _make_services(registry)

    result = await search_strategies(
        services,
        PRINCIPAL,
        {"query": "reversion", "asset_class": "crypto"},
    )

    assert result == {"count": 0, "strategies": []}


async def test_search_asset_class_does_not_over_match():
    """``equity`` must NOT match crypto_breakout (crypto/forex)."""
    registry = _make_registry(ALL_FIXTURES)
    services = _make_services(registry)

    result = await search_strategies(services, PRINCIPAL, {"asset_class": "equity"})

    assert "crypto_breakout" not in {s["name"] for s in result["strategies"]}


async def test_search_survives_malformed_marketplace_non_dict():
    """A manifest with a non-dict ``marketplace`` (e.g. a list, as sometimes
    emitted by broken manifests) must not crash search_strategies. The strategy
    is still searchable by its top-level fields."""
    manifest = {
        "name": "weird",
        "version": "0.1.0",
        "description": "A strategy with a malformed marketplace field.",
        "tags": ["alpha"],
        "risk_level": "medium",
        "asset_class": "equity",
        # High-severity trigger: truthy non-dict marketplace would previously
        # raise AttributeError on marketplace.get(...).
        "marketplace": ["malformed", "list"],
    }
    registry = _make_registry({"weird": manifest})
    services = _make_services(registry)

    # No exception, and the top-level asset_class still drives a match.
    result = await search_strategies(
        services, PRINCIPAL, {"asset_class": "equity"}
    )
    assert [s["name"] for s in result["strategies"]] == ["weird"]


async def test_search_malformed_marketplace_does_not_silently_match_all():
    """A malformed marketplace must not make a strategy match an unrelated
    asset class — the guard yields an empty asset list, so unrelated filters
    correctly exclude the strategy."""
    manifest = {
        "name": "weird",
        "description": "no top-level asset class",
        "marketplace": ["malformed", "list"],
    }
    registry = _make_registry({"weird": manifest})
    services = _make_services(registry)

    result = await search_strategies(
        services, PRINCIPAL, {"asset_class": "forex"}
    )
    assert result == {"count": 0, "strategies": []}


async def test_search_asset_class_blank_is_no_op():
    """An explicitly blank asset_class behaves as 'no filter' (returns all)."""
    registry = _make_registry(ALL_FIXTURES)
    services = _make_services(registry)

    result = await search_strategies(services, PRINCIPAL, {"asset_class": "  "})

    assert result["count"] == 3
    assert {s["name"] for s in result["strategies"]} == set(ALL_FIXTURES)
