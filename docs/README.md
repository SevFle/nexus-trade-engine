<!--
  Documentation stack justification:
  This is a Python (FastAPI) + React project. We chose plain Markdown
  in /docs because:
  1. The project already has extensive Markdown docs in /docs (architecture/,
     adr/, operations/, legal/, observability/). Introducing MkDocs or another
     build tool would create a parallel doc system and fragment the content.
  2. GitHub renders Markdown natively — zero build step, zero deployment.
  3. The README.md at project root already links into this tree; a static
     site generator would add complexity without proportional value for a
     team that primarily reads docs on GitHub.
  If the team later wants a published docs site, MkDocs Material is the
  natural choice (Python-native, good search, Mermaid support). The content
  here is structured to be MkDocs-compatible: one h1 per file, relative
  links, no HTML extensions.
-->

# Nexus Trade Engine — Documentation

## Getting Started

| Document | Description |
|----------|-------------|
| [Development Setup](development.md) | Local dev environment, Docker, tests, migrations, linting |
| [Deployment](deployment.md) | Production infrastructure, configuration, rollout process |
| [Architecture](architecture.md) | System components, data flow, execution pipeline, boundaries |

## Reference

| Document | Description |
|----------|-------------|
| [API Reference](api-reference.md) | All REST endpoints, request/response shapes, auth requirements |
| [Data Model](data-model.md) | Database entities, relationships, constraints, migration policy |
| [Technical Decisions](technical-decisions.md) | ADR-style entries for major architectural choices |

## Operations

| Document | Description |
|----------|-------------|
| [Runbooks](runbooks.md) | Debugging the most common production issues |
| [Limitations & Technical Debt](limitations-and-debt.md) | Honest inventory of known gaps and debt |

## Existing Specialized Docs

| Path | Description |
|------|-------------|
| [architecture/overview.md](architecture/overview.md) | Component inventory, request lifecycle, where-to-put-new-code guide |
| [architecture/database.md](architecture/database.md) | Migration policy, TimescaleDB usage, async access patterns |
| [architecture/plugins.md](architecture/plugins.md) | Plugin system internals |
| [adr/](adr/) | Architecture Decision Records (MADR format) |
| [operations/](operations/) | SLOs, backup/recovery, DR drills, load testing |
| [operations/runbooks/](operations/runbooks/) | Granular per-subsystem runbooks |
| [observability/logging.md](observability/logging.md) | Structured logging conventions |
| [legal/processors.md](legal/processors.md) | Data processor documentation |
| [PLUGIN_DEV_GUIDE.md](PLUGIN_DEV_GUIDE.md) | Strategy plugin developer guide |
| [RELEASING.md](RELEASING.md) | Release automation with release-please |
