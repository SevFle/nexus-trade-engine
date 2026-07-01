# Architecture Decision Records

An ADR captures *one decision* that shaped the project: what we
decided, why, what we considered, and what we accepted as a
trade-off. ADRs are append-only — when a decision is reversed, the
new ADR supersedes the old one and the old one is marked accordingly,
but neither is deleted.

We follow the [MADR](https://adr.github.io/madr/) format. See
[`template.md`](template.md) for the boilerplate.

## Index

| Number | Status   | Title                                        |
|-------:|----------|----------------------------------------------|
| 0001   | Accepted (partially superseded by 0006) | [Scaffold technology choices](0001-scaffold-tech-choices.md) |
| 0002   | Accepted | [Auth & RBAC model](0002-auth-rbac.md)        |
| 0003   | Accepted | [Mobile experience strategy — PWA on top of the React frontend](0003-mobile-app-strategy.md) |
| 0004   | Accepted | [Task queue — TaskIQ over Celery / RQ / arq](0004-task-queue-taskiq.md) |
| 0005   | Accepted | [Valkey client + Valkey 8 broker (over redis-py / Redis)](0005-valkey-over-redis.md) |
| 0006   | Accepted | [Password hashing (bcrypt) and MFA secret at rest (Fernet)](0006-bcrypt-fernet.md) |
| 0007   | Accepted | [Strategy sandbox — allowlist import model](0007-strategy-sandbox-allowlist-imports.md) |
| 0008   | Accepted | [Pluggable MetricsBackend Protocol (over hard-coded Prometheus)](0008-pluggable-metrics-backend.md) |
| 0009   | Accepted | [Cross-replica WebSocket event delivery via Redis pub/sub bridge](0009-cross-replica-eventbus-bridge.md) |
| 0010   | Accepted | [Multi-strategy orchestration — two orchestrators and HOLD-as-side](0010-multi-strategy-orchestration.md) |

When you accept a new ADR, add a row to this table in the same PR.

## When to write one

Write an ADR when the answer to "why is it like that?" is going to be
non-obvious to a future contributor. Concrete triggers:

- Picking between multiple viable technologies (e.g. TaskIQ vs Celery
  vs RQ).
- Locking in a contract or schema that other code depends on
  (e.g. webhook payload shape, plugin manifest format).
- Accepting an unusual constraint (e.g. "we will run on a single node;
  no HA story until v1.0").
- Making a decision that contradicts an existing ADR.

Don't write one for: routine bug fixes, refactors, code cleanups,
small library upgrades. The git log + PR description is enough for
those.

## Lifecycle

- **Proposed** — open a PR with the ADR file, status `Proposed`. The
  PR discussion is where the decision is debated.
- **Accepted** — when the PR merges, the status flips to `Accepted`
  and the file becomes immutable except for status changes (e.g.
  `Superseded by 00NN`).
- **Rejected** — the PR is closed without merging. We don't keep
  rejected ADRs in the repo; the discussion lives on the PR.
- **Superseded** — a newer ADR replaces it. Edit the older ADR's
  `Status:` line to `Superseded by 00NN — <title>` and add a backlink
  in the new one. Don't delete the file.

## Numbering

Sequential, four digits, zero-padded. Pick the next number when you
open the PR. If two PRs land in the same window with conflicting
numbers, whoever merges first keeps theirs; the other one renames in
the merge.

## Filename convention

`<NNNN>-<short-kebab-slug>.md` — e.g. `0003-pluggable-metrics-backend.md`.
The slug should match the title closely enough that a `grep` finds
it.

## Where else this links

- [`GOVERNANCE.md`](../../GOVERNANCE.md) — decision-making process
  (lazy consensus + ADR for big changes).
- [`docs/architecture/`](../architecture/) — current state, which is
  the *outcome* of accumulated ADRs.
- [`CONTRIBUTING.md`](../../CONTRIBUTING.md) — when to use an ADR vs.
  a regular PR description.
