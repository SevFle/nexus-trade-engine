<!--
Documentation stack: MkDocs with the Material theme.

Why MkDocs Material (not plain Markdown / VitePress / Nextra):
  - The engine is a Python project (FastAPI + SQLAlchemy + TaskIQ), with
    a sibling Vite frontend. The audience reading this docs tree is
    operators, on-call engineers, and strategy developers — all of whom
    are served by a static site with cross-linking, search, and version
    banners. Material gives that out of the box.
  - VitePress / Nextra pull a Node toolchain into the build, which the
    Python service team would then have to maintain alongside the
    `frontend/` Vite app already in this repo. Avoiding the duplication
    keeps the docs build fully independent of the frontend build.
  - Plain Markdown (no build) is the fallback, but we lose graph/search
    navigation, and most editors render Mermaid poorly without a build
    step. The cost of `pip install mkdocs-material` is one line in
    `pyproject.toml` `[project.optional-dependencies] docs`.

Build / serve locally:

    uv sync --extra docs
    uv run mkdocs serve -f docs/mkdocs.yml

Publish: GitHub Pages via `.github/workflows/docs.yml` (rendered site
is written to `site/` and uploaded as an artifact).
-->

# Nexus Trade Engine — Documentation

This is the canonical documentation for the Nexus Trade Engine
codebase. Start here; everything else is a link away.

| If you are…                 | Read these in order                                                                 |
|-----------------------------|--------------------------------------------------------------------------------------|
| New to the codebase         | [Architecture overview](architecture/overview.md) → [Data model](data-model.md) → [Development setup](development.md) |
| Onboarding a new strategy   | [Plugin developer guide](PLUGIN_DEV_GUIDE.md) → [Architecture / plugins](architecture/plugins.md) → [API reference](api/README.md) |
| On-call                     | [Operations / SLOs](operations/slos.md) → [Runbooks](operations/runbooks/README.md) → [Deployment](deployment.md) |
| Reviewing a design decision | [ADR index](adr/README.md)                                                            |
| Shipping a release          | [Releasing](RELEASING.md) → [Deployment](deployment.md)                              |

## Document index

### Architecture
- [System overview](architecture/overview.md) — component map, request
  lifecycle, event flow, non-goals.
- [Database](architecture/database.md) — migration policy, TimescaleDB
  usage, async-session conventions.
- [Plugin system](architecture/plugins.md) — manifest format, sandbox
  model, lifecycle.
- [Plugin SDK architecture](architecture/plugin-sdk-architecture.jsx)
  (interactive).
- [Trading framework architecture](architecture/trading-framework-architecture.jsx)
  (interactive).

### API
- [API reference](api/README.md) — auth model, conventions, status
  codes, and an index of endpoint groups.
- [Endpoint reference](api/endpoints.md) — every HTTP and WebSocket
  endpoint, request / response shapes, auth requirements.

### Data
- [Data model](data-model.md) — entities, relationships, constraints,
  index strategy.

### Engineering
- [Development setup](development.md) — native and docker dev stacks.
- [Contributing](../CONTRIBUTING.md) — branching, TDD, PR checklist.
- [Plugin developer guide](PLUGIN_DEV_GUIDE.md) — building a strategy
  plugin end-to-end.
- [Releasing](RELEASING.md) — release-please flow, image publishing.

### Operations
- [Deployment](deployment.md) — environments, infra requirements,
  rollout procedure, secrets.
- [SLOs](operations/slos.md) and [runbook index](operations/runbooks/README.md).
- [Backup & recovery](operations/backup-and-recovery.md).
- [DR drill checklist](operations/dr-drill-checklist.md).
- [Load testing](operations/load-testing.md).

### Legal & privacy
- [Legal document processors](legal/processors.md).
- [Terms of Service](../legal/terms-of-service.md),
  [Privacy Policy](../legal/privacy-policy.md),
  [EULA](../legal/eula.md),
  [Marketplace EULA](../legal/marketplace-eula.md),
  [Risk disclaimer](../legal/risk-disclaimer.md),
  [Data-provider attributions](../legal/data-provider-attributions.md).

### Decisions
- [ADR-0001 — Scaffold tech choices](adr/0001-scaffold-tech-choices.md).
- [ADR-0002 — Auth & RBAC](adr/0002-auth-rbac.md).
- [ADR-0003 — Mobile app strategy](adr/0003-mobile-app-strategy.md).
- [ADR template](adr/template.md).

### Honest gaps
- [Known limitations & technical debt](limitations.md) — what is
  half-built, what is deferred, and what the priority order is.

## Conventions

- Every doc is plain Markdown (CommonMark + GitHub Flavored Markdown).
  Mermaid diagrams are rendered by Material at build time.
- Keep files under 500 lines. Split into a sub-page when a section
  outgrows that — the index above is the navigation source of truth.
- Link to source via relative paths (`../../engine/...`) so links
  resolve on GitHub without a build.
- Date every ADR; mark every runbook with the alert it owns. Don't ship
  an alert without a runbook.
- The docs build is part of CI: a broken link or unrendered Mermaid
  diagram fails the pipeline.
