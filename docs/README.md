# Nexus Trade Engine — Engineering Documentation

<!--
  Doc-stack choice: MkDocs (Material theme).

  Rationale: this is a Python-only project (FastAPI engine + a
  stand-alone installable SDK under `sdk/`). MkDocs Material is the
  de-facto Python-docs toolchain — it renders plain Markdown, has a
  first-class Mermaid plugin (we use Mermaid for architecture and
  ER diagrams), integrates with `mkdocstrings` for Python API
  auto-docs, and builds static HTML that we can host on GitHub
  Pages without a Node toolchain.

  Alternatives considered:
    - VitePress / Nextra  — TypeScript-only; would force a Node
      build on a Python repo and add a second lockfile.
    - Sphinx              — heavyweight; RST learning curve for
      new contributors; we already write Markdown.
    - Plain /docs Markdown only — fine as a fallback, but loses
      search, navigation, diagram rendering, and CI build checks.

  Config lives at `mkdocs.yml` at the repo root; the requirements
  pin is in `docs/requirements.txt`. Build locally with:
      pip install -r docs/requirements.txt
      mkdocs serve
-->

This directory is the source of truth for engineers working on the
engine, SDK, and operational tooling. It is intended for a competent
reader who wants the **why** behind the **what** — not a beginner
tutorial.

## Quick navigation

| If you want to…                              | Read                                                                |
|----------------------------------------------|---------------------------------------------------------------------|
| Understand the moving parts                  | [Architecture overview](architecture/overview.md)                   |
| See the data model and relationships         | [Database & data model](architecture/database.md)                   |
| Read every HTTP endpoint                     | [API reference](api-reference/index.md)                             |
| Know why major decisions were made           | [ADR index](adr/README.md)                                          |
| Stand the project up locally                 | [Development setup](development.md)                                 |
| Ship a change to production                  | [Deployment](operations/deployment.md)                              |
| Page someone at 03:00                        | [Runbooks](operations/runbooks/README.md)                           |
| See the honest backlog of warts              | [Known limitations & technical debt](operations/known-limitations.md) |
| Build a strategy plugin                      | [Plugin developer guide](PLUGIN_DEV_GUIDE.md)                       |
| Cut a release                                | [Releasing](RELEASING.md)                                           |

## Layout

```
docs/
├── README.md                  ← you are here
├── development.md             ← local setup, test commands, lint
├── PLUGIN_DEV_GUIDE.md        ← external-facing SDK guide
├── RELEASING.md               ← release runbook
├── architecture/
│   ├── overview.md            ← system components + Mermaid diagram
│   ├── database.md            ← schema, migrations, ER shape
│   └── plugins.md             ← plugin runtime & sandbox
├── adr/
│   ├── README.md              ← how to write an ADR + index
│   ├── 0001-scaffold-tech-choices.md
│   ├── 0002-auth-rbac.md
│   ├── 0003-mobile-app-strategy.md
│   └── template.md
├── api-reference/
│   ├── index.md               ← auth model, conventions, errors
│   ├── auth.md                ← /auth, /auth/mfa, /auth/api-keys
│   ├── portfolios.md
│   ├── backtest.md
│   ├── market-data.md
│   ├── strategies.md
│   ├── marketplace.md
│   ├── tax.md
│   ├── scoring.md
│   ├── webhooks.md
│   ├── websocket.md
│   ├── privacy.md
│   ├── legal.md
│   ├── reference.md
│   └── system.md
├── operations/
│   ├── deployment.md          ← infra, env vars, rollout
│   ├── backup-and-recovery.md
│   ├── dr-drill-checklist.md
│   ├── slos.md
│   ├── load-testing.md
│   ├── known-limitations.md
│   └── runbooks/
│       ├── README.md
│       ├── api-availability.md
│       ├── api-latency.md
│       ├── auth-mfa.md
│       ├── backtest-submit.md
│       ├── task-pipeline.md
│       ├── webhook-delivery.md
│       └── database.md
├── observability/
│   └── logging.md
├── legal/
│   └── processors.md
├── contributors.md
├── LAST_AUDIT.md              ← touched on every doc-audit cycle
└── requirements.txt           ← pins for mkdocs + plugins
```

## Editorial conventions

- **Voice:** senior engineer to senior engineer. Skip "let's", skip
  hand-holding. State the constraint, the trade-off, and the chosen
  path.
- **Code paths:** link to source with `engine/path/to/file.py:NN`
  so the reader can jump straight into the editor.
- **No copy-pasted source.** Show the contract (function signature,
  schema, request body); link to the implementation for the body.
- **ADRs are append-only.** Update status (`Superseded by …`) but
  never rewrite history.
- **Runbooks are written for the on-call at 03:00.** First 60
  seconds first; forensics after.

## When to update these docs

- Any PR that adds an env var → update `architecture/overview.md`
  + `operations/deployment.md` in the same PR.
- Any PR that adds a route → update `api-reference/`.
- Any PR that adds an SLO metric → update `operations/slos.md`
  and the matching runbook.
- Any PR that ships a non-trivial architectural change → write
  an ADR **before** the code lands.
