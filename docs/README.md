# Nexus Trade Engine — Documentation

<!--
Documentation stack choice
==========================
This project is a Python 3.11+ monolith (FastAPI + SQLAlchemy async +
TaskIQ + Polars) with a small Vite/React frontend. We use **MkDocs
with the Material theme** for the rendered docs site:

  - Native fit for Python projects — every Markdown file in `/docs`
    stays portable and reviewable in GitHub's renderer; MkDocs just
    adds nav, search, Mermaid rendering, and versioned publishing on
    top.
  - No JS toolchain to maintain (VitePress/Nextra are JS-first and
    would pull in a competing build pipeline alongside the existing
    Vite frontend).
  - Material gives us the Mermaid diagrams, dark mode, and global
    search the architecture overview relies on, without the overhead
    of a full static-site generator like Sphinx or Docusaurus.

The site is built from the same Markdown you are reading now via
`mkdocs.yml` at the repo root (`pip install mkdocs-material &&
mkdocs serve`). Every page is also readable directly on GitHub, so
nothing about the source organisation is MkDocs-specific.

This index is the canonical entry point. Add new pages here and to
`mkdocs.yml: nav` in the same PR.
-->

Welcome. This directory holds the engineering documentation for the
Nexus Trade Engine — an opinionated, plugin-driven algorithmic
trading framework that treats cost, tax, and slippage modelling as
first-class inputs to every strategy.

The docs are written for engineers who will read, change, and operate
this code. They explain **why** a decision was made, not just **what**
the code does today.

## Reading order

If you are new to the codebase, read in this order:

1. **[Architecture overview](architecture/overview.md)** — components,
   request lifecycle, where new code goes.
2. **[Data model](architecture/data-model.md)** — entities, relationships,
   and the constraints the schema enforces.
3. **[API reference](api-reference.md)** — every HTTP and WebSocket
   endpoint with auth and payload shapes.
4. **[Technical decisions](adr/)** — ADRs explaining the non-obvious
   design choices (auth model, cost model, task queue, etc.).
5. **[Development setup](development.md)** — local build, test, lint.
6. **[Deployment](deployment.md)** — infra requirements, env vars,
   rollout.
7. **[Known limitations & tech debt](known-limitations.md)** — what
   is intentionally missing and the priority order to fix it.
8. **[Runbooks](operations/runbooks/)** — operational playbooks for
   the alerts we actually page on.

## Topical index

| Area | Document | Audience |
|---|---|---|
| System shape | [Architecture overview](architecture/overview.md) | all engineers |
| System shape | [Plugin architecture](architecture/plugins.md) | strategy authors |
| System shape | [Database](architecture/database.md) | backend engineers |
| Data | [Data model](architecture/data-model.md) | backend engineers |
| Interface | [API reference](api-reference.md) | API consumers |
| Interface | [WebSocket protocol](api-reference.md#websocket) | realtime consumers |
| Decisions | [ADR index](adr/) | senior engineers |
| Build | [Development setup](development.md) | contributors |
| Build | [Plugin developer guide](PLUGIN_DEV_GUIDE.md) | strategy authors |
| Ship | [Deployment](deployment.md) | platform / SRE |
| Ship | [Release process](RELEASING.md) | maintainers |
| Operate | [Runbooks](operations/runbooks/) | on-call |
| Operate | [SLOs](operations/slos.md) | on-call |
| Operate | [Backup & recovery](operations/backup-and-recovery.md) | on-call |
| Operate | [Load testing](operations/load-testing.md) | perf |
| Contribute | [Contributors guide](contributors.md) | all contributors |
| Compliance | [Legal documents](../legal/) | operators |
| Compliance | [Data providers & attributions](legal/processors.md) | operators |

## Conventions

- Markdown only. No reST, no AsciiDoc — keeps review diffs clean.
- One topic per file. Split when a file crosses ~500 lines.
- Link to source with relative paths (`../../engine/...`) so the
  GitHub renderer resolves them. MkDocs copies them through unchanged.
- When adding a new ADR, copy
  [`adr/template.md`](adr/template.md) and increment the number.
- When adding a new SLO, ship the alert + the runbook in the same PR.
  An alert without a runbook is a pager incident waiting to happen.
