# Nexus Trade Engine — Documentation

<!--
  Documentation stack choice
  --------------------------
  This repository ships documentation as plain Markdown files under /docs.

  Rationale: the engine is Python (FastAPI + SQLAlchemy + TaskIQ) with a
  React frontend, but no portion of the docs benefits from a build
  pipeline. There is no cross-package TypeDoc/Typedoc API surface to
  generate, no React component library to showcase, and no shared design
  tokens between the backend and the frontend. Adding VitePress, Nextra,
  MkDocs-Material, or Docusaurus would pull in a Node/Python build
  dependency, a non-trivial config file, a sidebar manifest to maintain,
  and a CI step — none of which pays for itself given the audience
  (engineers working in this repo) and the shape of the content (long
  technical prose, Mermaid diagrams, ADRs).

  Plain Markdown renders correctly on GitHub, in IDEs, and in any
  static-site generator we may adopt later. The /docs tree is organised
  so it can be copy-pasted into MkDocs or Docusaurus with only a config
  file addition if we ever want a hosted, themed site.

  Conventions:
  - All files < 500 lines. Split when growing past that.
  - Cite source as `path/to/file.py:LINE` so IDEs jump.
  - One ADR per major decision in /docs/adr/; the cross-cutting summary
    lives in /docs/architecture/decisions.md.
  - Mermaid diagrams are inline (GitHub renders them).
-->

This directory holds the engineering documentation for Nexus Trade
Engine — the "why" behind the code, the boundaries between components,
and the operational expectations. The top-level [`README.md`](../README.md)
is the elevator pitch; everything past that lives here.

## Where to start

| You want to… | Read this |
|---|---|
| Understand the moving parts | [`architecture/overview.md`](architecture/overview.md) |
| See the request lifecycle and event flow | [`architecture/overview.md`](architecture/overview.md#request-lifecycle-http) |
| Know which DB tables exist and why | [`architecture/database.md`](architecture/database.md) and [`architecture/data-model.md`](architecture/data-model.md) |
| Understand the plugin / sandbox model | [`architecture/plugins.md`](architecture/plugins.md) and [`PLUGIN_DEV_GUIDE.md`](PLUGIN_DEV_GUIDE.md) |
| Browse every API endpoint | [`api/reference.md`](api/reference.md) |
| Read the rationale for a major choice | [`architecture/decisions.md`](architecture/decisions.md) (one-pager) or [`adr/`](adr/) (full ADRs) |
| Get a dev environment running | [`development.md`](development.md) |
| Deploy / operate the service | [`operations/deployment.md`](operations/deployment.md), [`operations/slos.md`](operations/slos.md), [`operations/runbooks/`](operations/runbooks/) |
| Cut a release | [`RELEASING.md`](RELEASING.md) |
| Know what's broken or half-built | [`operations/known-issues.md`](operations/known-issues.md) |

## Layout

```
docs/
├── README.md                    ← you are here
├── development.md               ← local setup, tests, migrations, lint
├── PLUGIN_DEV_GUIDE.md          ← strategy-plugin author guide
├── RELEASING.md                 ← release process (release-please)
├── LAST_AUDIT.md                ← most recent roadmap / audit pass
├── contributors.md              ← contributor list
├── architecture/
│   ├── overview.md              ← system diagram, request lifecycle, event flow
│   ├── database.md              ← migration policy + TimescaleDB usage
│   ├── data-model.md            ← entities, relationships, constraints
│   ├── plugins.md               ← plugin SDK + sandbox + registry
│   └── decisions.md             ← one-page ADR summary
├── adr/
│   ├── 0001-scaffold-tech-choices.md
│   ├── 0002-auth-rbac.md
│   ├── 0003-mobile-app-strategy.md
│   └── template.md
├── api/
│   └── reference.md             ← every endpoint, auth, schemas
├── operations/
│   ├── deployment.md            ← infra, env vars, rollout
│   ├── slos.md                  ← SLOs, error budgets, alert routing
│   ├── backup-and-recovery.md
│   ├── dr-drill-checklist.md
│   ├── load-testing.md
│   ├── known-issues.md          ← tech debt + known limitations
│   └── runbooks/
│       ├── README.md            ← runbook index
│       ├── api-availability.md
│       ├── api-latency.md
│       ├── auth-mfa.md
│       ├── backtest-submit.md
│       ├── task-pipeline.md
│       └── webhook-delivery.md
├── observability/
│   └── logging.md
└── legal/
    └── processors.md
```

## Conventions

- **Audience.** Write for a competent engineer who is new to this
  codebase, not new to the industry. Skip "what is a database" but do
  explain "why TimescaleDB and not InfluxDB".
- **Citations.** Use `path/to/file.py:LINE` so the reader can jump in
  an IDE. Quote the relevant five lines, not fifty.
- **Length.** No file over 500 lines. Split when growing past that.
- **Style.** Plain Markdown. Mermaid diagrams inline. Code blocks get
  language hints. Avoid emojis.
- **ADRs.** One decision per file in `adr/NNNN-slug.md`. The summary
  table in `architecture/decisions.md` is the index.
- **Runbooks.** One SLO per runbook; the runbook is the contract that
  the alert's `runbook` annotation points at. See
  [`operations/runbooks/README.md`](operations/runbooks/README.md).

## Updating

Most engineering docs go stale within six months unless ownership is
explicit. The convention here:

- The PR that ships a new endpoint updates `api/reference.md` in the
  same change.
- The PR that ships a new SLO ships a runbook in the same change.
- The PR that ships a migration updates `architecture/data-model.md`
  in the same change.
- Quarterly, the [`LAST_AUDIT.md`](LAST_AUDIT.md) pass re-checks
  accuracy and refreshes anything that drifted.
