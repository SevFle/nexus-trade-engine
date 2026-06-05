# Nexus Trade Engine — Documentation

<!--
  Doc stack choice
  ----------------
  This is a Python service (FastAPI + SQLAlchemy + TaskIQ + Alembic).
  There is no JavaScript build step on the engine side; the React
  frontend lives in `frontend/` and is documented separately.

  Options considered:
    - MkDocs Material — strong fit (Python ecosystem, search, version
      selector, mermaid support out of the box), but adds a build
      pipeline and a non-trivial amount of theme/mkdocs.yml maintenance
      for a project at this maturity.
    - VitePress / Nextra — wrong language ecosystem; the engine is not
      Node-based.
    - Plain Markdown rendered by GitHub / IDE preview — zero build,
      zero runtime dependency, works in any text editor, renders
      correctly on GitHub and in any markdown viewer, and survives
      repository forks without configuration.

  Decision: plain Markdown. We will revisit MkDocs Material the moment
  a non-engineering audience needs this material (e.g. external plugin
  authors). Until then the cost of a doc-build pipeline outweighs the
  benefits.
-->

This directory is the engineering documentation set for Nexus Trade
Engine. It is written for engineers who need to understand **why** the
system looks the way it does, not just **what** is in it.

## Read order

If you are new to the codebase, read in this order:

1. [`architecture/overview.md`](architecture/overview.md) — system shape,
   request lifecycle, the request-flow diagram.
2. [`architecture/decisions.md`](architecture/decisions.md) — the
   architecture decision records (ADRs) that locked in the choices you
   see in code. Read these before proposing a different stack.
3. [`api/reference.md`](api/reference.md) — every HTTP route, its
   payload, its auth requirement.
4. [`api/data-model.md`](api/data-model.md) — entity-relationship view
   of the Postgres schema.
5. [`development/setup.md`](development/setup.md) — commands a new
   contributor runs on day one.
6. [`deployment/overview.md`](deployment/overview.md) — what production
   looks like and how code gets there.
7. [`operations/runbooks.md`](operations/runbooks.md) — diagnosis
   playbooks for the failures you will actually see.

## File map

```
docs/
├── README.md                        ← you are here
├── architecture/
│   ├── overview.md                  System shape and request flow.
│   └── decisions.md                 ADR-style record of major choices.
├── api/
│   ├── reference.md                 HTTP + WebSocket routes, by tag.
│   └── data-model.md                SQLAlchemy entities and relations.
├── development/
│   └── setup.md                     Local dev, tests, lint, typecheck.
├── deployment/
│   └── overview.md                  Infra, env vars, rollout process.
├── operations/
│   ├── runbooks.md                  Diagnosis playbooks (top issues).
│   ├── slos.md                      Service level objectives.
│   ├── backup-and-recovery.md       RPO/RTO and restore drills.
│   ├── dr-drill-checklist.md        DR exercise steps.
│   └── load-testing.md              Load profile and harness.
├── observability/
│   └── logging.md                   Structured log schema.
├── legal/
│   └── processors.md                Data processor inventory.
├── adr/                             Original individual ADR files.
│   ├── 0001-scaffold-tech-choices.md
│   ├── 0002-auth-rbac.md
│   └── 0003-mobile-app-strategy.md
├── tech-debt.md                     Honest, prioritized debt list.
├── PLUGIN_DEV_GUIDE.md              Strategy plugin author guide.
├── RELEASING.md                     Release / versioning procedure.
├── contributors.md                  Contributor checklist.
├── development.md                   Legacy dev doc (kept for links).
└── LAST_AUDIT.md                    Last documentation audit timestamp.
```

## Conventions

- **File cap.** Every doc file stays under 500 lines. Split when it
  grows past that.
- **Link style.** Use relative paths from the file you're in
  (`../api/reference.md`), not absolute repo paths.
- **Code references.** When naming a function, include the file and
  line — e.g. `create_app()` at `engine/app.py:154` — so the reader
  can jump straight into the editor.
- **Mermaid.** Mermaid diagrams are supported by GitHub native
  rendering; no extra tooling required.
- **Audience.** The reader is a competent engineer who needs to ship a
  change. Optimise for the second reading (debugging) over the first
  reading (marketing).

## Maintenance

This directory is updated by the engineering docs automation on every
meaningful code change. The mtime of
[`LAST_AUDIT.md`](LAST_AUDIT.md) is the most recent reconciliation
point — if it is older than the change you are reading about, the docs
are stale and you should treat the source as authoritative.
