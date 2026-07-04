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

import re
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


def _extract_filter_attributes(manifest: dict[str, Any]) -> dict[str, Any]:
    """Pull the searchable fields out of a manifest.

    Manifests in the wild store these fields inconsistently — some at the
    top level, some nested under ``marketplace`` — and several spellings
    exist (``asset_class`` vs ``asset_classes`` vs ``preferred_assets``).
    This helper tolerates all of them so :func:`search_strategies` is not
    coupled to one schema version. Tags and asset classes are returned as
    lower-cased lists; ``risk_level`` as a lower-cased string or ``None``.
    """
    # High-severity guard: ``marketplace`` may legally be absent (None) but
    # malformed manifests can also store a non-dict here (e.g. a list or
    # string). A bare ``or {}`` only catches falsy values, so a truthy
    # non-dict would later crash on ``.get()``. Coerce defensively.
    marketplace = manifest.get("marketplace")
    if not isinstance(marketplace, dict):
        marketplace = {}

    raw_tags = manifest.get("tags")
    if raw_tags is None:
        raw_tags = marketplace.get("tags")
    if isinstance(raw_tags, str):
        tags = [t.strip().lower() for t in raw_tags.split(",") if t.strip()]
    elif isinstance(raw_tags, list):
        tags = [str(t).strip().lower() for t in raw_tags if str(t).strip()]
    else:
        tags = []

    raw_assets = (
        manifest.get("asset_classes")
        or manifest.get("asset_class")
        or manifest.get("preferred_assets")
        or marketplace.get("preferred_assets")
        or marketplace.get("asset_classes")
        or marketplace.get("asset_class")
    )
    if isinstance(raw_assets, str):
        assets = [raw_assets.strip().lower()] if raw_assets.strip() else []
    elif isinstance(raw_assets, list):
        assets = [str(a).strip().lower() for a in raw_assets if str(a).strip()]
    else:
        assets = []

    raw_risk = manifest.get("risk_level")
    if raw_risk is None:
        raw_risk = marketplace.get("risk_level")
    risk_level = str(raw_risk).strip().lower() if raw_risk else None

    return {"tags": tags, "risk_level": risk_level, "asset_class": assets}


# Split on any run of non-word characters so "US equities" → ["us", "equities"],
# "fx/crypto" → ["fx", "crypto"], etc.
_TOKEN_SPLIT = re.compile(r"[\W_]+", re.UNICODE)

# Minimum token length required before each plural-suffix rule is applied.
# Below these thresholds stripping the suffix would mangle short tokens
# ("us", "fx", "is", "ss", ...).
_IES_RULE_MIN_LEN = 4
_ES_RULE_MIN_LEN = 3
_S_RULE_MIN_LEN = 2


def _singularize(word: str) -> str:
    """Crude singularizer for asset-class token comparison.

    Strips common English plural suffixes so ``equities``→``equity``,
    ``stocks``→``stock``, ``boxes``→``box``. Intentionally small — its only
    job is to bridge the singular/plural gap a plain substring or prefix test
    cannot (``equity`` is neither a substring nor a prefix of ``equities``).
    Short words and double-``s`` endings are left alone to avoid mangling
    tokens like ``us`` / ``class`` / ``fx``.
    """
    w = word.lower().strip()
    if len(w) > _IES_RULE_MIN_LEN and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > _ES_RULE_MIN_LEN and w.endswith("es"):
        return w[:-2]
    if len(w) > _S_RULE_MIN_LEN and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def _asset_class_matches(query: str, asset_classes: list[str]) -> bool:
    """Return True if ``query`` matches any of ``asset_classes``.

    The match is intentionally permissive so a caller searching for the
    *singular* ``equity`` still finds a strategy filed under the *plural*
    ``US equities``. A pure substring test (``query in asset``) misses that
    case because ``equity`` is not a substring of ``equities``, and a plain
    token-prefix test also misses it (``equities`` does not start with
    ``equity``). Light singularization of both sides closes the gap.

    A query matches an asset class when **any** is true:

    * ``query`` is a substring of the (lower-cased) asset string, **or**
    * the singularized ``query`` equals the singularized form of any token
      in the asset string (``equity`` ↔ ``equities``), **or**
    * one singularized form is a prefix of the other (``crypt`` ↔ ``crypto``).

    Callers pass already-lower-cased inputs.
    """
    if not query:
        return True
    query = query.lower().strip()
    if not query:
        return True
    query_stem = _singularize(query)
    for asset in asset_classes:
        asset_lc = str(asset).lower().strip()
        if not asset_lc:
            continue
        if query in asset_lc:
            return True
        tokens = [tok for tok in _TOKEN_SPLIT.split(asset_lc) if tok]
        for tok in tokens:
            tok_stem = _singularize(tok)
            if (
                tok_stem == query_stem
                or tok_stem.startswith(query_stem)
                or query_stem.startswith(tok_stem)
            ):
                return True
    return False


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


async def search_strategies(
    services: EngineServices,
    _principal: AuthPrincipal,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Filter installed strategies by free-text query, tags, risk, and asset class.

    Every filter is optional; with no filters the full catalog (the same set
    :func:`list_strategies` returns) is produced. When multiple filters are
    supplied they are AND-combined — a strategy must satisfy all of them to
    be included.

    * ``query`` — case-insensitive substring matched against the strategy
      *name* and *description*.
    * ``tags`` — strategy must contain *all* of the requested tags
      (intersection; case-insensitive).
    * ``risk_level`` — exact, case-insensitive match (e.g. ``low`` / ``medium``
      / ``high``).
    * ``asset_class`` — case-insensitive substring match against any of the
      strategy's asset classes (so ``equity`` matches ``US equities``).

    Each result summary additionally carries ``tags``, ``risk_level``, and
    ``asset_class`` so the assistant can see why a strategy matched and
    refine subsequent searches.
    """
    query = arguments.get("query")
    tags = arguments.get("tags")
    risk_level = arguments.get("risk_level")
    asset_class = arguments.get("asset_class")

    # ── Argument validation (defence-in-depth on top of JSON Schema) ──
    if query is not None and not isinstance(query, str):
        raise ValidationError("query must be a string")
    query_norm = query.strip().lower() if isinstance(query, str) else ""

    if tags is not None:
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise ValidationError("tags must be an array of strings")
        tags_norm = [t.strip().lower() for t in tags if t.strip()]
    else:
        tags_norm = []

    if risk_level is not None and not isinstance(risk_level, str):
        raise ValidationError("risk_level must be a string")
    risk_norm = risk_level.strip().lower() if isinstance(risk_level, str) else ""

    if asset_class is not None and not isinstance(asset_class, str):
        raise ValidationError("asset_class must be a string")
    asset_norm = asset_class.strip().lower() if isinstance(asset_class, str) else ""

    # ── Build summaries and apply filters ──
    registry = services.plugin_registry
    matches: list[dict[str, Any]] = []
    for name in registry.list_strategies():
        manifest = registry.get_manifest(name) or {}
        summary = _summarize(name, manifest)
        attrs = _extract_filter_attributes(manifest)
        summary["tags"] = attrs["tags"]
        summary["risk_level"] = attrs["risk_level"]
        summary["asset_class"] = attrs["asset_class"]

        if query_norm:
            haystack = f"{summary['name']} {summary['description']}".lower()
            if query_norm not in haystack:
                continue
        if tags_norm and not set(tags_norm).issubset(set(attrs["tags"])):
            continue
        if risk_norm and attrs["risk_level"] != risk_norm:
            continue
        if asset_norm and not _asset_class_matches(asset_norm, attrs["asset_class"]):
            continue

        matches.append(summary)

    return to_jsonable({"count": len(matches), "strategies": matches})


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


__all__ = ["get_strategy_details", "list_strategies", "search_strategies"]
