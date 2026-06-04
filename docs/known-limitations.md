# Known Limitations & Technical Debt

An honest inventory, ranked by impact. Each item is something an
operator or new contributor will trip over — and what they should do
about it today.

Priority legend:

- **P0** — incorrect or unsafe behaviour. Fix before exposing to
  untrusted input or live money.
- **P1** — operational fragility or missing core feature on the
  published roadmap. Fix in the next quarter.
- **P2** — ergonomics / polish. Fix when next touching the area.

---

## P0 — Backtest results are not persisted

**Where**: [`engine/api/routes/backtest.py:22`](../engine/api/routes/backtest.py:22)

The `POST /api/v1/backtest/run` route stores results in an
**in-process Python dict** (`_backtest_results`) with a 1-hour TTL.
The `BacktestResult` table exists (migration 002) and is used by the
strategy evaluator, but the REST endpoint does not write to it.

**Impact**: every process restart loses in-flight and completed
backtests. Multi-replica deploys cannot share results. The GDPR export
gracefully handles orphaned rows (gh#157) but the rows aren't being
created in the first place.

**Workaround today**: poll `GET /backtest/results/{id}` within 1 hour
of submission, from the same replica. Operators running behind a load
balancer need sticky sessions on `/api/v1/backtest/*`.

**Fix path**: switch `_run_backtest_background` to write to
`backtest_results` and the GET handler to query by `id` + `user_id`.
The `BackgroundTasks` call should become a TaskIQ enqueue.

---

## P1 — Three Execution Modes (Roadmap: partial)

Live and paper execution land in `engine/core/execution/`, but the
public surface only exposes backtest. Specifically:

- `engine/core/execution/paper.py` and `live.py` exist but are wired
  to nothing on the API surface.
- `engine/core/live/loop.py` and `kill_switch.py` are scaffolded; the
  live loop has no route entry, no worker task, and no LB / health
  integration.
- The README lists "Live broker integration (Alpaca, IBKR)" as a
  roadmap item; only `AlpacaDataProvider` (read-only market data) is
  shipped.

**Workaround today**: the engine is a **backtest engine** for
production purposes. Treat the live execution code as an internal
preview.

---

## P1 — Strategy Marketplace is a stub

**Where**: [`engine/api/routes/marketplace.py`](../engine/api/routes/marketplace.py)

`browse`, `install`, `uninstall`, `rate` all return
`{"status": "not_implemented"}`. The categories endpoint returns a
hardcoded list. There is no marketplace registry (local or remote)
behind the routes.

**Workaround today**: install strategies by placing them under
`engine/plugins/<kind>/<name>/` and reloading the plugin registry.
The marketplace routes exist purely to lock the public API shape.

---

## P1 — Data provider registry has no first-class credentials store

**Where**: [`engine/data/providers/config.py`](../engine/data/providers/config.py),
[`config/data_providers.example.yaml`](../config/data_providers.example.yaml).

Provider secrets (Polygon API key, Alpaca key/secret, Binance) are
read from the YAML at `NEXUS_DATA_PROVIDERS_CONFIG`. That YAML is
expected to live on disk and be readable by the engine process.

**Impact**: secret rotation requires a process restart; no integration
with Vault / AWS Secrets Manager / GCP Secret Manager; no per-tenant
secrets.

**Workaround today**: render the YAML at deploy time from your secrets
manager of choice (envsubst, Helm, etc.) and ship it as a bind-mounted
read-only file.

---

## P1 — WebSocket manager is process-local

**Where**: [`engine/api/websocket/manager.py:9`](../engine/api/websocket/manager.py:9).

The `ConnectionManager` is a single-process dict. Broadcasts do not
cross replica boundaries.

**Workaround today**: deploy a single replica if you need WebSocket
delivery guarantees, or accept that broadcasts are best-effort per
replica.

**Fix path**: bridge the manager through a Valkey pubsub channel,
consume on each replica. The module docstring calls this out as an
intentional follow-up.

---

## P1 — TaskIQ plumbing incomplete

`engine/tasks/worker.py` defines the broker; the compose file runs
it. But:

- `POST /backtest/run` uses FastAPI `BackgroundTasks`, not TaskIQ —
  so backtests run in the *web* process, not the worker. A long
  backtest stalls the uvicorn worker pool.
- The task-pipeline SLO in [`operations/slos.md`](operations/slos.md)
  is defined but has no emitter yet — `nexus.task.runs_total` is the
  intended metric name.

**Workaround today**: run uvicorn with `--workers N` and tune
`NEXUS_WORKER_CONCURRENCY` high enough to absorb long backtests. Do
not depend on the worker process for backtest isolation today.

---

## P2 — Per-route ignore list in `pyproject.toml` is large

**Where**: [`pyproject.toml`](../pyproject.toml) lines 73–130.

`[tool.ruff.lint.per-file-ignores]` has accumulated many specific
overrides (PLR2004, PLC0415, E501, etc.). This is "correct" in that
each ignore was added deliberately, but it makes the lint signal
weaker overall and hides real issues.

**Fix path**: chip away — every PR that touches one of these files
should try to drop the ignore. Don't fix in one mega-PR.

---

## P2 — Marketplace `MarketplaceEntry` Pydantic model is unused

Defined in [`routes/marketplace.py:15`](../engine/api/routes/marketplace.py:15)
but never returned by any handler. Remove when the marketplace stub
is replaced.

---

## P2 — No Alembic check in CI

There is no automated check that `alembic upgrade head` against an
empty DB matches the SQLAlchemy models. Drift is caught only when a
human reads `models.py` and the migration side-by-side.

**Fix path**: add a CI job that boots an empty Postgres service,
runs `alembic upgrade head`, then asserts each model table exists.
~30 lines of bash.

---

## P2 — Test coverage gate at 70% (`make test`), 80% in `pyproject.toml`

`Makefile` runs `pytest --cov-fail-under=70`; `pyproject.toml`
declares `fail_under = 80`. The Makefile is the canonical entry
point. The mismatch is unintentional drift; the project actually
passes the 80% gate (per recent CI runs and `LAST_AUDIT.md`).

**Fix path**: align both at 80%.

---

## P2 — SLO metrics backend is null by default

`set_metrics(PrometheusBackend())` runs in the lifespan, but if no
Prometheus scrape target is wired the SLOs in
[`operations/slos.md`](operations/slos.md) emit zeros forever. This
is a monitoring gap, not a code bug.

**Workaround today**: wire Prometheus against `/metrics` and
Alertmanager against the rule file at
[`observability/prometheus/slo-rules.yaml`](../observability/prometheus/slo-rules.yaml).

---

## P2 — Live trading routes not yet SLO'd

The SLO table in `operations/slos.md` calls out that live trading
will need its own SLOs when #109/#111 land. Today there's nothing to
SLO because live isn't shipped.

---

## P2 — Many ADR-level decisions not yet captured

`docs/adr/` has three ADRs. Several other decisions that shape the
codebase are recorded only as PR descriptions or commit messages —
e.g. the choice of TaskIQ over Celery, Valkey over Redis-py, bcrypt
over Argon2 for passwords. Use [`adr/template.md`](adr/template.md)
to capture these as they come up in code review.

---

## Not-a-limitation: intentional non-goals

These come up enough in code review that they're worth listing:

- **Not multi-tenant SaaS.** One operator per database. Multi-org is
  out of scope for the foreseeable future (see
  [`architecture/overview.md`](architecture/overview.md)).
- **No embedded DB for production.** SQLite is supported in tests
  only; production needs Postgres (TimescaleDB optional but
  recommended).
- **No sync DB sessions in handlers.** Async-only. Don't add sync
  code paths even for "quick scripts" — they block the event loop.
- **No first-class mobile app.** ADR-0003 covers this; the API is
  the public surface.
