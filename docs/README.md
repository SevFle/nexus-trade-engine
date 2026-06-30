# Nexus Trade Engine — Documentation

<!--
Doc-stack choice
================
This repo ships documentation as plain Markdown under /docs. We deliberately
did **not** adopt MkDocs Material (the conventional pick for Python projects)
for three reasons:

1. Every existing doc (`architecture/`, `adr/`, `operations/`, the runbooks)
   is already Markdown that renders well on GitHub directly. Introducing a
   build step now would invalidate dozens of in-repo links and force a
   `mkdocs serve` workflow on contributors who currently read docs on
   github.com.
2. The audience is senior engineers working on the codebase, not external
   users browsing a marketing site. GitHub's Markdown rendering plus
   inline Mermaid covers every diagram we need.
3. The repo already pulls in zero documentation-related toolchain —
   `pyproject.toml` is for the engine and SDK only. Adding MkDocs would
   cross-contaminate concerns and bloat CI.

We will revisit MkDocs Material when there is a published product site with
non-engineer readers. Until then, "plain Markdown in /docs" is the
intentional choice. ADR-style entries live in `adr/`; runbooks live in
`operations/runbooks/`; everything else is flat under `docs/`.
-->

This directory is the engineering source-of-truth for Nexus Trade Engine.
It is written for engineers who will read the source alongside the prose —
we explain *why*, not *what*.

## Reading order

| If you want to… | Read this |
|------------------|-----------|
| Get a 10-minute mental model of the system | [`architecture/overview.md`](architecture/overview.md) |
| Understand the live-trading stack (brokers / OMS / loop) | [`architecture/brokers-and-live-trading.md`](architecture/brokers-and-live-trading.md) |
| Understand every table and its constraints | [`data-model.md`](data-model.md) |
| Call the REST / WebSocket API | [`api-reference.md`](api-reference.md) |
| Drive the engine from an LLM agent (MCP) | [`mcp-server.md`](mcp-server.md) |
| Run the engine locally | [`development.md`](development.md) |
| Ship a release | [`deployment.md`](deployment.md) · [`RELEASING.md`](RELEASING.md) |
| Write a strategy plugin | [`PLUGIN_DEV_GUIDE.md`](PLUGIN_DEV_GUIDE.md) · [`architecture/plugins.md`](architecture/plugins.md) |
| Understand non-obvious design choices | [`adr/`](adr/README.md) |
| Operate the running system | [`operations/slos.md`](operations/slos.md) · [`operations/runbooks/`](operations/runbooks/README.md) |
| Know what's broken or half-built | [`known-limitations.md`](known-limitations.md) |
| Debug a production incident | [`operations/runbooks/common-issues.md`](operations/runbooks/common-issues.md) |

## Layout

```
docs/
├── README.md                       ← you are here (index + doc-stack rationale)
├── architecture/                   ← component-by-component "current state"
│   ├── overview.md
│   ├── brokers-and-live-trading.md  ← broker adapters, OMS, live loop, kill-switch
│   ├── database.md                 ← migration policy, table inventory
│   └── plugins.md                  ← plugin SDK + registry
├── adr/                            ← architecture decision records (why we chose X)
│   ├── 0001-scaffold-tech-choices.md
│   ├── 0002-auth-rbac.md
│   ├── 0003-mobile-app-strategy.md
│   ├── 0004-task-queue-taskiq.md
│   ├── 0005-valkey-over-redis.md
│   ├── 0006-bcrypt-fernet.md
│   ├── 0007-strategy-sandbox-allowlist-imports.md
│   ├── 0008-pluggable-metrics-backend.md
│   └── 0009-cross-replica-eventbus-bridge.md
├── api-reference.md                ← every HTTP/WS route, auth, schemas
├── mcp-server.md                   ← MCP tools/resources/auth (LLM agent surface)
├── data-model.md                   ← entities, relationships, invariants
├── deployment.md                   ← infra requirements, env, rollout
├── development.md                  ← local setup, test suite, lint loop
├── known-limitations.md            ← tech debt, ranked
├── RELEASING.md                    ← release engineering
├── PLUGIN_DEV_GUIDE.md             ← writing strategies
├── contributors.md                 ← contributor onboarding
├── observability/
│   └── logging.md                  ← structlog schema, redaction rules
├── operations/                     ← how we run it in prod
│   ├── slos.md
│   ├── backup-and-recovery.md
│   ├── dr-drill-checklist.md
│   ├── load-testing.md
│   └── runbooks/                   ← per-SLO + common debug runbooks
├── legal/
│   └── processors.md               ← GDPR data-processor inventory
└── LAST_AUDIT.md                   ← most recent code/security audit summary
```

## Conventions

- **Markdown is the source of truth.** Code-generated OpenAPI specs at
  `/docs` (served by FastAPI) supplement but do not replace these docs.
- **Diagrams**: prefer Mermaid in fenced code blocks; the two `.jsx`
  files under `architecture/` exist for the interactive React preview
  but their content is duplicated in the markdown so non-React readers
  aren't blocked.
- **Linking**: relative paths only (`../architecture/overview.md`), so
  links work both on GitHub and in any local Markdown viewer.
- **Per-file length cap**: 500 lines. Split a file rather than letting
  it accrete. The only exception today is `api-reference.md`, which is
  split by domain.
- **No marketing copy.** The README at the repo root is the public
  face; everything under `docs/` is engineering-grade.

## Updating docs

Code-change PRs that touch any of the following must update docs in the
same PR (enforced in CODEOWNERS, not yet in CI):

| Code change | Required doc update |
|---|---|
| New / changed HTTP route | `api-reference.md` |
| New / changed MCP tool or resource | `mcp-server.md` |
| New / changed DB model or migration | `data-model.md` + `architecture/database.md` |
| New env var | `deployment.md` + `architecture/overview.md` "Configuration" |
| New SLO or alert | `operations/slos.md` + matching runbook under `operations/runbooks/` |
| Major architectural decision | new ADR under `adr/` from `adr/template.md` |

Stale docs are a bug. If you find one, open an issue tagged `docs` or
fix it inline — both are accepted.
