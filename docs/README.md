<!--
Documentation stack choice: plain Markdown rendered directly from /docs.

Rationale: this codebase already shipped with a plain-Markdown /docs tree
that mixes .md files with two interactive .jsx architecture diagrams
(`docs/architecture/*-architecture.jsx`) that MkDocs / VitePress would
not render without a custom plugin. The product surface is a Python
FastAPI engine plus a React dashboard, so MkDocs Material is the
"official" recommendation for Python-first projects — but MkDocs would
require migrating those .jsx diagrams and committing to a build pipeline
for content that today loads directly in GitHub and IDE Markdown
preview. The team agreed (see ADR-0001) to keep docs as plain Markdown
and lean on GitHub's native renderer plus the React-based diagram files
that already exist for interactive exploration. If we ever want search
+ theming we can add MkDocs later without rewriting content; the
files in this tree are already structured so that mkdocs.yml would be a
few dozen lines of `nav:`.

Every file in this tree is hand-written and reflects the actual
codebase. The audit timestamp lives in `LAST_AUDIT.md`.
-->

# Nexus Trade Engine — Documentation

This directory is the canonical source of truth for how Nexus is built,
operated, and extended. The README in the repo root is the elevator
pitch; everything below is the engineering detail behind it.

## Where to start

| You are… | Read these, in order |
|---|---|
| **A new engineer** onboarding | [architecture/overview](architecture/overview.md) → [architecture/database](architecture/database.md) → [architecture/data-model](architecture/data-model.md) → [development](development.md) → [api/](api/) |
| **An operator** running Nexus in production | [deployment](deployment.md) → [operations/slos](operations/slos.md) → [operations/backup-and-recovery](operations/backup-and-recovery.md) → [operations/runbooks/](operations/runbooks/) |
| **A strategy author** | [PLUGIN_DEV_GUIDE](PLUGIN_DEV_GUIDE.md) → [architecture/plugins](architecture/plugins.md) → [api/backtest](api/backtest.md) → [api/scoring](api/scoring.md) |
| **Writing an integration** | [api/](api/) (start at the README) → [api/webhooks](api/webhooks.md) → [api/websocket](api/websocket.md) |
| **Investigating an incident** | [operations/runbooks/](operations/runbooks/) → [observability/logging](observability/logging.md) → [known-limitations](known-limitations.md) |

## Reading order for the whole tree

1. **Architecture** — what the moving pieces are.
   - [`architecture/overview.md`](architecture/overview.md) — system
     map and request lifecycle.
   - [`architecture/database.md`](architecture/database.md) — schema,
     migrations, ownership.
   - [`architecture/data-model.md`](architecture/data-model.md) —
     entities, relationships, ERD.
   - [`architecture/plugins.md`](architecture/plugins.md) — plugin
     system, registry, sandbox.
2. **API reference** — every endpoint, request/response shapes, auth.
   - [`api/README.md`](api/README.md) — auth model and conventions
     shared by every route.
   - Per-area files: `auth.md`, `backtest.md`, `portfolio.md`,
     `market-data.md`, `webhooks.md`, `webhook.md`, `legal.md`,
     `privacy.md`, `tax.md`, `system.md`, `marketplace.md`,
     `strategies.md`, `scoring.md`, `reference.md`.
3. **Operating it** — production concerns.
   - [`deployment.md`](deployment.md) — env vars, infra, rollout.
   - [`operations/slos.md`](operations/slos.md) — SLOs and burn-rate
     alerts.
   - [`operations/backup-and-recovery.md`](operations/backup-and-recovery.md) —
     backup, PITR, RPO/RTO.
   - [`operations/runbooks/`](operations/runbooks/) — one runbook per
     alert group.
   - [`operations/load-testing.md`](operations/load-testing.md) —
     performance regression baseline.
4. **Decisions and history** — *why* it's like this.
   - [`adr/`](adr/) — Architecture Decision Records (append-only).
   - [`legal/processors.md`](legal/processors.md) — how the legal
     document templates are processed.
   - [`observability/logging.md`](observability/logging.md) — log
     schema and redaction rules.
5. **Honest gaps**.
   - [`known-limitations.md`](known-limitations.md) — what doesn't
     work yet, prioritised.
6. **Meta**.
   - [`development.md`](development.md) — local dev environment.
   - [`contributors.md`](contributors.md) — contributor recognition.
   - [`RELEASING.md`](RELEASING.md) — release runbook.
   - [`PLUGIN_DEV_GUIDE.md`](PLUGIN_DEV_GUIDE.md) — strategy author
     guide.

## Conventions

- **Source of truth = code.** Markdown here describes the *current*
  shape. When you change behaviour, change the doc in the same PR.
- **No forward-looking statements in the architecture tree.** Roadmap
  items go in `STRATEGY.md` or an ADR. If you find yourself writing
  "we will" or "soon", it belongs in an ADR.
- **Each file < 500 lines.** Split into per-area files when they grow.
- **Link relatively.** `[`docs/foo.md`](../foo.md)` so links work in
  GitHub, VSCode, and any future MkDocs build.

## File index (alphabetical)

```
docs/
├── README.md                        ← you are here
├── LAST_AUDIT.md                    ← timestamp of last reconciliation
├── PLUGIN_DEV_GUIDE.md              ← guide for strategy authors
├── RELEASING.md                     ← release runbook
├── contributors.md                  ← contributor recognition
├── deployment.md                    ← production deployment
├── development.md                   ← local dev setup
├── known-limitations.md             ← tech debt + missing features
├── adr/
│   ├── README.md                    ← ADR process and index
│   ├── 0001-scaffold-tech-choices.md
│   ├── 0002-auth-rbac.md
│   ├── 0003-mobile-app-strategy.md
│   └── template.md
├── api/
│   ├── README.md                    ← auth model + conventions
│   ├── auth.md                      ← /auth + /mfa + /api-keys
│   ├── backtest.md
│   ├── legal.md
│   ├── market-data.md
│   ├── marketplace.md
│   ├── portfolio.md
│   ├── privacy.md
│   ├── reference.md
│   ├── scoring.md
│   ├── strategies.md
│   ├── system.md
│   ├── tax.md
│   ├── webhooks.md
│   └── websocket.md
├── architecture/
│   ├── README.md
│   ├── overview.md
│   ├── database.md
│   ├── data-model.md                ← entities, relationships, ERD
│   ├── plugins.md
│   ├── plugin-sdk-architecture.jsx
│   └── trading-framework-architecture.jsx
├── legal/
│   └── processors.md
├── observability/
│   └── logging.md
└── operations/
    ├── backup-and-recovery.md
    ├── dr-drill-checklist.md
    ├── load-testing.md
    ├── slos.md
    └── runbooks/
        ├── README.md
        ├── api-availability.md
        ├── api-latency.md
        ├── auth-mfa.md
        ├── backtest-submit.md
        ├── task-pipeline.md
        └── webhook-delivery.md
```

## Updating this index

When you add a doc, add a row here in the same PR. The reviewer is the
second line of defence against an out-of-date index — don't make them
guess where the new file lives.
