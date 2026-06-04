# Nexus Trade Engine — Engineering Documentation

<!--
  Documentation stack: plain Markdown under /docs (no static-site generator).

  Why plain Markdown and not MkDocs Material (the usual default for a Python
  project):

  1. The repository already ships an established tree of plain-Markdown docs
     (architecture/, operations/runbooks/, adr/, legal/, observability/). They
     are linked from code comments, alert annotations, runbooks, and ADR
     cross-references. Introducing a generator would either invalidate those
     links or require a `docs_dir` migration that touches every doc at once.
  2. The primary readers are senior engineers on the team who already
     navigate the repo in their editor / on GitHub. Markdown renders
     natively in both — no build step, no broken `mkdocs serve` in CI.
  3. Every alert runbook is referenced verbatim from Prometheus annotations
     (`runbook` URLs point to `docs/operations/runbooks/<name>.md` in this
     tree). A generator would force us to republish those URLs to a hosted
     site and re-point the alerts.
  4. The repo already has a Vite frontend; if we ever need a polished
     public docs site we would add it as a separate job (VitePress under
     `frontend/`), not retrofit MkDocs in-place.

  The cost is no auto-search and no nav sidebar. That is acceptable for an
  internal-codebase doc set of <40 files. If we cross ~100 files we should
  revisit and pick a generator at that point.
-->

This directory is the engineering source of truth for Nexus Trade Engine.
It is written for engineers who will read code anyway — every doc tries to
explain *why* a decision was made, not just *what* it produced.

## Where to start

| If you want to… | Read this |
|------------------|-----------|
| Understand the moving parts | [architecture/overview.md](architecture/overview.md) |
| Understand the data model | [architecture/database.md](architecture/database.md) |
| Make a major technical decision | [adr/](adr/) — and add a new ADR |
| Call the API | [api/README.md](api/README.md) |
| Set up a dev environment | [development.md](development.md) |
| Deploy the service | [deployment.md](deployment.md) |
| Operate the service / handle an alert | [operations/runbooks/](operations/runbooks/) |
| Write a strategy plugin | [PLUGIN_DEV_GUIDE.md](PLUGIN_DEV_GUIDE.md) |
| Cut a release | [RELEASING.md](RELEASING.md) |
| See what we know is broken | [limitations.md](limitations.md) |

## Section index

```text
docs/
├── README.md                      ← you are here
├── development.md                 ← dev environment, test suite, lint/typecheck
├── deployment.md                  ← infra requirements, env vars, rollout
├── limitations.md                 ← known gaps & tech debt, prioritised
├── PLUGIN_DEV_GUIDE.md            ← SDK quick-start for strategy authors
├── RELEASING.md                   ← release-please flow
├── contributors.md                ← contributor expectations
├── LAST_AUDIT.md                  ← kaizen do_engineering_docs heartbeat
├── api/                           ← HTTP / WebSocket API reference
│   ├── README.md
│   ├── auth.md
│   ├── trading.md
│   ├── data.md
│   ├── observability.md
│   ├── webhooks.md
│   ├── privacy-legal.md
│   └── system.md
├── architecture/
│   ├── README.md
│   ├── overview.md                ← components, request lifecycle, event flow
│   ├── database.md                ← schema, migrations, async access patterns
│   ├── plugins.md                 ← plugin kinds, discovery, lifecycle
│   ├── plugin-sdk-architecture.jsx
│   └── trading-framework-architecture.jsx
├── adr/                           ← Architecture Decision Records
│   ├── README.md
│   ├── template.md
│   ├── 0001-scaffold-tech-choices.md
│   ├── 0002-auth-rbac.md
│   └── 0003-mobile-app-strategy.md
├── legal/
│   └── processors.md              ← data-controller / subprocessor inventory
├── observability/
│   └── logging.md                 ← structlog setup, redaction, sampling
└── operations/
    ├── slos.md                    ← SLOs + error budgets
    ├── backup-and-recovery.md
    ├── dr-drill-checklist.md
    ├── load-testing.md
    └── runbooks/                  ← one runbook per SLO
        ├── README.md
        ├── api-availability.md
        ├── api-latency.md
        ├── auth-mfa.md
        ├── backtest-submit.md
        ├── task-pipeline.md
        └── webhook-delivery.md
```

## Conventions

- **Lines are kept under ~500 per file.** If a doc grows past that, split
  it (see `docs/api/` for the pattern: one index, one file per domain).
- **Cross-references are relative paths**, not GitHub URLs. This keeps
  links valid in forks, in your editor, and on a future static-site
  generator. Resolve them against the repo root.
- **Code references use `file_path:line_number`** so the reader can jump
  in their editor.
- **Mention commit hashes or PR numbers** when a decision was triggered
  by a specific incident (SEV-xxx, gh#xxx). Future engineers will grep
  for them.
- **Update `LAST_AUDIT.md`** when you materially change a doc. Kaizen
  uses the mtime as a heartbeat; don't break the heartbeat.

## When to add vs. update

- **New component / module** → update [architecture/overview.md](architecture/overview.md).
- **New HTTP / WebSocket endpoint** → update the matching file in [api/](api/).
- **New table / column** → update [architecture/database.md](architecture/database.md),
  add an Alembic revision, and add a row to the chain table there.
- **New SLO or alert** → update [operations/slos.md](operations/slos.md),
  ship the matching Prometheus rule and runbook in the same PR.
- **New external dependency (SaaS, vendor API)** → update
  [legal/processors.md](legal/processors.md).
- **Major technical decision** → write an ADR (copy
  [adr/template.md](adr/template.md), pick the next free number).
