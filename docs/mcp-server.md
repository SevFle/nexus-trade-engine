# MCP Server

Nexus exposes its engine to AI assistants through an
[Model Context Protocol](https://modelcontextprotocol.io/) (MCP) server
under [`engine/mcp/`](../engine/mcp/). MCP is a JSON-RPC protocol that lets
an LLM *call* well-typed tools and *read* static resources, so a developer
can say "backtest mean-reversion on AAPL for 2023 and tell me the Sharpe"
and the assistant drives the engine directly.

> **Status: partial (preview).** Every piece of the server *domain layer*
> ships — tool catalog, resource catalog, auth, dispatch, pagination,
> rate-limiting, error mapping, progress, observability. What is **not**
> yet present is the top-level *transport assembly* that wires those pieces
> into a runnable `stdio`/`http` process (see
> [known-limitations.md](known-limitations.md)). Read this doc as the
> contract the finished server will honour; the per-component code is the
> source of truth today and is exercised by `engine.mcp.handlers.dispatch_tool`.

## Where it lives

| Path | Role |
|------|------|
| [`mcp/tool_definitions.py`](../engine/mcp/tool_definitions.py) | Declarative tool catalog (name, JSON Schema, RBAC role, hints). |
| [`mcp/resources.py`](../engine/mcp/resources.py) | Static reference resources (`nexus://...`). |
| [`mcp/handlers.py`](../engine/mcp/handlers.py) | `dispatch_tool()` — validates args, runs the adapter, paginates, guards size. |
| [`mcp/adapters/`](../engine/mcp/adapters/) | One adapter per tool: `(services, principal, arguments) -> dict`. Bridges to the real engine. |
| [`mcp/auth.py`](../engine/mcp/auth.py) | `AuthPrincipal` + `extract_principal()` — reuses engine JWT/RBAC. |
| [`mcp/pagination.py`](../engine/mcp/pagination.py) | Cursor pagination + `ResultGuard` token-budget cap. |
| [`mcp/rate_limiter.py`](../engine/mcp/rate_limiter.py) | Per-principal token bucket. |
| [`mcp/errors.py`](../engine/mcp/errors.py) | Typed `MCPError` hierarchy + `map_engine_exception()`. |
| [`mcp/progress.py`](../engine/mcp/progress.py) | `ProgressReporter` (no-op safe) around `notifications/progress`. |
| [`mcp/observability.py`](../engine/mcp/observability.py) | structlog + `mcp.*` metrics on the pluggable backend. |
| [`mcp/config.py`](../engine/mcp/config.py) | `MCPServerSettings` — every `NEXUS_MCP_*` knob. |

The MCP server is a **standalone process**, deliberately decoupled from
[`engine/app.py`](../engine/app.py) (the FastAPI app). It is *not* mounted as
a router; it shares code with the REST surface (JWT validation, RBAC
hierarchy, cost model, portfolio type) but has its own lifecycle so it can
run as a `stdio` child of an IDE without the full API/DB stack.

## Design principles

1. **Same identity model as REST.** An assistant authenticated over MCP is
   indistinguishable from one authenticated over
   [`POST /api/v1/auth/login`](api-reference.md). The server calls the
   *exact* [`decode_token`](../engine/api/auth/jwt.py) the REST dependency
   uses, and `require_role` checks the *same* `ROLE_HIERARCHY`. No second
   auth system to drift.
2. **LLM safety over completeness.** Responses are size-guarded
   (`ResultGuard`, ~24 000 tokens), list tools are cursor-paginated
   (default 50, max 500), and engine tracebacks are *never* surfaced —
   `map_engine_exception()` collapses everything unexpected into an opaque
   `EngineError` so internal detail can't leak into the model's context.
3. **Read-only across the board.** All nine tools are annotated
   `readOnlyHint` (none is `destructiveHint`). The only compute tool,
   `run_backtest`, is sandboxed — historical data only, never places
   live orders, no persistent side effects — and is additionally gated
   behind `quant_dev`.
4. **Injectable services.** Adapters take an `EngineServices` container, so
   the server runs in two modes: **online** (default factories hit Yahoo +
   read manifests from disk) and **hermetic** (`EngineServices.for_testing()`
   with fakes — no network, no DB).

## Tools

Each tool carries a JSON Schema (advertised as MCP `inputSchema`) and a
minimum RBAC role. Roles use the same hierarchy as REST
([api-reference.md](api-reference.md)): a principal at level `R` may call a
tool whose `required_role` is at level `≤ R`.

| Tool | Role | Paginated | Notes |
|------|------|-----------|-------|
| `run_backtest` | `quant_dev` | no | Compute-only. Never places live orders. |
| `get_portfolio_status` | `viewer` | no | Cash, market value, total return %, realized P&L. |
| `get_positions` | `viewer` | no | Open positions: qty, avg cost, price, weight. |
| `get_orders` | `viewer` | **yes** | Order history (chronological). |
| `list_strategies` | `viewer` | **yes** | Installed-strategy catalog. |
| `get_strategy_details` | `viewer` | no | Full metadata for one strategy. |
| `get_market_data` | `viewer` | **yes** | OHLCV bars by symbol/interval/period. |
| `get_cost_model` | `viewer` | no | Cost breakdown for a hypothetical trade. |
| `get_performance_metrics` | `viewer` | no | Sharpe/Sortino/max-DD/etc. from an equity curve. |

### Argument shapes (canonical)

```jsonc
// run_backtest
{ "strategy_name": "mean_reversion_basic", "symbol": "AAPL",
  "start_date": "2023-01-01", "end_date": "2023-12-31",
  "initial_capital": 100000 }            // default 100000

// get_market_data
{ "symbol": "AAPL", "interval": "1d",    // enum: 1m|5m|15m|1h|1d|1wk|1mo
  "period": "1y", "limit": 50, "cursor": "<opaque>" }

// get_cost_model
{ "symbol": "AAPL", "quantity": 100, "price": 189.50,
  "side": "buy", "avg_volume": 0 }        // avg_volume feeds the slippage model

// get_performance_metrics
{ "equity_curve": [ {"timestamp": "...", "total_value": 100000}, /* ≥2 points */ ],
  "initial_capital": 100000 }
```

`get_portfolio_status`, `get_positions`, and `get_orders` accept an optional
`portfolio_id` (defaults to the in-memory `default` portfolio seeded with
$100 000). `list_strategies` and `get_strategy_details` take no / one
`strategy_name` argument respectively.

`run_backtest` deliberately returns a **compact summary** — final capital,
total return %, trade count, scalar metrics, and the strategy evaluator's
verdict. It never embeds the full equity curve or trade log in the response
(callers page through those via `get_market_data` / `get_orders` if needed).

## Resources

Resources are read without a tool call — the assistant reads them for
context. All return `application/json`.

| URI | Contents |
|-----|----------|
| `nexus://strategies/catalog` | Discovered strategy manifests (name, version, author, symbols, params). |
| `nexus://symbols/list` | Seed symbol universe (AAPL, MSFT, GOOGL, AMZN, SPY). |
| `nexus://timeframes/list` | `1m, 5m, 15m, 1h, 1d, 1wk, 1mo`. |
| `nexus://risk-parameters/ranges` | min/max/default for position %, drawdown, stop, take-profit, max positions. |
| `nexus://cost-model/defaults` | Commission, spread/slippage bps, tax rates, wash-sale window. |

## Authentication & authorization

Because `stdio` transport has no HTTP headers, credentials are resolved in
priority order ([`mcp/auth.py`](../engine/mcp/auth.py)):

1. Per-request `_meta.authorization` (`Bearer <jwt>`) or `_meta.api_key`.
2. The static API-key table (`NEXUS_MCP_STATIC_API_KEYS`, a JSON
   `{ "<token>": "<role>" }` map — DB-free service tokens).
3. The process-level `NEXUS_MCP_TOKEN` (the standard way to pass a JWT to a
   local `stdio` server).

Resolution stops at the first hit. A JWT is validated with the engine's
[`decode_token`](../engine/api/auth/jwt.py) (so expiry, signature, and
`NEXUS_SECRET_KEY` rotation all apply). The resulting `AuthPrincipal` carries
`user_id`, `role`, `email`, `scopes`, and `auth_method`
(`jwt | api_key | anonymous`).

When `NEXUS_MCP_AUTH_REQUIRED=false` (local dev only), an anonymous principal
with `NEXUS_MCP_DEFAULT_ROLE` is issued for every request. RBAC is then
enforced by `require_role(principal, tool.required_role)` — same
`ROLE_HIERARCHY` as REST, so a `viewer` principal cannot call `run_backtest`.

## Request handling pipeline

```
tools/call  ──▶  extract_principal(_meta)        ──▶  AuthPrincipal  (or AuthenticationError)
           ──▶  RateLimiter.check(principal)     ──▶  (or RateLimitError)
           ──▶  dispatch_tool(name, args, …)
                  ├─ get_tool(name)              ──▶  (or ValidationError: unknown tool)
                  ├─ _validate_arguments(schema) ──▶  (or ValidationError: missing required)
                  ├─ require_role(principal, def.required_role)
                  ├─ adapter(services, principal, args)
                  ├─ paginate(list-heavy results)
                  └─ ResultGuard.guard(result)   ──▶  trim if > token budget
           ──▶  observability: mcp.tool.call / .duration_ms / .error
           ──▶  CallToolResult(text=to_jsonable(result))
```

Expected failures are surfaced as a `CallToolResult` with `isError=True`
(the spec-recommended path — it does not tear down the session). Transport /
auth rejections raise a JSON-RPC error instead. See [Errors](#errors)
below.

## Pagination & size guard

List-heavy tools (`get_orders`, `get_market_data`, `list_strategies`)
return a cursor page:

```jsonc
{ "items": [ /* ≤ limit rows */ ],
  "limit": 50, "next_cursor": "<opaque or null>" }
```

The cursor is an opaque base64 offset. `limit` is clamped to
`[1, NEXUS_MCP_MAX_PAGE_SIZE]` (default 50, max 500). Independently,
`ResultGuard` estimates the response size at ~4 chars/token and, if it
exceeds `NEXUS_MCP_RESULT_TOKEN_BUDGET` (default 24 000), trims the largest
list value to fit — a backstop so a misbehaving tool can never blow out the
assistant's context window.

## Errors

Defined in [`mcp/errors.py`](../engine/mcp/errors.py).

| Error class | Code | Raised when |
|-------------|------|-------------|
| `AuthenticationError` | `-32001` | No/invalid credential and `auth_required=true`. |
| `AuthorizationError` | `-32002` | Principal's role < tool's `required_role`. |
| `RateLimitError` | `-32003` | Token bucket exhausted. |
| `ValidationError` | `-32602` (`INVALID_PARAMS`) | Bad/missing tool arguments. |
| `NotFoundError` | `-32005` | Referenced strategy/portfolio/position absent. |
| `EngineError` | `-32004` | Any other engine failure. **No traceback is leaked.** |

`map_engine_exception()` normalizes arbitrary engine exceptions: messages
containing `"not found"` / `"no position"` → `NotFoundError`;
`ValueError`/`TypeError` → `ValidationError`; everything else → a generic
`EngineError` carrying only the exception class name.

## Rate limiting

[`RateLimiter`](../engine/mcp/rate_limiter.py) is an in-memory, per-principal
token bucket (`NEXUS_MCP_RATE_LIMIT_PER_MINUTE=120`,
`NEXUS_MCP_RATE_LIMIT_BURST=30` by default). Each authenticated identity gets
an independent bucket so one chatty client cannot starve another. State is
process-local — there is no Valkey-backed shared limiter today, so the
effective limit is `per_minute × server_instances`.

## Observability

MCP traffic reuses the engine's existing primitives so it lands in the same
dashboards as REST/WebSocket:

- **Logs** — `structlog` events keyed by tool name and principal (the public
  `to_public_dict()` summary; the raw token is never logged).
- **Metrics** — emitted on the pluggable
  [`MetricsBackend`](adr/0008-pluggable-metrics-backend.md) under the
  `mcp.*` namespace: `mcp.tool.call`, `mcp.tool.duration_ms`,
  `mcp.tool.error`.

## Configuration

All knobs live in [`mcp/config.py`](../engine/mcp/config.py) as
`MCPServerSettings`, env-prefixed `NEXUS_MCP_`. The full set is mirrored in
[`.env.example`](../.env.example).

| Variable | Default | Purpose |
|----------|---------|---------|
| `NEXUS_MCP_SERVER_NAME` | `nexus-mcp-server` | Advertised server identity. |
| `NEXUS_MCP_SERVER_VERSION` | `0.1.0` | Advertised version. |
| `NEXUS_MCP_INSTRUCTIONS` | *(help text)* | Server `instructions` string the model reads on connect. |
| `NEXUS_MCP_TRANSPORT` | `stdio` | `stdio` \| `http`. |
| `NEXUS_MCP_HTTP_HOST` | `127.0.0.1` | Bind host for HTTP transport. |
| `NEXUS_MCP_HTTP_PORT` | `8765` | Bind port for HTTP transport. |
| `NEXUS_MCP_HTTP_PATH` | `/mcp` | HTTP endpoint path. |
| `NEXUS_MCP_AUTH_REQUIRED` | `true` | `false` → anonymous principal in dev. |
| `NEXUS_MCP_DEFAULT_ROLE` | `viewer` | Role for anonymous sessions. |
| `NEXUS_MCP_TOKEN` | `""` | Process-level JWT/engine token (stdio). |
| `NEXUS_MCP_STATIC_API_KEYS` | `""` | JSON `{"<token>": "<role>"}` service-token map. |
| `NEXUS_MCP_RATE_LIMIT_PER_MINUTE` | `120` | Per-principal bucket refill. |
| `NEXUS_MCP_RATE_LIMIT_BURST` | `30` | Per-principal burst ceiling. |
| `NEXUS_MCP_RESULT_TOKEN_BUDGET` | `24000` | Soft response-size cap (~4 chars/token). |
| `NEXUS_MCP_DEFAULT_PAGE_SIZE` | `50` | Default cursor page size. |
| `NEXUS_MCP_MAX_PAGE_SIZE` | `500` | Hard cap on `limit`. |
| `NEXUS_MCP_BACKTEST_PROGRESS_INTERVAL` | `0` | Equity-points between progress notices (0 = off). |
| `NEXUS_MCP_BACKTEST_MAX_BARS` | `50000` | Hard cap on bars per backtest. |
| `NEXUS_MCP_BACKTEST_DEFAULT_PROVIDER` | `yahoo` | Market-data provider id. |

## Extending the server

| Adding… | Goes in |
|---------|---------|
| A new tool | A `ToolDefinition` in `tool_definitions.py` + an adapter in `adapters/` + a `_register(name)` entry in `handlers.py`. Pick the minimum `required_role` deliberately. |
| A new resource | A `ResourceDefinition` + a `match` arm in `resources.py:read_resource`. |
| A new auth source | A branch in `auth.py:extract_principal` (today: JWT, static key, anonymous). |
| A new cost/strategy capability | The adapter, not the transport — `EngineServices` is the seam. |

Design rule: keep tools **small and read-only** unless there is no
alternative. Compute tools (`run_backtest`) are acceptable because they are
historical and side-effect-free; a tool that *trades live* would need its own
ADR before landing.
