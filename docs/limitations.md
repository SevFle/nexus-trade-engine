# Known limitations & technical debt

Honest list of what is half-built, what was deferred on purpose,
and what to pick up next. Ordered by *operational risk*, not by
feature request order — the top of this page is what an on-call
engineer should be most aware of.

Priority guide:

- **P0** — data loss, security, or correctness risk. Fix before
  shipping anything new.
- **P1** — missing functionality that blocks a stated roadmap
  deliverable.
- **P2** — DX or polish gap. Annoying but not dangerous.

---

## P0 — backtest result store is in-process

The backtest runner stores results in a module-level dict
(`_backtest_results` in
[`engine/api/routes/backtest.py:22`](../engine/api/routes/backtest.py))
with a 1-hour TTL. Implications:

- **Lost on restart.** If the API process restarts between
  `POST /run` (202 Accepted) and `GET /results/{id}`, the caller
  gets a 404 and there is no recovery path.
- **Sticky to a single process.** Multi-worker uvicorn
  (`--workers N`) breaks this: the worker that ran the backtest is
  not necessarily the one serving the GET. Symptom is intermittent
  404s.
- **The DB row is written**, but the full per-bar equity /
  drawdown curve is not — only the summary metrics land in
  `BacktestResult.metrics`.

**Fix path.** Persist the full result to a `backtest_result_detail`
table (or to S3 / object storage with a presigned URL in the row),
swap `_run_backtest_background` for a TaskIQ task so the worker
process owns the computation, and add an `enqueue_id` lookup that
returns `pending | completed | failed` regardless of which process
served the GET. The TaskIQ broker is already wired; the gap is the
storage layer.

## P0 — backtest runs on the API process

`POST /api/v1/backtest/run` uses FastAPI `BackgroundTasks`, which
runs in the API event loop. A long backtest blocks the worker and
inflates P99 latency for every other route.

**Fix path.** Move the runner into a TaskIQ task (broker already
configured), keep the API route as a pure dispatcher, and poll
status via `GET /results/{id}`.

## P0 — marketplace is a stub

`engine/marketplace/` is an empty package.
[`engine/api/routes/marketplace.py`](../engine/api/routes/marketplace.py)
returns hard-coded shapes for `browse` / `categories` and returns
`{"status": "not_implemented"}` for `install` / `uninstall` /
`rate`. The route is in the public API; clients that depend on it
will see shape changes when the real implementation lands.

**Fix path.** Decide whether the registry is local-DB or a remote
catalog. Track work in a dedicated ADR before writing the schema.

## P1 — sandboxed plugin runtime is partial

`engine/plugins/sandbox.py` and `engine/plugins/restricted_importer.py`
exist, and there are tests in
[`tests/test_nexus_sdk_strategy.py`](../tests/test_nexus_sdk_strategy.py),
but:

- Network egress is gated by the manifest's `requires_network`
  flag, which the manifest author declares themselves — not
  enforced by the sandbox.
- No memory / CPU cap. A runaway strategy can OOM the engine.
- No per-strategy storage quota.

**Fix path.** Pick one: (a) run untrusted strategies in a
subprocess with resource limits via `resource.setrlimit`, (b)
containerize them via firecracker / gVisor. (a) is the cheaper
interim fix; (b) is the right long-term answer. Document the
choice in a new ADR before writing code.

## P1 — React frontend is missing

The frontend directory exists and has tooling (Vite, Tailwind,
react-query) but no product-shaped UI. The README links to a
dashboard that doesn't exist yet. All functionality is reachable
only through Swagger / curl.

**Fix path.** Roadmap item; see `EXECUTION_ORDER.md` for the
sequencing relative to live trading.

## P1 — live trading not wired

`engine/core/execution/live.py` exists but no broker integration
ships in the repo. The `ICostModel` contract, OMS state machine,
and risk engine are designed for live trading; the missing piece
is a real broker adapter (Alpaca / IBKR).

**Fix path.** Land one broker behind a feature flag
(`NEXUS_ENABLE_LIVE_TRADING`). The kill switch is already
implemented in `engine/core/live/kill_switch.py`.

## P1 — JWT secret rotation is documented but untested in CI

`NEXUS_SECRET_KEY` + `NEXUS_SECRET_KEY_PREVIOUS` enables dual-key
acceptance, but there's no integration test that covers the
rotation window. A bad rotation (e.g. wrong key format) would lock
every user out with no warning.

**Fix path.** Add a `tests/test_secret_rotation.py` that mints a
token under the previous key, rotates, and asserts the token still
validates. Mark it `@pytest.mark.integration`.

## P1 — no per-portfolio ACLs

`Portfolio.user_id` is the only authorization check. Sharing a
portfolio between users (read-only advisor, multi-trader team) is
impossible without DB-level hacks. See ADR-0002 "Open questions".

**Fix path.** New `portfolio_members(portfolio_id, user_id, role)`
table + a `portfolio_role` enum. Add a new ADR for the access
model before changing the schema.

## P1 — backtest_results portfolio_id is nullable, orphan rows

Migration `003_bt_result_nullable_pid` made the FK nullable so
ad-hoc backtests (no portfolio) work. The GDPR export pipeline
fixed the orphan-read path (commit `8d8f8a2`), but there's still
no scheduled job that purges orphans older than the retention
window.

**Fix path.** Wire the existing `engine/data/retention_cleanup.py`
to also clean `backtest_results` rows with `portfolio_id IS NULL
AND created_at < now() - interval '90 days'`. Schedule via TaskIQ
cron.

## P2 — coverage gate is at 70% in Makefile but 80% in pyproject

`pyproject.toml` sets `fail_under = 80`, but `Makefile` calls
`pytest --cov-fail-under=70`. CI uses the Makefile. Either:

- raise the Makefile gate to 80 (the codebase already exceeds it),
  or
- delete one of the two so the contract is single-sourced.

Low priority; the gap doesn't ship bugs, but it makes coverage
discussions confusing.

## P2 — `_optional_user` lives in the legal route

`engine/api/routes/legal.py:31` defines `_optional_user`, a local
helper that mirrors the global auth dependency but tolerates a
missing token. This is a reusable pattern that other public-but-
optional-auth routes will need; it should move to
`engine/api/auth/dependency.py`.

## P2 — no aggregate UI for DSR

DSR rows are created and the operator can pull them via the API,
but there's no admin UI to triage / batch-complete them. A GDPR
Art. 12 SLA breach is silent until a user complains.

**Fix path.** Either an admin route (`GET /api/v1/admin/dsr`) or
a Grafana panel over `dsr_requests`. The panel is the smaller
lift and fits the existing observability stack.

## P2 — five test files have `# noqa: S108` blanket-ignored

`pyproject.toml` ignores S108 (insecure temp file) for the test
tree because fixtures legitimately write temp files. A handful of
tests use `NamedTemporaryFile` for sensitive-looking paths; review
whether any actually process user input and tighten the ignore to
file-level where possible.

## P2 — observability: log sampling defaults to 1.0 for INFO

`NEXUS_LOG_SAMPLING_INFO=1.0` in dev means the full INFO stream is
emitted. Production operators should set this lower (e.g. `0.1`)
to keep log volume manageable. Not a bug — but the default trips
operators who copy dev settings into prod.

**Fix path.** Either lower the default for `app_env=production`
or add a sanity log line at startup that warns if sampling = 1.0
in prod.

---

## Tracking

Most of these are linked to GitHub issues in the changelog. When
picking one up:

1. Open an issue if one doesn't exist; tag it `tech-debt` and the
   priority label (`P0` / `P1` / `P2`).
2. Post a one-paragraph plan in the issue before opening a PR.
3. If the fix is non-trivial, write the ADR before the code.
4. Strike the entry from this file in the same PR that closes the
   issue.
