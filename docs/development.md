# Development environment

Two ways to run Nexus Trade Engine locally:

1. **Native** — `uv` + a Postgres + Valkey running on your host.
2. **Docker** (recommended) — `make docker-dev` brings up the full
   stack (engine, worker, frontend, Postgres, Valkey) with hot-reload
   on every service.

## Native

```bash
# 1. Install Python deps via uv
uv sync --extra dev

# 2. Bring up Postgres + Valkey via the production compose file
make docker-up      # starts only db + valkey containers in the background

# 3. Run migrations
make migrate

# 4. Run the API with hot-reload (uvicorn --reload)
make dev
```

Runs the engine on `http://localhost:8000`. Tear down infra with
`make docker-down`.

The frontend (Vite) lives in `frontend/`:

```bash
cd frontend
npm install
npm run dev   # http://localhost:5173
```

## Docker (recommended)

`docker-compose.dev.yml` is a self-contained dev stack. Everything is
bind-mounted into the containers so edits take effect immediately:

| Service  | Port | What it does                                   |
|----------|------|------------------------------------------------|
| `db`     | 5432 | TimescaleDB / Postgres 16                      |
| `valkey` | 6379 | Redis-compatible queue / cache                 |
| `app`    | 8000 | FastAPI engine, `uvicorn --reload`             |
| `worker` | —    | TaskIQ worker, `taskiq ... --reload`           |
| `frontend` | 5173 | Vite dev server with HMR                     |

```bash
make docker-dev          # foreground; Ctrl-C to stop
make docker-dev-logs     # in another terminal
make docker-dev-down     # tear down (preserves the pgdata volume)
```

The first build takes 1–2 minutes (uv installs the lockfile + npm
install). Subsequent edits do **not** require a rebuild — bind mounts
make the source visible, and the reload watchers pick up changes.

### When you actually need a rebuild

- `pyproject.toml` / `uv.lock` changed → `make docker-dev-rebuild`.
- `frontend/package.json` / `package-lock.json` changed → delete the
  `frontend-node-modules` named volume and `make docker-dev` again
  (the named volume protects npm install state across runs but pins
  it).
- `Dockerfile.dev` itself changed → `make docker-dev-build`.

### Hot-reload internals

- Python: `uvicorn --reload --reload-dir /app/engine` watches the
  bind-mounted `engine/` tree.
- TaskIQ worker: same `--reload` flag, so taskiq tasks reload too.
- Vite: standard HMR over the websocket on port 5173.
- macOS / Windows: file events are not delivered through bind mounts,
  so the dev compose sets `WATCHFILES_FORCE_POLLING=true` for Python
  and `CHOKIDAR_USEPOLLING=true` for the frontend. Polling adds tiny
  CPU overhead but is the only reliable way.

### When hot-reload doesn't fire

- Confirm the file you edited is inside `engine/` (Python) or
  `frontend/src/` (Vite). Edits outside the watch path do nothing.
- On Linux, hot-reload uses inotify directly — no polling fallback —
  so a saved file should reload within ~100 ms.
- If logs show repeated `WatchedFileChangeError` → your bind mount has
  a permissions mismatch. Try `make docker-dev-down && docker volume rm
  nexus-trade-engine_app-venv && make docker-dev`.

## Running the test suite

```bash
make test                    # full pytest suite (host, against current uv env)
```

Inside the dev stack:

```bash
docker compose -f docker-compose.dev.yml exec app pytest
```

CI runs the same suite against a Postgres service (see
`.github/workflows/ci.yml`).

### Coverage setup

The project has two importable source packages measured for coverage:

| Import name | Filesystem path | Description |
|---|---|---|
| `engine` | `engine/` | Main FastAPI application, DB, API routes |
| `nexus_sdk` | `sdk/nexus_sdk/` | Strategy-plugin SDK |

**Important:** The PyPI project name is `nexus-trade-engine` but there is
no importable package called `nexus_trade_engine`. Always use the actual
package names for coverage:

```bash
# Correct
pytest --cov=engine --cov=nexus_sdk

# WRONG — will produce "Module nexus_trade_engine was never imported"
pytest --cov=nexus_trade_engine
```

Configuration lives in `pyproject.toml`:

- `[tool.pytest.ini_options]` — `addopts` includes
  `--cov=engine --cov=nexus_sdk`, and `pythonpath = [".", "sdk"]` makes
  `nexus_sdk` importable from its `sdk/` directory.
- `[tool.coverage.run]` — `source = ["engine", "nexus_sdk"]` tells
  coverage.py which packages to measure. `fail_under = 80` gates merges.
- `[tool.ruff.lint.isort]` — `known-first-party` must list both
  `engine` and `nexus_sdk` so import sorting stays consistent.
- `Makefile` — the `test` target delegates to `uv run pytest` without
  overriding `--cov-fail-under`, letting `pyproject.toml` own the gate.

Run `make test` (or `pytest`) to get a terminal report and an HTML
report at `htmlcov/index.html`.

## Database migrations

```bash
make migrate                                     # run pending migrations
make migrate-new msg="add foo column to bar"     # autogenerate a revision
```

Migrations live in `engine/db/migrations/versions/`. Conventional
numbering: `008_<short_slug>.py`. The chain is sequential — pick the
next number when adding one.

## Linting & type-checking

```bash
make lint        # ruff check + ruff format --check
make fix         # ruff --fix + ruff format
make typecheck   # basedpyright
```

Run before pushing — CI fails fast on lint regressions.

## Common tasks

| Task | Command |
|------|---------|
| Reset the dev DB | `make docker-dev-down && docker volume rm nexus-trade-engine_pgdata && make docker-dev` |
| Open a psql shell | `docker compose -f docker-compose.dev.yml exec db psql -U nexus nexus` |
| Tail only the engine logs | `docker compose -f docker-compose.dev.yml logs -f app` |
| Run a one-off Python command | `docker compose -f docker-compose.dev.yml exec app python -c "..."` |
| Rebuild everything from scratch | `make docker-dev-rebuild` |

## See also

- [`CONTRIBUTING.md`](../CONTRIBUTING.md) — branching, TDD workflow,
  PR checklist.
- [`docs/RELEASING.md`](RELEASING.md) — how releases are cut.
- [`docs/operations/`](operations/) — production-shaped runbooks
  (backup, SLOs, on-call).
