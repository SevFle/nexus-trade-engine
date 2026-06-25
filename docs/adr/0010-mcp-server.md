# ADR-0010: Expose the engine to AI assistants via an MCP server

- **Status**: Accepted
- **Date**: 2024-06-20
- **Deciders**: lead maintainer + platform reviewer
- **Tags**: mcp, ai, integration, auth

## Context and Problem Statement

The engine's value — backtests, cost modeling, portfolio state, strategy
catalog — is locked behind a REST surface designed for human-driven dashboards
and headless scripts. Engineers and quant analysts increasingly work *through*
LLM coding agents and chat assistants: "backtest mean-reversion on AAPL for
2023 and explain the drawdown." Forcing that workflow to go through hand-rolled
HTTP calls (or an assistant faking a browser) is brittle and gives the model no
typed contract to reason about.

We needed a first-class way for an assistant to call the engine's capabilities
with strong typing, scoped authorization, and safety rails — without
re-implementing auth, RBAC, or the cost model a second time.

## Decision Drivers

- **Typed tool contracts.** Assistants reason better about a declared JSON
  Schema + description than about ad-hoc REST payloads. We wanted every
  capability the model can call to be discoverable and validated.
- **Identity reuse.** A second auth system would drift from the REST one. The
  decision had to share the engine's JWT validation and `ROLE_HIERARCHY` so an
  MCP-authenticated principal is identical to a REST-authenticated one.
- **LLM safety.** Models happily consume (and leak) oversized or sensitive
  output. The protocol surface had to let us cap response size, paginate lists,
  and guarantee engine tracebacks never reach the model.
- **Decoupled lifecycle.** The server must run as a `stdio` child of an IDE
  without standing up the full FastAPI app + Postgres + Valkey, but still
  delegate to real engine components when online.

## Considered Options

1. **No agent surface** — assistants call REST directly.
2. **Custom JSON-RPC over HTTP** — a bespoke "agent API".
3. **OpenAI-style function-calling shim** — generate function schemas from
   OpenAPI and proxy.
4. **MCP (Model Context Protocol)** — adopt the open spec, ship a server with
   typed tools + resources.

## Decision Outcome

Chosen option: **Option 4 — MCP server under `engine/mcp/`**, because it is
an open, transport-agnostic, model-vendor-neutral protocol that gives us
typed `tools` and `resources` for free, matches every driver above, and
lets the server reuse engine code (`decode_token`, `ROLE_HIERARCHY`,
`DefaultCostModel`, `Portfolio`, `BacktestRunner`) without coupling to the
FastAPI app.

The server is a **standalone process** (`stdio` default, `http` optional),
deliberately *not* mounted as a router in `engine/app.py`. It shares *code*
with REST, not *lifecycle*. See [`mcp-server.md`](../mcp-server.md) for the
tool/resource catalog and [`engine/mcp/`](../../engine/mcp/) for source.

### Consequences

- **Positive** — one typed contract the model can discover; auth/RBAC/cost
  model defined once; size/pagination guards centralize LLM safety; adapters
  are pure `(services, principal, args) -> dict` functions, trivially testable
  hermetically via `EngineServices.for_testing()`.
- **Negative** — a second network-adjacent surface to secure, rate-limit, and
  observe (mitigated by reusing `decode_token` + the `MetricsBackend` and by
  annotating every tool `readOnlyHint` and gating the one compute tool behind
  `quant_dev`). The transport layer adds a process to deploy.
- **Neutral** — MCP is young; we pin the `mcp` SDK version and isolate its
  types in `tool_definitions.py`/`resources.py` so a spec change does not
  ripple into adapters.

## Pros and Cons of the Options

### Option 1 — no agent surface

- **Pros:** zero new code.
- **Cons:** assistants fake REST payloads (brittle, no schema, auth leaks into
  prompts); no typed discovery; fails the typed-contract driver.

### Option 2 — custom JSON-RPC

- **Pros:** full control.
- **Cons:** we reinvent tool advertisement, validation, progress, and resource
  semantics; no client ecosystem; maintainers must learn our private protocol.
  Fails the "don't re-implement" driver.

### Option 3 — function-calling shim from OpenAPI

- **Pros:** generated from the REST surface we already have.
- **Cons:** REST endpoints are human/dashboard-shaped (paginated query strings,
  cookie flows, legal-acceptance redirects), not tool-shaped; mappings to one
  vendor's function spec lock us in; no `resources` concept. Fails the
  model-vendor-neutrality and lifecycle drivers.

### Option 4 — MCP

- **Pros:** open spec, typed `tools` + `resources`, `stdio`/`http` transports,
  progress notifications, growing client ecosystem across vendors; lets us
  reuse engine code while running decoupled from FastAPI.
- **Cons:** spec is young (we pin + isolate); a deployable process to own.

## Links

- Reference doc: [`mcp-server.md`](../mcp-server.md)
- Source: [`engine/mcp/`](../../engine/mcp/)
- Auth model shared with REST: [`adr/0002-auth-rbac.md`](0002-auth-rbac.md)
- Metrics reuse: [`adr/0008-pluggable-metrics-backend.md`](0008-pluggable-metrics-backend.md)
- Implementing PRs: #959 (server), #961 (adapter fixes)
