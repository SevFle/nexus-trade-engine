# ADR-0010: MCP server — reuse engine auth/RBAC, standalone settings, stdio-first

- **Status**: Accepted (implementation in progress — see
  [`architecture/mcp-server.md`](../architecture/mcp-server.md) "Status & limitations")
- **Date**: 2026-06-23
- **Deciders**: Lead maintainer + platform reviewer
- **Tags**: mcp, auth, observability, ai

## Context and Problem Statement

gh#959 landed an MCP (Model Context Protocol) surface so AI assistants can
drive the engine: run backtests, inspect portfolios, list strategies, pull
market data, estimate costs, and compute metrics. MCP is becoming the
de-facto wire format for "tool use" against a backend, and shipping a
first-class server lets the engine be orchestrated by Claude Desktop,
custom agents, and service-to-service callers without a human at the REST
UI.

The non-obvious decisions were about *boundaries*, not features. An MCP
server needs auth, rate limiting, observability, and result shaping — and
the engine already has opinions on all four for REST and WebSocket. The
question was whether to share those opinions or build MCP-specific ones.
A second question was *how the server runs*: stdio (local assistant host)
or HTTP (mounted in the FastAPI app), and whether it needs the full
DB/API surface to boot.

## Decision Drivers

- **One identity model.** A principal authenticated over MCP should be
  indistinguishable from one authenticated over REST or WebSocket — same
  roles, same JWT, same audit trail. Splitting auth would mean two
  permission systems to keep in sync.
- **Standalone operability.** A local stdio server (the common MCP deploy)
  must boot in milliseconds without a database, a Valkey, or the full
  FastAPI app.
- **LLM context safety.** Unlike a browser, an assistant has a hard token
  budget. A naive `get_market_data` over a year of daily bars, or a full
  order log, can blow out the context window and corrupt the conversation.
  Result shaping is a *first-class* concern, not an afterthought.
- **Hermetic testability.** The adapters must be unit-testable without a
  running broker, DB, or network.
- **No new infra.** The server should reuse the observability backend and
  RBAC hierarchy already in the tree.

## Considered Options

1. **Reuse engine JWT + `ROLE_HIERARCHY`; separate `MCPServerSettings`;
   stdio-first; pure-function adapters with an `EngineServices` DI
   container; mandatory `ResultGuard`.**
2. **A parallel MCP-only auth model** (separate tokens/roles, MCP-local
   permission table).
3. **HTTP-only, mounted inside the FastAPI app**, sharing `engine.config`
   and the request auth dependency directly.
4. **Thin pass-through: no pagination/guard layer** — return raw engine
   objects and let the client/assistant deal with size.

## Decision Outcome

Chosen option: **Option 1**, because it satisfies all five drivers at
once and keeps the MCP server a *peer* of the REST/WS surfaces rather
than a fork.

### How it works

- **Auth** ([`engine/mcp/auth.py`](../../engine/mcp/auth.py)):
  `extract_principal()` resolves a credential in priority order —
  per-request `_meta.authorization`/`_meta.api_key`, then the static
  API-key table (`NEXUS_MCP_STATIC_API_KEYS`), then the process-level
  `NEXUS_MCP_TOKEN`. Valid JWTs are decoded with the **exact** validator
  the REST API uses (`engine.api.auth.jwt.decode_token`); roles are
  checked against the shared `ROLE_HIERARCHY` via `require_role()`. With
  `NEXUS_MCP_AUTH_REQUIRED=false`, an anonymous principal at
  `NEXUS_MCP_DEFAULT_ROLE` is issued for local dev.
- **Settings** ([`engine/mcp/config.py`](../../engine/mcp/config.py)):
  `MCPServerSettings` is a standalone `pydantic-settings` class with the
  `NEXUS_MCP_` prefix — deliberately **not** part of
  `engine.config.Settings`. A stdio server therefore does not read
  `NEXUS_DATABASE_URL`/`NEXUS_VALKEY_URL` and can boot with zero infra.
- **Adapters** ([`engine/mcp/adapters/`](../../engine/mcp/adapters/)):
  every tool is a pure async `(services, principal, arguments) -> dict`.
  `EngineServices` is the single injectable dependency; `for_testing()`
  pins fakes so adapters run hermetically.
- **Result safety** ([`engine/mcp/pagination.py`](../../engine/mcp/pagination.py)):
  `dispatch_tool()` always runs `ResultGuard`, which estimates tokens at
  ~4 chars/token and truncates the longest list to fit
  `NEXUS_MCP_RESULT_TOKEN_BUDGET` (default 24 000), stamping a
  `truncated` flag. List-heavy tools (`get_orders`, `get_market_data`,
  `list_strategies`) are cursor-paginated (base64 offset, max 500/page).
- **Transport**: stdio is primary (`NEXUS_MCP_TRANSPORT=stdio`, the
  default); HTTP (`/mcp` on `127.0.0.1:8765`) is the secondary transport
  for mounted deployments.
- **Observability** ([`engine/mcp/observability.py`](../../engine/mcp/observability.py)):
  `mcp.tool.call` / `mcp.tool.duration_ms` / `mcp.tool.error` are emitted
  through the *same* pluggable `MetricsBackend` (ADR-0008) as REST/WS, so
  one dashboard covers all three surfaces.

### Consequences

- **Positive** — one identity/permission model across REST, WS, and MCP.
  Revoking a role or rotating a JWT key fixes all three at once.
- **Positive** — the stdio server boots with no DB/Valkey; online vs
  hermetic is just which `EngineServices` you construct.
- **Positive** — no assistant can receive a context-busting payload:
  `ResultGuard` is mandatory and runs on every result.
- **Positive** — MCP metrics land in the existing Prometheus pipeline
  with no new exporter.
- **Negative** — the transport-wiring bootstrap (`engine/mcp/server.py`)
  is **not yet in the tree**, so the module is a landed library, not a
  runnable server today (see
  [`architecture/mcp-server.md`](../architecture/mcp-server.md)). The
  `pyproject.toml` ruff ignore for `engine/mcp/server.py` is the
  placeholder.
- **Negative** — `MCPServerSettings` is a second settings class.
  Operators have to know that MCP knobs live under `NEXUS_MCP_*`, not
  `NEXUS_*`, and they are absent from `.env.example` / `deployment.md`.
- **Negative** — `PortfolioStore` is in-memory and *not* the SQLAlchemy
  `portfolios` table, so MCP portfolio reads do not reflect REST-created
  books until the store is wired to the DB.

## Pros and Cons of the Options

### Option 1 — Reuse + standalone settings + stdio-first + guard (chosen)

- **Pros:** Satisfies every driver; one auth model; boots standalone;
  LLM-safe by construction; hermetic-testable.
- **Cons:** Two settings classes to document; server entrypoint still
  pending; portfolio store not yet DB-backed.

### Option 2 — Parallel MCP-only auth

- **Pros:** MCP could ship "simpler" scoped tokens without touching REST.
- **Cons:** Two permission systems to keep in sync; breaks the "one
  identity" goal; duplicates JWT validation and role logic; harder audit
  story.

### Option 3 — HTTP-only, mounted in FastAPI, shared `engine.config`

- **Pros:** One settings class; auth dependency reused verbatim; no
  separate server process.
- **Cons:** Cannot run as a local stdio server (the dominant MCP deploy
  model); forces a full app+DB boot for every assistant session; couples
  MCP availability to the HTTP process.

### Option 4 — No pagination/guard layer

- **Pros:** Less code; raw engine fidelity.
- **Cons:** Any list-heavy tool can exceed an LLM context window and
  corrupt the conversation; the failure mode is silent and catastrophic
  for the assistant UX. Unacceptable for a tool meant to be called by
  models.

## Links

- Original PR: gh#959
- Current-state doc: [`architecture/mcp-server.md`](../architecture/mcp-server.md)
- Source: [`engine/mcp/`](../../engine/mcp/)
- Related ADRs: [0002 — Auth & RBAC](0002-auth-rbac.md) (the role
  hierarchy MCP reuses), [0008 — pluggable MetricsBackend](0008-pluggable-metrics-backend.md)
  (the metrics sink MCP emits through)
- Known gap: [`docs/known-limitations.md`](../known-limitations.md)
  "MCP server is a landed library, not yet a runnable server"
