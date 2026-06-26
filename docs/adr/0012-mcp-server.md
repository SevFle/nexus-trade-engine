---
number: 0012
status: Accepted
date: 2026-06-25
---

# ADR-0012: Expose engine capabilities via an MCP server

## Context

Strategy development in Nexus is conversational: a quant wants to ask
"how did the momentum strategy do on AAPL last year, after costs?" and
iterate. Today that round-trip is REST-shaped — the human translates the
question into `GET /strategies`, `POST /backtest/run`, poll
`GET /backtest/results/{id}`, `GET /market-data/...`, and mentally stitches
the cost model in. LLM assistants (Claude, Cursor, etc.) are good at that
translation, but only if the engine speaks a protocol designed for tools
rather than HTTP verbs.

The [Model Context Protocol](https://modelcontextprotocol.io/) (MCP) is
exactly that: a JSON-RPC protocol where a server advertises typed **tools**
and **resources** and an assistant decides when to call them. gh#959 landed
the question of whether Nexus should expose one.

## Decision

**Ship an MCP server (`engine/mcp/`) that exposes a curated read-mostly
subset of the engine as tools + resources.**

The design rules we committed to:

1. **Reuse engine identity, don't fork it.** The MCP server validates JWTs
   with the *same* `decode_token` and evaluates roles against the *same*
   `ROLE_HIERARCHY` as the REST API. A principal is identical across
   surfaces — no second auth model to drift.
2. **stdio-first transport, http optional.** The overwhelmingly common
   deployment is a local stdio server the assistant spawns. HTTP
   (`streamable-http`) is configured but secondary. Tokens travel via
   `_meta` on the request or `NEXUS_MCP_TOKEN` on the process — there are
   no HTTP headers on stdio.
3. **Tools are read-mostly and RBAC-gated.** Every tool defaults to the
   `viewer` role; the only compute-intensive one (`run_backtest`) requires
   `quant_dev`. No tool places live orders — backtests are compute-only.
   Live trading stays on REST behind its own kill-switch surface.
4. **Defensive result shaping.** List tools are cursor-paginated and every
   response passes through a `ResultGuard` token-budget cap, because an
   assistant's context window is the real failure mode — not CPU.
5. **Config is a separate settings object.** `MCPServerSettings`
   (`NEXUS_MCP_*`) is independent of `engine.config.settings` so the server
   can run as a standalone process without booting the API/DB surface.
6. **The adapter seam is the whole dependency.** `EngineServices` is the
   single injected container; adapters are pure
   `(services, principal, args) -> dict` functions. This is what makes the
   dispatch layer testable today even though the transport is not.

## Considered alternatives

- **LLM-friendly REST only (OpenAPI + an assistant plugin).** Rejected: the
  cost-model / portfolio / backtest calls have non-obvious ordering and
  pagination, and an MCP tool description is a better prompt than a path
  template. We keep REST for machines and MCP for assistants.
- **A second, MCP-specific auth model.** Rejected: it would have drifted
  from REST within a release. Reusing JWT + RBAC cost us a few lines of
  `_meta` plumbing and bought a single identity story.
- **Expose live trading over MCP.** Rejected as out of scope for v1. Live
  trading requires reconciliation, kill-switch, and audit guarantees that
  belong to the REST/worker surface (umbrella #109/#111). MCP stays
  read-mostly until those are production-grade.
- **Embed the server in the FastAPI app.** Rejected: stdio MCP must run as
  its own process owned by the assistant. A separate settings object and
  `EngineServices` seam keep the two concerns cleanly separable.

## Consequences

**Positive**

- Assistants get a typed, self-describing surface they can call without a
  human translating intent into HTTP.
- The adapter/dispatch layer is transport-agnostic, so it is unit-testable
  without sockets and can be driven from a future CLI or notebook.
- RBAC reuse means role changes propagate to MCP for free.

**Negative**

- A second request surface to document, version, and SLO. We accept this
  because the audience (assistants) is distinct from the REST audience
  (dashboard, SDK).
- The cost-model and portfolio adapters currently read an **in-memory**
  `PortfolioStore`, not the SQLAlchemy `portfolios` table, so MCP portfolio
  state diverges from REST portfolio state until that is unified.

## Status today

The tool/auth/resource/dispatch/pagination/guard layer is complete and
importable. The **runnable transport bootstrap** (`server.py`) is **not**
shipped — see [`architecture/mcp.md`](../architecture/mcp.md#known-limitation-no-runnable-server)
and [`known-limitations.md`](../known-limitations.md). This ADR records the
*design contract*; the bootstrap is the remaining implementation work
tracked under the umbrella.
