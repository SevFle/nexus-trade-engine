# MCP Server

Nexus exposes its read-only engine surface (and compute-only
backtests) to LLM agents over the
[Model Context Protocol](https://modelcontextprotocol.io). This lets
an assistant — Claude Desktop, a custom agent, or any MCP client —
inspect portfolios, list strategies, pull market data, estimate
transaction costs, compute performance metrics, and run backtests
without speaking HTTP.

Everything here lives under [`engine/mcp/`](../engine/mcp/).

> **Status: partial.** Every component below is implemented and
> unit-tested, but the transport-binding entry point
> (`engine/mcp/server.py`) that wires these components to the `mcp`
> SDK's `Server` + stdio/HTTP transport is **not present on disk**.
> `pyproject.toml` references it (`"engine/mcp/server.py" =
> ["PLR0911"]`) which is why CI/lint still expect it. Until that file
> lands the module is a library of MCP primitives, not a runnable
> server. See [known-limitations.md](known-limitations.md#mcp). The
> tool/resource/auth contract documented below is the one the
> components already enforce, so a future `server.py` only has to
> bind transport to `dispatch_tool` / `read_resource` /
> `extract_principal`.

## Why MCP, not just "more REST"

The REST API is optimised for a deterministic client (the React app,
SDK users). An LLM agent is a different consumer:

- It benefits from **self-describing** tools and resources (the
  `description` strings are part of the prompt).
- It needs **bounded output** so a single tool call can't blow out its
  context window — hence `result_token_budget` and cursor pagination.
- It should never, by accident, place a real order. The entire MCP
  surface is **read-only or compute-only**: there is no tool that
  touches the order book, the broker, or persists side effects to the
  trading tables. `run_backtest` is the only "expensive" call and it
  is explicitly annotated `read_only=True, idempotent=True`.

## Module map

| File | Responsibility |
|---|---|
| [`config.py`](../engine/mcp/config.py) | `MCPServerSettings` — every `NEXUS_MCP_*` knob. Kept **separate** from `engine/config.py` so the server can run as a standalone stdio process without the full API/DB surface. |
| [`tool_definitions.py`](../engine/mcp/tool_definitions.py) | Declarative tool catalog: name, description, JSON-Schema `input_schema`, RBAC `required_role`, MCP `ToolAnnotations`. The single source of truth for what tools exist. |
| [`handlers.py`](../engine/mcp/handlers.py) | `dispatch_tool(name, args, services, principal, progress)` — routes a tool call to its adapter, validates args against the schema, applies pagination, guards result size. |
| [`adapters/`](../engine/mcp/adapters/) | One module per tool family: `portfolio_adapter`, `strategy_adapter`, `market_data_adapter`, `backtest_adapter`. Pure async functions `(services, principal, arguments) -> dict`. |
| [`adapters/__init__.py`](../engine/mcp/adapters/__init__.py) | `EngineServices` (injectable capabilities) + `PortfolioStore` (in-memory portfolios) + `to_jsonable` (Decimal/datetime/dataclass → JSON). |
| [`resources.py`](../engine/mcp/resources.py) | Static reference resources served via `resources/list` + `resources/read` (strategy catalog, symbols, timeframes, risk ranges, cost-model defaults). |
| [`auth.py`](../engine/mcp/auth.py) | `extract_principal` + `require_role`. Resolves a credential to an `AuthPrincipal` and enforces the same `ROLE_HIERARCHY` as the REST API. |
| [`rate_limiter.py`](../engine/mcp/rate_limiter.py) | Per-principal in-memory token bucket. |
| [`pagination.py`](../engine/mcp/pagination.py) | Cursor pagination (base64 offset) for list-heavy tools. |
| [`progress.py`](../engine/mcp/progress.py) | `ProgressReporter` wrapping `session.send_progress_notification`. No-op-safe. |
| [`errors.py`](../engine/mcp/errors.py) | `MCPError` hierarchy with JSON-RPC codes; `map_engine_exception` normalises engine errors so tracebacks never leak. |
| [`observability.py`](../engine/mcp/observability.py) | structlog events + counters/histograms via the pluggable `MetricsBackend`. |

## Tools

All nine tools are advertised with `read_only` MCP annotations. The
default `required_role` is `viewer`; only `run_backtest` requires
`quant_dev` because it is compute-intensive. Roles follow the same
`ROLE_HIERARCHY` as the REST API
([`api-reference.md`](api-reference.md#roles-rbac-hierarchy)).

| Tool | Required role | Paginated | Summary |
|---|---|---|---|
| `run_backtest` | `quant_dev` | — | Historical backtest of a strategy on one symbol over a date range. Compute-only, never places live orders. |
| `get_portfolio_status` | `viewer` | — | Cash, total value, total return %, realized P&L. |
| `get_positions` | `viewer` | — | Open positions: qty, avg cost, price, market value, weight. |
| `get_orders` | `viewer` | ✅ | Chronological order history. |
| `list_strategies` | `viewer` | — | Installed strategies: version, description, author, symbols, defaults. |
| `get_strategy_details` | `viewer` | — | Full metadata for one strategy. |
| `get_market_data` | `viewer` | ✅ | OHLCV bars for a symbol + period + interval. |
| `get_cost_model` | `viewer` | — | Transaction-cost breakdown estimate (commission, spread, slippage, fee, tax). |
| `get_performance_metrics` | `viewer` | — | Compute Sharpe/Sortino/max-DD/win-rate/profit-factor from an equity curve. |

### Argument shapes (required fields)

- **`run_backtest`** — `strategy_name`, `symbol`, `start_date`,
  `end_date` (ISO-8601); optional `initial_capital` (default 100000).
- **`get_strategy_details`** — `strategy_name`.
- **`get_market_data`** — `symbol`; optional `interval`
  (`1m|5m|15m|1h|1d|1wk|1mo`, default `1d`), `period` (default `1y`).
- **`get_cost_model`** — `symbol`, `quantity`, `price`; optional
  `side` (`buy|sell`), `avg_volume` (for slippage).
- **`get_performance_metrics`** — `equity_curve`
  (`[{timestamp,total_value}, …]`, ≥2 points); optional
  `initial_capital`.
- **`get_orders` / `get_market_data`** accept pagination params
  `limit` and `cursor` (the opaque cursor from a prior page).

`dispatch_tool` validates required fields against the tool's
`input_schema` before invoking the adapter, returning a
`ValidationError` (MCP `isError=true`) on a mismatch.

### Backtest result shape

`run_backtest` deliberately returns a **summary**, never the full
equity curve or trade log — those would overrun the assistant's
context. Callers that need the series page through the dedicated read
tools. The summary contains: `strategy_name`, `symbol`, date range,
`initial_capital`, `final_capital`, `total_return_pct`,
`total_trades`, `equity_points` (count), a filtered scalar `metrics`
map, `evaluation`, and `equity_curve_truncated` (set to
`result_token_budget`).

## Resources

Cheap, read-only context the assistant can pull without a tool call.
Served at `application/json`.

| URI | Contents |
|---|---|
| `nexus://strategies/catalog` | Discovered strategy manifests (name, version, description, author, symbols, timeframe, parameters). |
| `nexus://symbols/list` | Commonly-traded symbol universe (AAPL, MSFT, GOOGL, AMZN, SPY, …). |
| `nexus://timeframes/list` | `["1m","5m","15m","1h","1d","1wk","1mo"]`. |
| `nexus://risk-parameters/ranges` | min/max/default/unit for `max_position_pct`, `max_drawdown_pct`, `stop_loss_pct`, `take_profit_pct`, `max_open_positions`. |
| `nexus://cost-model/defaults` | The engine `DefaultCostModel` scalars (commission, spread/slippage bps, tax rates, wash-sale window). |

## Auth model

`extract_principal` (in [`auth.py`](../engine/mcp/auth.py)) resolves a
credential to an `AuthPrincipal {user_id, email, role, auth_method}`.
Because stdio MCP has no HTTP headers, the token is resolved in this
priority order:

1. **Client request metadata** — the `_meta` field on the MCP request
   params (the canonical place; some clients put it in a custom key,
   which `_extract_token` also tolerates).
2. **Static API-key table** — `NEXUS_MCP_STATIC_API_KEYS`, a JSON map
   `{"<token>": "<role>"}` for DB-free service-to-service auth.
3. **Process-level token** — `NEXUS_MCP_TOKEN`, used when the client
   cannot set metadata at all (e.g. a daemon wrapper).

Token resolution, in order:

1. If `NEXUS_MCP_AUTH_REQUIRED=false`, return an **anonymous**
   principal with role `NEXUS_MCP_DEFAULT_ROLE` (default `viewer`).
   This is the local-dev fast path.
2. Try to decode the token as an **engine JWT** (reuses the exact
   `decode_token` validator the REST API uses) → `auth_method="jwt"`.
3. Else look the token up in the **static API-key map** →
   `auth_method="api_key"`.
4. Else raise `AuthenticationError` → JSON-RPC error to the client.

`require_role(principal, minimum_role)` is enforced per tool by
`dispatch_tool`. The hierarchy is shared with REST
(`admin > portfolio_manager > developer > quant_dev > retail_trader >
user > viewer`), so a principal authenticated over MCP is
indistinguishable from one authenticated over HTTP for authorization
purposes.

## Rate limiting

A per-principal **in-memory token bucket**
([`rate_limiter.py`](../engine/mcp/rate_limiter.py)) gates every tool
dispatch. Defaults: `rate_limit_per_minute=120`, `burst=30`. A
principal over budget gets `RateLimitError` with a
`retry_after_seconds` hint. The bucket key is the principal identity
(`user_id` for JWTs, `api-key` for static keys, `anonymous` when auth
is disabled) — *not* the raw token, so rotating a token does not reset
the limit.

> In-memory means per-process. A multi-process HTTP deployment would
> need a shared backend (Valkey) to enforce a global limit — the same
> trade-off the REST rate limiter makes (see
> [`api-reference.md`](api-reference.md#cross-cutting-middleware)).

## Result safety

| Setting | Default | Purpose |
|---|---|---|
| `result_token_budget` | `24_000` | Soft cap (~4 chars/token) on a response payload. `ResultGuard` trims oversize lists. |
| `default_page_size` | `50` | Default `limit` for paginated tools. |
| `max_page_size` | `500` | Hard ceiling on a single page. |
| `backtest_max_bars` | `50_000` | Caps how much market history a single backtest can scan. |

`to_jsonable` also normalises non-JSON values: `Decimal → float`,
`datetime → ISO-8601`, `NaN/inf → null`. A metrics computation that
produces `NaN` cannot corrupt the response.

## Transports

`MCPServerSettings.transport` selects one of:

- **`stdio`** (default) — the canonical MCP local-server mode. The
  client spawns the server as a subprocess and talks JSON-RPC over
  its stdin/stdout. This is why MCP config is split out from
  `engine.config`: a stdio process should not need a DB URL or the
  full FastAPI surface.
- **`http`** — `http_host`/`http_port`/`http_path` (defaults
  `127.0.0.1:8765/mcp`), for a long-lived, remotely-addressable
  server. `http_log_level` controls uvicorn-style logging.

The eventual `server.py` is responsible for instantiating the
`mcp.server` `Server`, registering the `tools/list`, `tools/call`,
`resources/list`, `resources/read` handlers against `dispatch_tool` /
`read_resource` / `list_resources`, threading `extract_principal` +
`RateLimiter` through every call, then running the chosen transport.

## Configuration

All settings live in [`engine/mcp/config.py`](../engine/mcp/config.py)
under the `NEXUS_MCP_` prefix and/or `.env`.

| Variable | Default | Notes |
|---|---|---|
| `NEXUS_MCP_SERVER_NAME` | `nexus-mcp-server` | Server identity in the MCP handshake. |
| `NEXUS_MCP_SERVER_VERSION` | `0.1.0` | |
| `NEXUS_MCP_INSTRUCTIONS` | *(see source)* | Server-level instructions the client shows the model. |
| `NEXUS_MCP_TRANSPORT` | `stdio` | `stdio` \| `http`. |
| `NEXUS_MCP_HTTP_HOST` | `127.0.0.1` | |
| `NEXUS_MCP_HTTP_PORT` | `8765` | |
| `NEXUS_MCP_HTTP_PATH` | `/mcp` | |
| `NEXUS_MCP_HTTP_LOG_LEVEL` | `info` | |
| `NEXUS_MCP_AUTH_REQUIRED` | `true` | `false` → anonymous `default_role` principal. |
| `NEXUS_MCP_DEFAULT_ROLE` | `viewer` | Role for anonymous sessions. |
| `NEXUS_MCP_TOKEN` | `""` | Process-level JWT/engine token (see auth resolution). |
| `NEXUS_MCP_STATIC_API_KEYS` | `""` | JSON `{"<token>":"<role>"}` for DB-free auth. |
| `NEXUS_MCP_RATE_LIMIT_PER_MINUTE` | `120` | Per-principal bucket refill. |
| `NEXUS_MCP_RATE_LIMIT_BURST` | `30` | Per-principal bucket ceiling. |
| `NEXUS_MCP_RESULT_TOKEN_BUDGET` | `24000` | Soft response size cap. |
| `NEXUS_MCP_DEFAULT_PAGE_SIZE` | `50` | |
| `NEXUS_MCP_MAX_PAGE_SIZE` | `500` | |
| `NEXUS_MCP_BACKTEST_PROGRESS_INTERVAL` | `0` | `0` disables intra-run progress; `>0` emits every N equity points. |
| `NEXUS_MCP_BACKTEST_MAX_BARS` | `50000` | |
| `NEXUS_MCP_BACKTEST_DEFAULT_PROVIDER` | `yahoo` | Provider name passed to `get_data_provider`. |

> These are inventoried in [`.env.example`](../.env.example) under the
> `# ── MCP server (engine/mcp) ──` block, so operators can see the full
> surface. The one piece still missing is the transport entry point
> itself — `engine/mcp/server.py` (see
> [known-limitations.md](known-limitations.md#mcp)).

## Engine services (`EngineServices`)

The server never reaches into engine internals directly — it goes
through the `EngineServices` container
([`adapters/__init__.py`](../engine/mcp/adapters/__init__.py)), which
holds:

- `plugin_registry` — strategy discovery + loading.
- `portfolio_store` — in-memory `Portfolio` objects (a `default`
  portfolio seeded with $100,000 is created on construction). This is
  *not* the SQLAlchemy portfolio table; it lets the server expose
  portfolio inspection without a DB session.
- `cost_model` — a `DefaultCostModel` for `get_cost_model`.
- `market_data_provider_factory` — callable returning a fresh
  `MarketDataProvider` per backtest.
- `strategies_dir` — where `discover_strategies` looks for manifests.

Two construction modes:

- **Online** (default factories) — hits live providers (Yahoo) and
  reads strategy manifests from disk.
- **Hermetic** (`EngineServices.for_testing(...)`) — inject fakes so
  unit tests need no network or DB. `for_testing` captures a single
  provider and returns it on every call, which is what hermetic tests
  want; online deployments construct `EngineServices` directly so a
  fresh provider is built per backtest.

## Observability

[`observability.py`](../engine/mcp/observability.py) records every
tool dispatch through the same pluggable `MetricsBackend` as the REST
API (`engine.observability.metrics`):

- `mcp.tool.calls` (counter, tags: `tool`, `status`)
- `mcp.tool.errors` (counter, same tags)
- `mcp.tool.duration_ms` (histogram, same tags)

plus structlog events keyed `mcp.tool.start` / `mcp.tool.success` /
`mcp.tool.error`. `render_metrics()` exposes the recorded values in
Prometheus text-exposition format (used by an HTTP `/metrics` route
when the server runs over HTTP transport). An unconfigured backend
(`MetricsBackend` default) is a no-op, so the server still runs with
zero metrics infrastructure.

## Error model

[`errors.py`](../engine/mcp/errors.py) distinguishes two failure
classes:

1. **JSON-RPC protocol errors** — fail the *whole* request with a
   code: `AuthenticationError` (-32001), `AuthorizationError`
   (-32002), `RateLimitError` (-32003).
2. **Tool-content errors** — return a *successful* JSON-RPC response
   whose `result` has `isError=true` and the message in `content`.
   This is the spec-recommended way to surface per-tool failures
   (`ValidationError`, `NotFoundError`, `EngineError`) so the
   assistant can read the message and self-correct.

`map_engine_exception` converts any unexpected engine exception into
an opaque `EngineError`, so internal tracebacks (which may name
internal classes or SQL) never reach the model.

## See also

- [`architecture/plugins.md`](architecture/plugins.md) — how
  strategies are discovered (the registry `list_strategies` /
  `get_strategy_details` read from).
- [`architecture/overview.md`](architecture/overview.md) — where the
  MCP server sits relative to the REST API and workers.
- [`known-limitations.md`](known-limitations.md) — the `server.py`
  transport gap (the only remaining piece; the env-var inventory is
  already in `.env.example`).
