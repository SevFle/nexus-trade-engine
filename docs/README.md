# Nexus Trade Engine — Documentation

<!--
Documentation stack choice: plain Markdown rendered by GitHub (and any
CommonMark viewer). The repo is a Python project, so MkDocs Material
would be a defensible choice — but the existing tree already mixes
hand-written Markdown across `docs/`, top-level `*.md`, `legal/`, and
`observability/`, with no static-site generator configured. Adding
MkDocs now would either (a) duplicate content into a `site/` build
artifact or (b) force a `mkdocs.yml` rewrite that moves files and breaks
the inbound links already scattered through `README.md`, `CONTRIBUTING.md`,
`GOVERNANCE.md`, and every ADR.

We pick plain Markdown instead because:
  - Every doc renders on github.com without build infra.
  - The repo is open-source-leaning; GitHub-native keeps the surface
    trivially browsable from a phone, an iPad, or `gh repo view`.
  - The ADR / architecture / operations / runbook split already follows
    the standard "Markdown in /docs" layout.
  - There is no CI bandit for deploying a built site, so MkDocs Material
    would only add overhead.

If/when the team wants a hosted docs site, the upgrade path is:
  - `mkdocs-material` with `nav` pointing at these files, no moves.
  - Wire `mkdocs build` into `.github/workflows/docs.yml` and deploy to
    GitHub Pages. The plain-Markdown files are already valid input.
-->

This directory is the canonical reference for engineers working on the
engine. Code comments and commit messages capture *what changed*; the
docs here capture *why it is shaped this way* and *how to operate it*.

## What lives where

| Path                                          | Audience           | Contents                                                       |
|-----------------------------------------------|--------------------|----------------------------------------------------------------|
| [`README.md`](../README.md)                   | Everyone           | Project pitch + 60-second quick start.                          |
| [`architecture/`](architecture/)              | Engineers          | Component boundaries, request lifecycle, plugin model, DB.      |
| [`api/`](api/)                                | API consumers      | Endpoint reference with request/response shapes and auth.      |
| [`adr/`](adr/)                                | Engineers          | Immutable record of major technical decisions.                  |
| [`data-model.md`](data-model.md)              | Engineers, DBAs    | Entities, relationships, constraints.                          |
| [`decisions.md`](decisions.md)                | Engineers          | Plain-index summary of decisions that don't have an ADR yet.   |
| [`setup.md`](setup.md)                        | New contributors   | Dev environment, env vars, running the test suite.              |
| [`deployment.md`](deployment.md)              | Operators          | Container image, compose stack, rollout.                       |
| [`runbooks.md`](runbooks.md)                  | On-call            | Diagnosis steps for the most likely production issues.         |
| [`limitations.md`](limitations.md)            | Everyone           | Honest, prioritized list of what doesn't work yet.             |
| [`operations/`](operations/)                  | On-call, SRE       | SLOs, backup/DR, load testing, drill checklists.                |
| [`observability/`](observability/)            | On-call            | Logging conventions and trace context propagation.              |
| [`legal/`](legal/)                            | Operators          | Operator / processor responsibilities under the bundled ToS.    |
| [`PLUGIN_DEV_GUIDE.md`](PLUGIN_DEV_GUIDE.md)  | Strategy authors   | How to write and ship a plugin.                                 |
| [`RELEASING.md`](RELEASING.md)                | Maintainers        | Cutting a release.                                              |
| [`development.md`](development.md)            | Engineers          | Dev workflow: native vs. docker-dev, hot-reload, lint/typecheck. |
| [`contributors.md`](contributors.md)          | New contributors   | Onboarding checklist.                                           |
| [`LAST_AUDIT.md`](LAST_AUDIT.md)              | Maintainers        | Most recent architecture audit summary.                         |

## Reading order for a new engineer

1. [`../README.md`](../README.md) for the pitch.
2. [`architecture/overview.md`](architecture/overview.md) for the boxes.
3. [`architecture/components.md`](architecture/components.md) for the
   component diagram and boundary rules.
4. [`architecture/data-flow.md`](architecture/data-flow.md) for what
   happens to a single request end-to-end.
5. [`data-model.md`](data-model.md) for what's stored where.
6. [`setup.md`](setup.md) to bring up a dev stack.
7. [`adr/0001-scaffold-tech-choices.md`](adr/0001-scaffold-tech-choices.md)
   and the rest of the ADR index for the *why*.

## Reading order for an operator

1. [`deployment.md`](deployment.md) to provision a stack.
2. [`operations/slos.md`](operations/slos.md) to know what "healthy"
   means.
3. [`runbooks.md`](runbooks.md) and [`operations/runbooks/`](operations/runbooks/)
   for when it isn't.
4. [`operations/backup-and-recovery.md`](operations/backup-and-recovery.md)
   before the first user pays you.

## Conventions

- **Cross-references** use relative paths so links work both on GitHub
  and in a local editor.
- **ADRs are append-only.** Decision changed? Open a new ADR and mark
  the old one `Superseded by 00NN`. See [`adr/README.md`](adr/README.md).
- **Runbooks assume the operator has shell access to the engine host**
  and to the Postgres / Valkey containers. Cloud-only operators
  translate the docker exec / psql calls to their platform's equivalents.
- **No emojis** in docs (matches the `opencode` style).
- **One topic per file, under 500 lines.** Split when a file grows past
  that — the API reference is already split by area for this reason.
