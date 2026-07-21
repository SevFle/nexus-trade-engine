<!--
Doc-stack choice: MkDocs + Material
===================================
This is a Python project (FastAPI + SQLAlchemy + TaskIQ + Polars), so the
documentation is built with **MkDocs** and the **Material for MkDocs**
theme. We picked it over the alternatives for concrete reasons:

* VitePress / Nextra are JS/TS toolchains. Wiring a Node build pipeline
  into a Python-only repo would cross-contaminate concerns and bloat CI.
* Plain Markdown in /docs (the previous approach) renders on GitHub but
  gives no full-text search, no collapsible/versioned navigation, and no
  reliable Mermaid rendering. For a codebase this size (19 DB tables,
  13 ADRs, a 337-line API reference, 7 runbooks) those matter.
* MkDocs Material consumes the existing Markdown verbatim, so the files
  stay first-class on GitHub AND get a navigable, searchable, themed
  site. Config lives in /mkdocs.yml; deps in the `docs` extra of
  pyproject.toml. No content was forked.

Trade-off we accepted: the docs link to source files with repo-root-
relative paths (../engine/app.py). Those resolve on GitHub (the primary
reading surface) but point *outside* docs_dir for MkDocs, so
mkdocs.yml sets validation.links.not_found = warn — the build stays green
and the GitHub links keep working. See the "Building & previewing"
section below for why `--strict` is not yet used in CI.

ADRs live in adr/, runbooks in operations/runbooks/, everything else is
flat under docs/. Keep each file under 500 lines; split rather than grow.
-->

# Nexus Trade Engine — Documentation

## Building & previewing the docs locally

Docs are built with MkDocs Material (see the rationale in the comment at
the top of this file and in [`mkdocs.yml`](../mkdocs.yml)).

```bash
uv sync --extra docs                       # install mkdocs + material only
uv run mkdocs serve                        # http://127.0.0.1:8000 — live reload
uv run mkdocs build                        # static site into ./site/
```

`mkdocs serve` rebuilds on every save. Mermaid diagrams render
automatically (Material 9.x bundles the renderer); no extra plugin is
loaded.

> Do **not** run `mkdocs build --strict` in CI yet. The intentional
> source-relative links (`../engine/...`) emit `not_found` warnings by
design (see the comment above). The default `warn` level keeps the build
green; `--strict` would upgrade those to errors. A future cleanup that
moves source links behind a generated API section can flip `--strict` on.

When you add a page, register it in the `nav:` block of
[`mkdocs.yml`](../mkdocs.yml) in the same PR so it shows up in the sidebar
(unregistered pages still build and are searchable, but emit an
`omitted_files` warning).

This directory is the engineering source-of-truth for Nexus Trade Engine.
It is written for engineers who will read the source alongside the prose —
we explain *why*, not *what*.

## Reading order

| If you want to… | Read this |
|------------------|-----------|
| Get a 10-minute mental model of the system | [`architecture/overview.md`](architecture/overview.md) |
| Understand the decision/execution domain layer (instruments, orchestration, cost & risk, execution, accounting) | [`architecture/core-domains.md`](architecture/core-domains.md) |
| Understand the 86-KPI analytics + scoring + optimization internals | [`architecture/analytics.md`](architecture/analytics.md) |
| Understand how market data is routed, failed over, and validated | [`architecture/data-providers.md`](architecture/data-providers.md) |
| Understand every table and its constraints | [`data-model.md`](data-model.md) |
| Pick the right multi-strategy coordinator (voters vs. capital-aware) | [`architecture/multi-strategy.md`](architecture/multi-strategy.md) |
| Call the REST / WebSocket API | [`api-reference.md`](api-reference.md) (conventions) · [`api-reference/routes.md`](api-reference/routes.md) (per-endpoint catalog) · [`api-reference/websocket.md`](api-reference/websocket.md) (WS wire protocol) |
| Drive the engine from an LLM agent (MCP) | [`mcp-server.md`](mcp-server.md) · [`mcp/capability-audit.md`](mcp/capability-audit.md) · [`mcp/tool-catalog.md`](mcp/tool-catalog.md) |
| Run the engine locally | [`development.md`](development.md) |
| Ship a release | [`deployment.md`](deployment.md) · [`RELEASING.md`](RELEASING.md) |
| Write a strategy plugin | [`PLUGIN_DEV_GUIDE.md`](PLUGIN_DEV_GUIDE.md) · [`architecture/plugins.md`](architecture/plugins.md) |
| Understand non-obvious design choices | [`adr/`](adr/README.md) |
| Operate the running system | [`operations/slos.md`](operations/slos.md) · [`operations/runbooks/`](operations/runbooks/README.md) |
| Know what's broken or half-built | [`known-limitations.md`](known-limitations.md) |
| Debug a production incident | [`operations/runbooks/common-issues.md`](operations/runbooks/common-issues.md) |

## Layout

```
repo root
├── mkdocs.yml                    ← MkDocs Material config (theme, nav, extensions)
docs/
├── README.md                       ← you are here (index + doc-stack rationale)
├── architecture/                   ← component-by-component "current state"
│   ├── overview.md                 ← service view (app, lifecycle, deploy)
│   ├── core-domains.md             ← domain view (instruments, orchestration, cost & risk, execution)
│   ├── analytics.md                ← analytics, scoring & optimization (split out of core-domains)
│   ├── multi-strategy.md           ← the five strategy coordinators (voters + capital-aware)
│   ├── data-providers.md           ← market-data provider layer: registry routing, fail-over, symbol validation
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
│   ├── 0009-cross-replica-eventbus-bridge.md
│   ├── 0010-static-ast-validation-toctou-loading.md
│   ├── 0011-runtime-introspection-blocking.md
│   └── 0012-sandbox-resource-limits-single-flight.md
├── api-reference.md                ← conventions: auth, legal gate, errors, middleware
├── api-reference/                  ← per-endpoint catalogs (split out of api-reference.md)
│   ├── routes.md                   ← every HTTP endpoint, grouped by router module
│   └── websocket.md                ← /ws + /ws/events wire protocol, channels, close codes
├── mcp-server.md                   ← MCP tools/resources/auth (LLM agent surface)
├── mcp/                            ← MCP surface audit + catalog (generated map)
│   ├── capability-audit.md         ← whole-surface review (findings, inventory)
│   ├── api-surface-map.yaml        ← GENERATED tool/resource/auth inventory
│   └── tool-catalog.md             ← per-tool reference (roles, args, pagination)
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
  it accrete. [`known-limitations.md`](known-limitations.md) is the one
  exhaustive reference doc that intentionally runs a little over,
  because it grows monotonically as we surface debt and is heavily
  cross-linked by anchor from the README roadmap and the runbooks —
  splitting it would fragment navigation more than it helps. It now
  runs to ~575 lines; the cap is still respected in spirit by splitting
  debt items into discrete anchored sections (`<a id="…"></a>`) rather
  than free-form prose. The HTTP/WS surface *used* to be a single
  ~570-line `api-reference.md`; it has since been split into a
  conventions page ([`api-reference.md`](api-reference.md), ~190 lines)
  plus two per-endpoint catalogs
  ([`api-reference/routes.md`](api-reference/routes.md) and
  [`api-reference/websocket.md`](api-reference/websocket.md)) — exactly
  the pattern this rule prescribes. Everything else is well under the
  cap.
- **No marketing copy.** The README at the repo root is the public
  face; everything under `docs/` is engineering-grade.

## Updating docs

Code-change PRs that touch any of the following must update docs in the
same PR (enforced in CODEOWNERS, not yet in CI):

| Code change | Required doc update |
|---|---|
| New / changed HTTP route | [`api-reference/routes.md`](api-reference/routes.md) (and `api-reference.md` if conventions change) |
| New / changed WebSocket route or message | [`api-reference/websocket.md`](api-reference/websocket.md) |
| New / changed MCP tool or resource | `mcp-server.md` |
| New / changed DB model or migration | `data-model.md` + `architecture/database.md` |
| New env var | `deployment.md` + `architecture/overview.md` "Configuration" |
| New SLO or alert | `operations/slos.md` + matching runbook under `operations/runbooks/` |
| Major architectural decision | new ADR under `adr/` from `adr/template.md` |
| New doc page | add the file **and** a `nav:` entry in `mkdocs.yml` |

Stale docs are a bug. If you find one, open an issue tagged `docs` or
fix it inline — both are accepted.
