# MCP server

Nexus Trade Engine ships a [Model Context Protocol][mcp] surface under
[`engine/mcp/`](../../engine/mcp/) so an AI assistant (Claude Desktop,
a custom agent, a CI helper) can drive the engine through the same
domain logic the REST API uses — run backtests, inspect portfolios,
list strategies, pull market data and cost-model defaults.

[mcp]: https://modelcontextprotocol.io/

> **Status — read this first.** The MCP module is a **library, not a
> running server**. Every layer below the wire transport is implemented
> — tool catalogue, request handlers, adapters, resources, auth, rate
> limiting, pagination, observability, errors — but there is **no
> `FastMCP()` instance, no `python -m engine.mcp` runner, and no
> stdio/HTTP transport bootstrap** in the tree. `pyproject.toml`
> references `engine/mcp/server.py` in its lint-ignore map, but that
> file does not exist on `main`. Until the transport lands, the module
> is reachable only via its unit tests and by code that imports the
> adapters directly. See [known-limitations.md](../known-limitations.md)
> for the tracked gap.

## Why MCP, and why now

The REST API is built for human-driven dashboards and SDK clients:
many small endpoints, each with its own auth header, pagination, and
error shape. An LLM agent does not want that surface. It wants a
small, self-describing set of *tools* it can call with structured
arguments, plus a few *resources* it can read for ambient context.
MCP is the wire protocol that bridges those two worlds without us
hand-rolling a "talk to the LLM" HTTP shim.

We chose to share the engine's domain code rather than re-implement
it for MCP:

- **One source of truth for behaviour.** `run_backtest` over MCP and
  `POST /api/v1/backtest/run` over REST eventually call the same
  backtest runner. Drift is impossible by construction.
- **Same auth principal.** MCP requests authenticate to the *same*
  `AuthPrincipal` model the REST API uses, so RBAC scopes are
  identical (see [api-reference.md](../api-reference.md)).
- **Same observability.** MCP tool calls emit `mcp.tool.*` metrics on
  the same pluggable `MetricsBackend` REST/WebSocket use
  ([ADR 0008](../adr/0008-pluggable-metrics-backend.md)).

## Layout

| Path | Role |
|---|---|
| [`config.py`](../../engine/mcp/config.py) | `MCPServerSettings` — every `NEXUS_MCP_*` knob lives here. |
| [`tool_definitions.py`](../../engine/mcp/tool_definitions.py) | Declarative tool catalogue (name, JSON Schema, required role, hints). |
| [`handlers.py`](../../engine/mcp/handlers.py) | `dispatch_tool()` — routes a `tools/call` to the right adapter, applies pagination, runs the `ResultGuard`. |
| [`adapters/`](../../engine/mcp/adapters/) | One adapter per tool family. Pure `async (services, principal, args) -> dict`. |
| [`adapters/__init__.py`](../../engine/mcp/adapters/__init__.py) | `EngineServices` DI container + `PortfolioStore` + JSON normaliser. |
| [`resources.py`](../../engine/mcp/resources.py) | Static reference resources (`nexus://strategies/catalog`, …) served via `resources/list` + `read`. |
| [`auth.py`](../../engine/mcp/auth.py) | `AuthPrincipal` + `extract_principal()` + `require_role()`. |
| [`errors.py`](../../engine/mcp/errors.py) | Typed error hierarchy + `map_engine_exception()`. |
| [`pagination.py`](../../engine/mcp/pagination.py) | Cursor pagination + `ResultGuard` token-budget cap. |
| [`rate_limiter.py`](../../engine/mcp/rate_limiter.py) | Per-principal token-bucket rate limiter. |
| [`progress.py`](../../engine/mcp/progress.py) | `ProgressReporter` for long-running backtest tools. |
| [`observability.py`](../../engine/mcp/observability.py) | structlog events + `mcp.tool.*` metric tags. |

The intended (not yet landed) `server.py` would wire these into a
`FastMCP` instance, register the tools from `mcp_tools()`, the
resources from `list_resources()`, and dispatch `tools/call` through
`dispatch_tool()`.

## Tool catalogue

Nine tools, all `read_only=True` and `idempotent=True` except where
noted. Every tool carries a `required_role` (default `viewer`);
`run_backtest` requires `quant_dev` because it is compute-heavy.

| Tool | Role | Paginated? | Notes |
|---|---|---|---|
| `run_backtest` | `quant_dev` | — | Historical backtest of one strategy on one symbol. Compute-only; never places live orders. Supports a `ProgressReporter` for equity-curve progress notifications. |
| `get_portfolio_status` | `viewer` | — | Cash, market value, total return %, realized P&L. |
| `get_positions` | `viewer` | — | Open positions with avg cost / weight. |
| `get_orders` | `viewer` | ✅ `orders` | Chronological trade history. |
| `list_strategies` | `viewer` | ✅ `strategies` | Installed-strategy catalogue from the plugin registry. |
| `get_strategy_details` | `viewer` | — | Manifest details for one strategy. |
| `get_market_data` | `viewer` | ✅ `bars` | OHLCV bars for a symbol/interval/period. |
| `get_cost_model` | `viewer` | — | Transaction-cost-model defaults (commission, spread, slippage, tax rates, wash-sale window). |
| `get_performance_metrics` | `viewer` | — | Rolling/performance metrics for a portfolio. |

Pagination is cursor-based (`cursor` opaque, `limit` 1–500, default
50). Paginated tools return `{items_key: [...], next_cursor,
has_more, limit}`; the `ResultGuard` then caps the whole response at
`NEXUS_MCP_RESULT_TOKEN_BUDGET` (~4 chars/token, default 24 000) so a
single tool call cannot blow out the assistant's context window.

## Resources

Static, read-only context the assistant can pull without a tool call.
All served as `application/json`.

| URI | Contents |
|---|---|
| `nexus://strategies/catalog` | Discovered strategy manifests (name, version, parameters, symbols, timeframe). |
| `nexus://symbols/list` | Curated symbol universe (AAPL, MSFT, GOOGL, AMZN, SPY today). |
| `nexus://timeframes/list` | `1m, 5m, 15m, 1h, 1d, 1wk, 1mo`. |
| `nexus://risk-parameters/ranges` | Min/max/default for `max_position_pct`, `max_drawdown_pct`, `stop_loss_pct`, `take_profit_pct`, `max_open_positions`. |
| `nexus://cost-model/defaults` | Live `DefaultCostModel` fields — same object the engine hands strategies. |

## Auth model

The MCP server reuses the engine's JWT validator
([`engine.api.auth.jwt.decode_token`](../../engine/api/auth/jwt.py))
and the `ROLE_HIERARCHY` from
[`engine/api/auth/dependency.py`](../../engine/api/auth/dependency.py),
so a principal authenticated over MCP is indistinguishable from one
authenticated over REST. Roles gate tools the same way
`require_role()` gates routes.

Because stdio MCP has no HTTP headers, the credential is resolved in
priority order inside `extract_principal()`:

1. Per-request `_meta.authorization` (`Bearer <jwt>`) or
   `_meta.api_key`. Works on both transports.
2. The static API-key table (`NEXUS_MCP_STATIC_API_KEYS`, a JSON
   `{"<token>": "<role>"}` map). DB-free service tokens — useful for
   machine-to-machine MCP.
3. The process-level `NEXUS_MCP_TOKEN` — the standard way to pass a
   credential to a local stdio server.

When `NEXUS_MCP_AUTH_REQUIRED=false` (local dev), an anonymous
principal with the configured `NEXUS_MCP_DEFAULT_ROLE` is issued.

### Rate limiting

[`RateLimiter`](../../engine/mcp/rate_limiter.py) is an in-memory,
per-principal token bucket: `NEXUS_MCP_RATE_LIMIT_PER_MINUTE` (120)
refill, `NEXUS_MCP_RATE_LIMIT_BURST` (30) ceiling. A principal that
exhausts its bucket gets a `RateLimitError` carrying
`retry_after_seconds`. The limiter is keyed on the *principal*, not
the connection, so a single noisy tool call cannot starve other
principals on the same server process.

> **Cross-replica caveat.** The bucket is per-process. A multi-replica
> MCP deployment effectively multiplies the limit by the replica
> count. This matches the default REST rate-limiter behaviour; flip
> `NEXUS_RATE_LIMIT_VALKEY_ENABLED=true` on the REST side for a
> distributed backend (the MCP limiter does not yet share that path).

## Errors

Two surfaces, mirroring the MCP spec:

- **JSON-RPC protocol errors** (`-32xxx` and the server-reserved
  `-32001…-32005`) tear down the request. Used for transport/auth
  failures where no tool result is meaningful.
- **Tool execution errors** return a `CallToolResult` with
  `isError=True` — the spec-recommended way to surface validation,
  engine, and operational errors to the assistant without dropping
  the session.

`map_engine_exception()` normalises engine exceptions onto this
hierarchy so adapters can `raise` domain errors and let the
dispatcher worry about the wire shape.

## DI: `EngineServices`

Every adapter is a pure `async (services, principal, args) -> dict`
function; the only state it touches is the injected
`EngineServices` container. That keeps adapters trivially unit-
testable and lets the server run in two modes:

- **Online** — default factories build real engine objects
  (`PluginRegistry`, `DefaultCostModel`, the configured market-data
  provider).
- **Hermetic** — `EngineServices.for_testing(...)` accepts fakes
  (in-memory provider, stub registry) so the test suite needs no
  network or DB.

`PortfolioStore` is an in-memory portfolio bag (one `$100 000`
"default" portfolio seeded on construction) — it deliberately does
*not* couple to the SQLAlchemy `portfolios` table. That keeps the
MCP surface usable for read-only introspection today; wiring it to
the durable portfolio table is part of the live-trading roadmap.

## Configuration

All settings are `NEXUS_MCP_*`-prefixed (kept separate from the main
`NEXUS_*` block in [`engine/config.py`](../../engine/config.py) so
the MCP server can boot standalone without pulling in the full
API/DB surface). Highlights:

| Variable | Default | Notes |
|---|---|---|
| `NEXUS_MCP_TRANSPORT` | `stdio` | `stdio` \| `http` (transport selection — honoured once the server entrypoint lands). |
| `NEXUS_MCP_HTTP_HOST` / `_PORT` / `_PATH` | `127.0.0.1` / `8765` / `/mcp` | HTTP transport bind. |
| `NEXUS_MCP_AUTH_REQUIRED` | `true` | Flip to `false` for local dev. |
| `NEXUS_MCP_DEFAULT_ROLE` | `viewer` | Role granted to anonymous sessions. |
| `NEXUS_MCP_TOKEN` | `""` | Process-level credential for stdio. |
| `NEXUS_MCP_STATIC_API_KEYS` | `""` | JSON `{"token":"role"}` map. |
| `NEXUS_MCP_RATE_LIMIT_PER_MINUTE` / `_BURST` | `120` / `30` | Per-principal token bucket. |
| `NEXUS_MCP_RESULT_TOKEN_BUDGET` | `24000` | Soft cap on response tokens. |
| `NEXUS_MCP_DEFAULT_PAGE_SIZE` / `MAX_PAGE_SIZE` | `50` / `500` | Pagination bounds. |
| `NEXUS_MCP_BACKTEST_PROGRESS_INTERVAL` | `0` | `0` disables progress notifications. |
| `NEXUS_MCP_BACKTEST_MAX_BARS` | `50000` | Hard cap on bars per backtest. |
| `NEXUS_MCP_BACKTEST_DEFAULT_PROVIDER` | `yahoo` | Provider the market-data adapter resolves. |

## What's missing (and what to do about it)

See [known-limitations.md](../known-limitations.md) for the tracked
"MCP server has no entrypoint" item. Concretely, landing a runnable
server needs:

1. A `server.py` that builds a `FastMCP` instance, registers
   `mcp_tools()` and `list_resources()`, and dispatches `tools/call`
   through `dispatch_tool()` with auth + rate-limit + observability
   wrappers.
2. A `python -m engine.mcp` entrypoint (and a `Makefile` target) that
   selects `stdio` vs `http` from `NEXUS_MCP_TRANSPORT`.
3. A worker/compose service entry so the HTTP transport can run
   alongside the engine.
4. An end-to-end test that boots the server, lists tools, and calls
   `get_cost_model` against a hermetic `EngineServices`.

Until then, treat `engine/mcp/` as the *implementation* of the MCP
contract and the missing `server.py` as the integration gap.
