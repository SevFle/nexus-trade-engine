<!--
Documentation stack choice: plain Markdown in /docs.

We picked plain Markdown over MkDocs Material (the obvious Python
candidate) for three concrete reasons:

1.  The repo is a single-tenant OSS product. There is no marketing site
    to integrate with, no versioned docs site to host, and no need for
    a search index beyond GitHub's code search. A static-site generator
    would buy us a nicer URL structure and a search bar; it would cost
    us a build step, a deployment pipeline, and a third place to keep
    prose in sync with code (alongside the repo's markdown and the
    in-code docstrings).

2.  The existing tree is already plain Markdown, organised by topic
    (`architecture/`, `adr/`, `operations/runbooks/`, `observability/`,
    `legal/`). Adopting MkDocs now would force a 1:1 rename pass
    (`overview.md` -> `index.md`) across every link in the repo and
    in the source (`engine/api/routes/*.py` docstrings link into this
    tree). The win doesn't justify the churn yet.

3.  Mermaid diagrams render natively on GitHub. The architecture
    overview uses one; MkDocs Material would need the
    `mkdocs-mermaid2-plugin` to do the same thing the GitHub web view
    already does for free.

Revisit if/when:
- We ship a versioned docs site (multi-version docs are genuinely
  painful in plain Markdown).
- We need full-text search across the docs from somewhere other than
  GitHub.
- A non-engineer audience (PM, sales) starts consuming these docs.
-->

# Nexus Trade Engine — Documentation

This directory is the engineering documentation set for the Nexus
Trade Engine. It is written for engineers — both new contributors
trying to find their bearings and operators running a deployment in
production. Everything in here is markdown so it renders in the GitHub
web UI without a build step.

If you are new, read in this order:

1. **[Architecture overview](architecture/overview.md)** — what the
   moving pieces are and how a request flows through them. Start here.
2. **[Data model](architecture/data-model.md)** — entities,
   relationships, and the constraints the engine relies on.
3. **[API reference](api/)** — every public HTTP route, its
   request/response shape, and what auth it requires.
4. **[Development setup](development.md)** — getting the engine, the
   worker, the database, and the frontend running locally.
5. **[Deployment](deployment.md)** — what a production deploy looks
   like, including required environment variables and the rollout
   process.
6. **[Known limitations & tech debt](limitations.md)** — what is
   honestly unfinished, in priority order.
7. **[Operations & runbooks](operations/)** — what to do when the
   pager goes off.
8. **[Architecture Decision Records](adr/)** — *why* the system is
   shaped the way it is.

## Top-level index

| Document                                              | Audience          | What it covers                                                            |
|-------------------------------------------------------|-------------------|---------------------------------------------------------------------------|
| [`architecture/overview.md`](architecture/overview.md)         | All               | Components, request lifecycle, key dependencies, where to put new code.  |
| [`architecture/data-model.md`](architecture/data-model.md)     | Backend engineers | Every entity, every constraint, every relationship.                      |
| [`architecture/database.md`](architecture/database.md)         | Backend engineers | Migration policy, async access patterns, TimescaleDB usage.              |
| [`architecture/plugins.md`](architecture/plugins.md)           | Plugin authors    | Strategy SDK, manifest format, sandboxing.                               |
| [`api/README.md`](api/README.md)                               | API consumers    | Endpoint catalogue, auth model, request/response shapes.                 |
| [`api/auth.md`](api/auth.md)                                   | API consumers    | Local, OAuth (Google, GitHub), OIDC, LDAP, MFA, API keys.                |
| [`api/websocket.md`](api/websocket.md)                         | API consumers    | Subscribe / publish protocol over the WS endpoint.                       |
| [`development.md`](development.md)                             | Contributors     | Native + Docker dev environments, test suite, lint, typecheck, migrations. |
| [`deployment.md`](deployment.md)                               | Operators        | Single-node prod deploy, infra requirements, env vars, rollout.          |
| [`limitations.md`](limitations.md)                             | All               | What does not work yet, and the order we plan to fix it in.              |
| [`contributors.md`](contributors.md)                           | Contributors     | Mental model, "where to add what", common gotchas.                      |
| [`operations/slos.md`](operations/slos.md)                     | Operators        | Service Level Objectives, error budgets, burn-rate alerts.               |
| [`operations/backup-and-recovery.md`](operations/backup-and-recovery.md) | Operators | Backup strategy, PITR, secrets management, restore procedures.           |
| [`operations/dr-drill-checklist.md`](operations/dr-drill-checklist.md)     | Operators | Quarterly disaster-recovery drill procedure.                             |
| [`operations/load-testing.md`](operations/load-testing.md)                 | Operators | k6 load profile and how to reproduce perf regressions.                  |
| [`operations/runbooks/`](operations/runbooks/)                             | On-call           | One-page playbook per alert group.                                       |
| [`observability/logging.md`](observability/logging.md)                     | Operators        | Structured-log schema, correlation ids, log sinks.                       |
| [`adr/`](adr/)                                              | All               | Architecture Decision Records — *why*, not *what*.                       |
| [`legal/processors.md`](legal/processors.md)                              | Compliance       | Data-processor register for GDPR Art. 30.                               |
| [`PLUGIN_DEV_GUIDE.md`](PLUGIN_DEV_GUIDE.md)                              | Plugin authors    | End-to-end strategy authoring walkthrough.                              |
| [`RELEASING.md`](RELEASING.md)                                            | Maintainers       | Release-please workflow + image publishing.                              |
| [`LAST_AUDIT.md`](LAST_AUDIT.md)                                          | Maintainers       | Latest automated audit (generated by the self-evolver loop).            |

## Conventions

- **Source of truth is the code.** When prose and code disagree, code
  wins; please open a PR that fixes the prose.
- **Markdown files in this tree describe the *current* state.**
  Forward-looking design lives in an ADR.
- **Link, don't duplicate.** If the same explanation fits in two
  places, pick the more specific home and link from the other.
- **Each doc answers four questions**: what it does, what its inputs
  and outputs are, what it depends on, and where the code lives.
- **File length: ≤ 500 lines.** Split when approaching the limit.

## Updating docs

When you change behaviour that an operator or API consumer can see,
update the doc in the same PR. The CI lint pass does not enforce this
(it can't), but reviewers will.

If a decision is non-obvious enough that a future contributor will
ask "why is it like that?", write an ADR. See
[`adr/README.md`](adr/README.md) for the bar.

## See also

- [`README.md`](../README.md) — project overview and quick-start.
- [`CONTRIBUTING.md`](../CONTRIBUTING.md) — branching, TDD workflow,
  PR checklist.
- [`SECURITY.md`](../SECURITY.md) — vulnerability disclosure channel.
- [`GOVERNANCE.md`](../GOVERNANCE.md) — decision-making process.
