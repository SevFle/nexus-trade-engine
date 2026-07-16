#!/usr/bin/env python3
"""Generate ``docs/mcp/api-surface-map.yaml`` from the live MCP modules.

Why a generator?
----------------
The MCP API surface is defined in code — ``engine/mcp/tool_definitions.py``
for tools, ``engine/mcp/resources.py`` for resources, etc. Hand-maintaining
a separate YAML map invites drift the moment a tool is added or renamed.

This script imports the real modules and emits a machine-readable map, so
the YAML is always a faithful snapshot of the implementation.

Usage::

    uv run python scripts/generate_mcp_api_surface.py
    uv run python scripts/generate_mcp_api_surface.py --check   # CI guard

``--check`` regenerates to a temp path and exits non-zero if the committed
file differs, so CI can block a PR that changed the surface without
refreshing the map.
"""

from __future__ import annotations

import argparse
import io
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

# Make the engine importable when run as a bare script from the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.api.auth.dependency import ROLE_HIERARCHY  # noqa: E402
from engine.mcp import errors as mcp_errors  # noqa: E402
from engine.mcp.auth import AuthPrincipal  # noqa: E402
from engine.mcp.handlers import _PAGINATED_KEYS  # noqa: E402
from engine.mcp.resources import RESOURCE_DEFINITIONS  # noqa: E402
from engine.mcp.tool_definitions import TOOL_DEFINITIONS  # noqa: E402

OUTPUT_PATH = REPO_ROOT / "docs" / "mcp" / "api-surface-map.yaml"

# Module inventory — descriptive metadata for the engine/mcp/ package.
# Kept here (not introspected) because it is documentation, not behaviour.
MODULES: list[dict[str, str]] = [
    {
        "file": "engine/mcp/config.py",
        "responsibility": (
            "MCPServerSettings — every NEXUS_MCP_* knob. Separate from "
            "engine.config so the server can run as a standalone stdio process."
        ),
    },
    {
        "file": "engine/mcp/tool_definitions.py",
        "responsibility": (
            "Declarative tool catalog (single source of truth): name, "
            "description, JSON-Schema input_schema, RBAC required_role, "
            "MCP ToolAnnotations."
        ),
    },
    {
        "file": "engine/mcp/handlers.py",
        "responsibility": (
            "dispatch_tool — routes a tools/call to its adapter, validates "
            "args against the schema, applies pagination, guards result size."
        ),
    },
    {
        "file": "engine/mcp/adapters/__init__.py",
        "responsibility": (
            "EngineServices (injectable capabilities) + PortfolioStore "
            "(in-memory portfolios, ownership model) + to_jsonable."
        ),
    },
    {
        "file": "engine/mcp/adapters/portfolio_adapter.py",
        "responsibility": (
            "Portfolio tools: status, positions, position, unrealized P&L, "
            "orders."
        ),
    },
    {
        "file": "engine/mcp/adapters/strategy_adapter.py",
        "responsibility": "Strategy tools: list_strategies, get_strategy_details.",
    },
    {
        "file": "engine/mcp/adapters/market_data_adapter.py",
        "responsibility": (
            "Market tools: get_market_data, get_cost_model, "
            "get_performance_metrics."
        ),
    },
    {
        "file": "engine/mcp/adapters/backtest_adapter.py",
        "responsibility": "run_backtest — historical strategy backtest.",
    },
    {
        "file": "engine/mcp/resources.py",
        "responsibility": (
            "Static reference resources (strategy catalog, symbols, "
            "timeframes, risk ranges, cost-model defaults)."
        ),
    },
    {
        "file": "engine/mcp/auth.py",
        "responsibility": (
            "extract_principal + require_role — resolves a credential to an "
            "AuthPrincipal and enforces the shared ROLE_HIERARCHY."
        ),
    },
    {
        "file": "engine/mcp/rate_limiter.py",
        "responsibility": "Per-principal in-memory token bucket.",
    },
    {
        "file": "engine/mcp/pagination.py",
        "responsibility": (
            "Cursor pagination (base64 offset) for list-heavy tools + "
            "ResultGuard (token-budget trimming)."
        ),
    },
    {
        "file": "engine/mcp/progress.py",
        "responsibility": (
            "ProgressReporter wrapping session.send_progress_notification "
            "(no-op-safe)."
        ),
    },
    {
        "file": "engine/mcp/errors.py",
        "responsibility": (
            "MCPError hierarchy with JSON-RPC codes; map_engine_exception "
            "normalises engine errors so tracebacks never leak."
        ),
    },
    {
        "file": "engine/mcp/observability.py",
        "responsibility": (
            "structlog events + counters/histograms via the pluggable "
            "MetricsBackend."
        ),
    },
]

# Transports are config values, not introspectable constants.
TRANSPORTS: list[dict[str, str]] = [
    {
        "name": "stdio",
        "description": (
            "Canonical MCP local-server mode: the client spawns the server as "
            "a subprocess and talks JSON-RPC over stdin/stdout."
        ),
        "settings": "transport=stdio",
    },
    {
        "name": "http",
        "description": (
            "Long-lived, remotely-addressable server over HTTP+JSON-RPC."
        ),
        "settings": "http_host, http_port, http_path, http_log_level",
    },
]

AUTH_METHODS: list[dict[str, str]] = [
    {
        "name": "jwt",
        "description": (
            "Engine JWT, decoded with the exact validator the REST API uses "
            "(decode_token)."
        ),
        "transport": "meta.authorization or NEXUS_MCP_TOKEN",
    },
    {
        "name": "api_key",
        "description": (
            "Static API-key table (NEXUS_MCP_STATIC_API_KEYS) for DB-free "
            "service-to-service auth."
        ),
        "transport": "meta.api_key or NEXUS_MCP_TOKEN",
    },
    {
        "name": "anonymous",
        "description": (
            "Issued only when NEXUS_MCP_AUTH_REQUIRED=false; gets "
            "NEXUS_MCP_DEFAULT_ROLE. Disables portfolio reads (ownership "
            "model rejects anonymous principals)."
        ),
        "transport": "n/a",
    },
]


def _tool_entry(definition: Any) -> dict[str, Any]:
    schema = definition.input_schema
    required: list[str] = list(schema.get("required", []))
    list_key = _PAGINATED_KEYS.get(definition.name)
    return {
        "name": definition.name,
        "required_role": definition.required_role,
        "read_only": definition.read_only,
        "destructive": definition.destructive,
        "idempotent": definition.idempotent,
        "paginated": {
            "cursor_enabled": list_key is not None,
            "list_key": list_key,
        },
        "required_fields": required,
        "properties": sorted(schema.get("properties", {}).keys()),
        "description": definition.description,
    }


def _resource_entry(defn: Any) -> dict[str, Any]:
    return {
        "uri": defn.uri,
        "name": defn.name,
        "mime_type": defn.mime_type,
        "description": defn.description,
    }


def _error_codes() -> list[dict[str, Any]]:
    """Introspect the JSON-RPC / MCP error code constants from errors.py."""
    entries: list[dict[str, Any]] = []
    for name in dir(mcp_errors):
        if not name.endswith("_ERROR") and name not in {
            "PARSE_ERROR",
            "INVALID_REQUEST",
            "METHOD_NOT_FOUND",
            "INVALID_PARAMS",
            "INTERNAL_ERROR",
        }:
            continue
        value = getattr(mcp_errors, name)
        if not isinstance(value, int):
            continue
        entries.append({"name": name, "code": value})
    entries.sort(key=lambda e: e["code"])
    return entries


def build_surface(now: datetime | None = None) -> dict[str, Any]:
    generated_at = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    tools = sorted((_tool_entry(t) for t in TOOL_DEFINITIONS), key=lambda e: e["name"])
    resources = sorted(
        (_resource_entry(r) for r in RESOURCE_DEFINITIONS), key=lambda e: e["uri"]
    )
    return {
        "meta": {
            "generated_at": generated_at,
            "generator": "scripts/generate_mcp_api_surface.py",
            "source_of_truth": [
                "engine/mcp/tool_definitions.py",
                "engine/mcp/resources.py",
                "engine/mcp/handlers.py",
                "engine/mcp/errors.py",
                "engine/mcp/auth.py",
            ],
            "note": (
                "This file is GENERATED — do not hand-edit. Re-run the "
                "generator after changing the MCP surface."
            ),
        },
        "summary": {
            "tool_count": len(tools),
            "resource_count": len(resources),
            "transports": [t["name"] for t in TRANSPORTS],
            "auth_methods": [a["name"] for a in AUTH_METHODS],
        },
        "modules": MODULES,
        "tools": tools,
        "resources": resources,
        "auth": {
            "principal": {
                "fields": list(AuthPrincipal.__dataclass_fields__.keys()),
            },
            "methods": AUTH_METHODS,
            "role_hierarchy": dict(ROLE_HIERARCHY),
        },
        "transports": TRANSPORTS,
        "errors": _error_codes(),
    }


def render(surface: dict[str, Any]) -> str:
    buffer = io.StringIO()
    buffer.write(
        "# MCP API surface map.\n"
        "# GENERATED by scripts/generate_mcp_api_surface.py — do not hand-edit.\n"
        "# Re-run it after changing the MCP surface (tools/resources/auth/errors).\n\n"
    )
    yaml.safe_dump(
        surface,
        buffer,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=100,
    )
    return buffer.getvalue()


def write_output(surface: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(surface), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if the committed map is out of date (CI guard).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT_PATH,
        help=f"Output path (default: {OUTPUT_PATH}).",
    )
    args = parser.parse_args(argv)

    surface = build_surface()

    if args.check:
        current = args.output.read_text(encoding="utf-8") if args.output.exists() else ""
        fresh = render(surface)
        if current.strip() != fresh.strip():
            sys.stderr.write(
                f"[mcp-surface] {args.output} is out of date. "
                "Run: uv run python scripts/generate_mcp_api_surface.py\n"
            )
            return 1
        sys.stdout.write(f"[mcp-surface] {args.output} is up to date.\n")
        return 0

    write_output(surface, args.output)
    sys.stdout.write(f"[mcp-surface] wrote {args.output}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
