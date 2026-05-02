# Contributor deep-dive

If you've read [`CONTRIBUTING.md`](../CONTRIBUTING.md) and
[`docs/development.md`](development.md) and you're ready to actually
move code, this doc is the orientation guide. It assumes you've
spent ten minutes reading [`docs/architecture/overview.md`](architecture/overview.md).

## Mental model

Three layers, top to bottom:

1. **Surface** (`engine/api/`, `frontend/`) — HTTP/WebSocket routes,
   React UI. Thin glue that validates input and renders output. No
   business logic lives here.
2. **Domain** (`engine/core/`, `engine/data/`, `engine/events/`,
   `engine/plugins/`) — backtest runner, strategy evaluator,
   execution primitives, plugin registry. Most behaviour lives here.
3. **Infrastructure** (`engine/db/`, `engine/observability/`,
   `engine/tasks/`, `engine/legal/`) — persistence, telemetry,
   background work, compliance. Stable and rarely changed.

When you propose a change, mentally locate it in this stack. A change
that touches only the surface is a UX or contract change. A change
in the domain is a behaviour change. A change in infrastructure is
something that affects every operator.

## How a feature ships

Borrowed from [`CONTRIBUTING.md`](../CONTRIBUTING.md) and refined for
day-to-day:

1. **Open or pick up an issue.** The autonomous-loop directive at the
   top of this repo means many issues are pre-scoped — read the
   labels (`priority-high`, `bucket:*`) before starting.
2. **Branch:** `feat/<slug>-<issue#>`, `fix/...`, `docs/...`. Match
   the conventional-commits prefix you'll use on the PR.
3. **Tests first.** Add a failing test that captures the behaviour
   you want. CI runs the same suite — green-on-laptop ≈ green-on-CI.
4. **Implement to green.** Smallest diff that makes the test pass.
5. **Lint + typecheck.** `make lint && make typecheck`. CI fails fast
   on lint regressions; do this before pushing.
6. **Open the PR** with the template populated (summary, linked
   issue, test plan, breaking-change note, follow-ups).
7. **Adversarially self-review** the diff before requesting review.
   Look for the things you'd flag if it were someone else's code.
8. **Merge** via squash + delete branch. Conventional-commits title
   becomes the changelog entry (release-please reads it).

## Code organization rules

- Keep files **<800 lines**, functions **<50 lines**. Splits should
  follow domain boundaries, not "I made it big".
- Prefer **Protocols** over concrete base classes for plugin-style
  contracts. The metrics backend in
  [`engine/observability/metrics.py`](../engine/observability/metrics.py)
  is the canonical example.
- **No mutation** of mutable inputs. Return a new value instead.
  Especially important for shared dicts / lists threaded through
  middlewares.
- **No silent error swallowing.** Either handle it explicitly or let
  it propagate. The exception is true "best-effort" paths
  (telemetry, retention sweeps) — they should log, not swallow.
- **Validate at the boundary.** Pydantic models on the way in,
  trusted internal types after that. Don't re-validate in domain
  code.

## Where to add what

Same table as [`overview.md`](architecture/overview.md#where-to-put-new-code),
restated here for quick reference:

| Adding…                               | Goes in                                         |
|---------------------------------------|--------------------------------------------------|
| HTTP endpoint                         | `engine/api/routes/<area>.py`                    |
| Background job                        | `engine/tasks/`                                  |
| Strategy / data provider / executor   | A plugin under `engine/plugins/<kind>/<name>/`   |
| Webhook template                      | `engine/events/webhook_dispatcher.py:render_template` + `_VALID_TEMPLATES` |
| Schema change                         | New Alembic revision in `engine/db/migrations/versions/` |
| Metric (general)                      | `get_metrics()` from `engine/observability/metrics.py` |
| Metric backing an SLO                 | Above + update `docs/operations/slos.md` and `observability/prometheus/slo-rules.yaml` |
| Operational runbook                   | `docs/operations/runbooks/<name>.md`             |
| ADR-worthy decision                   | `docs/adr/<NNNN>-<slug>.md`                      |

## Common gotchas

- **`structlog` `event` is reserved.** Pass `event_type=` for the
  domain-event name. The webhook dispatcher already learned this the
  hard way (see [`webhook-delivery runbook`](operations/runbooks/webhook-delivery.md)).
- **`uvicorn --reload` does not pick up edits to imported C
  extensions.** If you're touching `polars` / `numpy` extensions,
  restart manually.
- **Async sessions don't auto-rollback on exception.** Either use a
  context manager that does (`engine.api.deps.get_session`) or
  rollback explicitly in your `except`.
- **Webhook signing secrets are returned only on create.** Don't add
  a route that returns them on read — see
  [`engine/api/routes/webhooks.py`](../engine/api/routes/webhooks.py).
- **Migrations on a production DB lock writes.** For a column add,
  prefer `op.add_column` + a backfill in a follow-up migration over
  a single migration that does both.

## Getting help

- Architecture-level questions → open a discussion (link in
  [`SECURITY.md`](../SECURITY.md) → "Questions and discussion").
- Bug? → [`.github/ISSUE_TEMPLATE/bug.yml`](../.github/ISSUE_TEMPLATE/bug.yml).
- Feature idea? → [`.github/ISSUE_TEMPLATE/feature.yml`](../.github/ISSUE_TEMPLATE/feature.yml).
- Security? → never the public tracker;
  [`SECURITY.md`](../SECURITY.md) has the private channel.

## See also

- [`docs/architecture/`](architecture/) — current-state architecture.
- [`docs/adr/`](adr/) — historical decisions.
- [`docs/operations/`](operations/) — runbooks, backups, SLOs.
- [`docs/RELEASING.md`](RELEASING.md) — how releases get cut.
- [`docs/development.md`](development.md) — local environment setup.
