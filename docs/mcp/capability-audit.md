# MCP capability audit

This page is the **API surface audit** for the Nexus MCP server. It exists
so that the surface advertised to LLM agents over the Model Context
Protocol can be reviewed, versioned, and diffed against the implementation
in one place — instead of scattered across prose.

> **What changed.** This audit was missing entirely: there was no
> `docs/mcp/` directory, so the canonical plan's Step 1 (API surface audit)
> and Step 2 (tool catalog) had no home. They now live here, alongside the
> machine-readable [`api-surface-map.yaml`](api-surface-map.yaml) that is
> **generated** from the live modules (see
> [How this map stays accurate](#how-this-map-stays-accurate)).

## Scope

The audit covers everything an MCP client can observe:

| Capability | Count | Source of truth |
|---|---|---|
| Tools (`tools/list` → `tools/call`) | 11 | [`engine/mcp/tool_definitions.py`](../../engine/mcp/tool_definitions.py) |
| Resources (`resources/list` → `resources/read`) | 5 | [`engine/mcp/resources.py`](../../engine/mcp/resources.py) |
| Auth methods | 3 (jwt, api_key, anonymous) | [`engine/mcp/auth.py`](../../engine/mcp/auth.py) |
| Transports | 2 (stdio, http) | [`engine/mcp/config.py`](../../engine/mcp/config.py) |
| Error codes | 10 | [`engine/mcp/errors.py`](../../engine/mcp/errors.py) |
| Modules | 15 | [`engine/mcp/`](../../engine/mcp/) |

For the authoritative, machine-readable inventory — every tool's required
fields, properties, role, and pagination status — read
[`api-surface-map.yaml`](api-surface-map.yaml). The tables below are the
human-readable summary of that file.

## Audit findings

### F1 — The surface is read-only or compute-only by construction

Every one of the 11 tools advertises `readOnlyHint=true,
destructiveHint=false, idempotentHint=true`. There is **no tool** that
touches the order book, the broker, or persists side effects to the trading
tables. `run_backtest` is the only "expensive" call and it is explicitly
compute-only. This is a deliberate safety invariant, not an accident: an
LLM agent cannot, through the MCP surface, place a real order. **Status:
pass — no drift.**

### F2 — Role gating is uniform and least-privilege

The default `required_role` is the read-only `viewer`. Only `run_backtest`
escalates, to `quant_dev`, because it is compute-intensive. The hierarchy
is shared byte-for-byte with the REST API
(`admin > portfolio_manager > developer > quant_dev > retail_trader >
user > viewer`), so a principal authenticated over MCP is
indistinguishable from one authenticated over HTTP for authorization.
**Status: pass — no drift.**

### F3 — List-heavy tools are cursor-paginated

Three tools (`get_orders`, `list_strategies`, `get_market_data`) return
lists that are bounded by cursor pagination and then passed through
`ResultGuard` so a single call cannot blow out the assistant's context
budget. The mapping (tool → list key) lives in
[`engine/mcp/handlers.py`](../../engine/mcp/handlers.py) and is reflected
in the `paginated` field of each tool in the YAML map. **Status: pass.**

### F4 — Schema fragments are no longer aliased (refactor)

Before this cycle, the reusable JSON-Schema snippets (`_PORTFOLIO_ID_PROP`,
`_PAGINATION_PROPS`, `_DATE_RANGE_PROPS`, and the inline `symbol`
property) were module-level dicts **shared by reference** across multiple
tool definitions. That is a latent mutation hazard: an in-place edit of
one tool's fragment would have silently mutated every other tool that
aliased it. The `symbol` property was also duplicated in four tools.

The audit flagged this as Step-2 drift. It is now fixed in
[`tool_definitions.py`](../../engine/mcp/tool_definitions.py): each
fragment is produced by a small **factory function**
(`_symbol_prop`, `_portfolio_id_prop`, `_date_range_props`,
`_pagination_props`) that returns a fresh dict per call. The advertised
schemas are byte-for-byte identical to before (verified by regenerating
the surface map). **Status: fixed.**

### F5 — One structural gap remains: the transport entry point

Every component above is implemented and unit-tested, but the
transport-binding entry point (`engine/mcp/server.py`) that wires these
components to the `mcp` SDK's `Server` + stdio/HTTP transport is **not
present on disk**. `pyproject.toml` still references it
(`"engine/mcp/server.py" = ["PLR0911"]`), which is why lint expects it.
Until it lands the module is a library of MCP primitives, not a runnable
server. This is tracked in
[known-limitations.md](../known-limitations.md#mcp) and is **out of
scope** for this audit (it is a missing file, not surface drift).

## Surface inventory (summary)

### Tools

| Tool | Role | Paginated | Required fields |
|---|---|---|---|
| `get_cost_model` | viewer | — | `symbol`, `quantity`, `price` |
| `get_market_data` | viewer | ✅ bars | `symbol` |
| `get_orders` | viewer | ✅ orders | — |
| `get_performance_metrics` | viewer | — | `equity_curve` |
| `get_portfolio_status` | viewer | — | — |
| `get_position` | viewer | — | `symbol` |
| `get_positions` | viewer | — | — |
| `get_strategy_details` | viewer | — | `strategy_name` |
| `get_unrealized_pnl` | viewer | — | — |
| `list_strategies` | viewer | ✅ strategies | — |
| `run_backtest` | quant_dev | — | `strategy_name`, `symbol`, `start_date`, `end_date` |

The full per-tool property list, description, and MCP annotations are in
[`api-surface-map.yaml`](api-surface-map.yaml) and the dedicated
[tool catalog](tool-catalog.md).

### Resources

| URI | Contents |
|---|---|
| `nexus://strategies/catalog` | Discovered strategy manifests. |
| `nexus://symbols/list` | Commonly-traded symbol universe. |
| `nexus://timeframes/list` | `["1m","5m","15m","1h","1d","1wk","1mo"]`. |
| `nexus://risk-parameters/ranges` | min/max/default for risk params. |
| `nexus://cost-model/defaults` | `DefaultCostModel` scalars. |

### Error codes

| Code | Name | Surface |
|---|---|---|
| -32700 | `PARSE_ERROR` | JSON-RPC protocol |
| -32600 | `INVALID_REQUEST` | JSON-RPC protocol |
| -32601 | `METHOD_NOT_FOUND` | JSON-RPC protocol |
| -32602 | `INVALID_PARAMS` | JSON-RPC protocol |
| -32603 | `INTERNAL_ERROR` | JSON-RPC protocol |
| -32001 | `AUTHENTICATION_ERROR` | JSON-RPC protocol |
| -32002 | `AUTHORIZATION_ERROR` | JSON-RPC protocol |
| -32003 | `RATE_LIMIT_ERROR` | JSON-RPC protocol |
| -32004 | `ENGINE_ERROR` | tool-content (`isError=true`) |
| -32005 | `NOT_FOUND_ERROR` | tool-content (`isError=true`) |

## How this map stays accurate

`api-surface-map.yaml` is **generated**, not hand-maintained, so it cannot
drift from the code:

```bash
uv run python scripts/generate_mcp_api_surface.py     # regenerate
uv run python scripts/generate_mcp_api_surface.py --check   # CI guard
```

The generator imports the live modules
(`tool_definitions`, `resources`, `handlers`, `errors`, `auth`) and emits
the map. `--check` regenerates to a temp buffer and exits non-zero if the
committed file differs, so CI can block a PR that changes the surface
without refreshing the map.

## See also

- [Tool catalog](tool-catalog.md) — the per-tool reference this audit
  summarises.
- [`api-surface-map.yaml`](api-surface-map.yaml) — the machine-readable
  inventory.
- [MCP server](../mcp-server.md) — the design narrative (auth model,
  ownership, rate limiting, result safety).
- [`scripts/generate_mcp_api_surface.py`](../../scripts/generate_mcp_api_surface.py)
  — the generator.
