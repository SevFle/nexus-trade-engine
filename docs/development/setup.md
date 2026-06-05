# Development setup

Everything you need to get a local environment running, run the test
suite, and ship a change.

## Prerequisites

| Tool            | Minimum | Why |
|-----------------|---------|-----|
| Python          | 3.11    | Engine and SDK target (`pyproject.toml:5`). 3.12 also tested. |
| [uv](https://docs.astral.sh/uv/) | any recent | Package manager. Lockfile is `uv.lock`. |
| Docker + Compose | recent | Postgres+TimescaleDB and Valkey run in containers. |
| Node.js         | 20+     | Frontend only. Not required for engine work. |
| Git             | 2.30+   | For pre-commit hooks (if installed). |

`uv` is mandatory: the lockfile is `uv.lock`, not a `requirements.txt`.
Install it with:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## Day-one setup

```bash
git clone <repo-url> nexus-trade-engine
cd nexus-trade-engine

# 1. Install Python deps (creates .venv from uv.lock)
uv sync --all-extras

# 2. Start infrastructure (Postgres + Valkey)
docker compose up -d db valkey

# 3. Run database migrations
.venv/bin/alembic upgrade head

# 4. Seed sample market data and reference index
.venv/bin/python scripts/seed_data.py

# 5. Start the engine (with hot reload)
.venv/bin/uvicorn engine.app:create_app --factory --reload --host 0.0.0.0 --port 8000

# 6. (Optional) Frontend in a separate terminal
cd frontend
npm install
npm run dev
```

After step 5 the API is reachable at <http://localhost:8000>. Swagger
UI at <http://localhost:8000/docs>. Health probe at
<http://localhost:8000/health>.

The `Makefile` wraps the common operations. `make help` lists every
target; the most-used ones are at the top of this document.

## Environment variables

All env vars are prefixed `NEXUS_` and read by `pydantic-settings` at
`engine/config.py:7`. **If a knob exists, it is in that file** — this
list is the digest; the source is the truth.

### Required for any non-test environment

| Var                          | Default (dev) | Purpose |
|------------------------------|---------------|---------|
| `NEXUS_SECRET_KEY`           | *(empty)*     | HS256 JWT signing key. Required outside test env — startup aborts (`engine/app.py:131`). |
| `NEXUS_DATABASE_URL`         | `postgresql+asyncpg://nexus:nexus@localhost:5432/nexus` | SQLAlchemy async URL. |
| `NEXUS_VALKEY_URL`           | `valkey://localhost:6379/0` | Valkey/Redis URL. Used for rate limit, TaskIQ broker, refresh-token store. |
| `NEXUS_POSTGRES_USER`        | *(unset)*     | Required by docker-compose; see `docker-compose.yml:5`. |
| `NEXUS_POSTGRES_PASSWORD`    | *(unset)*     | Same. |
| `NEXUS_POSTGRES_DB`          | *(unset)*     | Same. |

### Auth / MFA

| Var                                          | Default | Purpose |
|----------------------------------------------|---------|---------|
| `NEXUS_AUTH_PROVIDERS`                       | `local` | Comma-separated provider list. Options: `local`, `google`, `github`, `oidc`, `ldap`. |
| `NEXUS_AUTH_LOCAL_ALLOW_REGISTRATION`        | `true`  | Disable to forbid self-registration. |
| `NEXUS_AUTH_OVERWRITE_ROLE_ON_LOGIN`         | `false` | When `true`, federated IdP role claims overwrite the local role. **Default false for safety** (SEV-741). |
| `NEXUS_JWT_ACCESS_TOKEN_EXPIRE_MINUTES`      | `60`    | |
| `NEXUS_JWT_REFRESH_TOKEN_EXPIRE_DAYS`        | `7`     | |
| `NEXUS_MFA_ENCRYPTION_KEY`                   | *(empty)* | Fernet key (url-safe base64, 32 bytes). Empty disables MFA enrollment. |
| `NEXUS_MFA_CHALLENGE_TTL_SECONDS`            | `300`   | |
| `NEXUS_MFA_BACKUP_CODES_COUNT`               | `10`    | |
| `NEXUS_GOOGLE_CLIENT_ID` / `_SECRET` / `_REDIRECT_URI` | *(empty)* | Google OAuth2. |
| `NEXUS_GITHUB_CLIENT_ID` / `_SECRET` / `_REDIRECT_URI` | *(empty)* | GitHub OAuth2. |
| `NEXUS_OIDC_DISCOVERY_URL` / `_CLIENT_ID` / `_CLIENT_SECRET` / `_REDIRECT_URI` | *(empty)* | Generic OIDC. |
| `NEXUS_OIDC_ROLE_CLAIM`                      | `roles` | Claim name to read roles from. |
| `NEXUS_LDAP_SERVER_URL` / `_BIND_DN` / `_BIND_PASSWORD` / `_SEARCH_BASE` / `_ROLE_MAPPING` | *(empty)* | LDAP provider. |

### Data providers

| Var                            | Default | Purpose |
|--------------------------------|---------|---------|
| `NEXUS_DATA_PROVIDERS_CONFIG`  | *(empty)* | Path to a YAML config that wires providers (Polygon, Alpaca, etc.). Empty = `YahooDataProvider` only. |
| `NEXUS_DATA_PROVIDERS_DEFAULT` | `yahoo` | Fallback when no provider matches. |

### Observability

| Var                          | Default | Purpose |
|------------------------------|---------|---------|
| `NEXUS_LOG_LEVEL`            | `INFO`  | |
| `NEXUS_LOG_FORMAT`           | `console` | `json` in production. |
| `NEXUS_LOG_SINK`             | `stdout` | `stdout`, `file`, or `otlp`. |
| `NEXUS_LOG_FILE_PATH`        | `logs/engine.log` | |
| `NEXUS_LOG_SAMPLING_INFO`    | `1.0`   | Fraction of INFO logs to emit. |
| `NEXUS_LOG_SAMPLING_DEBUG`   | `0.01`  | Fraction of DEBUG logs to emit. |
| `NEXUS_OTLP_ENDPOINT`        | *(empty)* | Empty = OTel tracing disabled. |
| `NEXUS_SENTRY_DSN`           | *(empty)* | Empty = Sentry disabled. |
| `NEXUS_APP_VERSION`          | `0.1.0` | Emitted as a Sentry tag and in /system/status. |

### Operational

| Var                                | Default | Purpose |
|------------------------------------|---------|---------|
| `NEXUS_APP_ENV`                    | `development` | `production` enables HSTS and secure cookies. `test` allows empty SECRET_KEY. |
| `NEXUS_APP_DEBUG`                  | `false` | FastAPI debug mode. Do not enable in production. |
| `NEXUS_APP_HOST` / `NEXUS_APP_PORT`| `0.0.0.0:8000` | uvicorn bind. |
| `NEXUS_CORS_ORIGINS`               | `["http://localhost:3000"]` | JSON list of allowed origins. |
| `NEXUS_RATE_LIMIT_PER_MINUTE`      | `600`   | Global default. |
| `NEXUS_RATE_LIMIT_BURST`           | `60`    | Token-bucket burst. |
| `NEXUS_RATE_LIMIT_EXEMPT_PATHS`    | `/health,/metrics` | Comma-separated. |
| `NEXUS_WORKER_CONCURRENCY`         | `4`     | TaskIQ worker concurrency hint. |
| `NEXUS_LEGAL_DOCUMENTS_DIR`        | `legal` | Markdown source for legal docs. |
| `NEXUS_OPERATOR_NAME` / `_EMAIL` / `_URL` | defaults | Substituted into legal doc templates. |
| `NEXUS_JURISDICTION`               | `United States` | |
| `NEXUS_PLATFORM_FEE_PERCENT`       | `30`    | Marketplace revenue share (legal copy). |

### Database tuning

| Var                                | Default | Purpose |
|------------------------------------|---------|---------|
| `NEXUS_DATABASE_POOL_SIZE`         | `5`     | SQLAlchemy pool size. |
| `NEXUS_DATABASE_MAX_OVERFLOW`      | `10`    | Overflow above pool. |

## Tests

The test suite uses pytest with `pytest-asyncio` in auto mode and
`pytest-cov` for coverage. The full pytest config is in
`pyproject.toml:142`.

```bash
# Full suite with coverage gate (fails if <70%)
make test

# Or directly
uv run pytest --cov-fail-under=70

# Fast path (skip slow / integration tests)
uv run pytest -m "not slow and not integration"

# Single file
uv run pytest tests/test_backtest_loop.py

# Verbose, no coverage
uv run pytest tests/test_order_manager.py -v --no-cov

# Parallel (xdist)
uv run pytest -n auto
```

Coverage is enforced at **80%** in `pyproject.toml:160` and the CI
gate is at **70%** via the `Makefile`. The CI gate is intentionally
lower than the project's actual coverage to give newly-added modules
some slack.

### Test fixtures

- `tests/conftest.py` — root fixtures: in-memory DB, FastAPI client,
  auth helpers.
- `tests/factories.py` — factory helpers for building model rows
  without boilerplate.

Tests use the `aiosqlite` driver (an in-memory SQLite) for the
fast-path database; tests marked `@pytest.mark.integration` use the
real Postgres.

### What to run before pushing

```bash
make lint         # ruff check + format check
make typecheck    # basedpyright
make test
```

The CI pipeline runs all three; pre-push hooks (if installed) short
circuit a CI roundtrip.

## Linting and formatting

- **ruff** (`pyproject.toml:60`) is the only checker. It covers
  pyflakes, pycodestyle, isort, pylint subset, bandit security,
  pyupgrade, and many more. Line length is 99.
- **basedpyright** in standard mode is the type checker
  (`pyproject.toml:135`). It runs against the SDK path too
  (`extraPaths = ["sdk"]`).
- **`make fix`** auto-fixes lint errors and applies formatter
  changes.

Per-file rule overrides are documented inline in `pyproject.toml:73`.
Notable patterns:

- Tests may use `S108` (asserting on tmp dir paths).
- `engine/api/routes/*.py` allows `B008` (FastAPI `Depends()` in
  defaults — the framework idiom).
- `engine/plugins/sandbox/core/policy.py` allows `PLR0911` (too many
  return statements — the policy dispatch table is a switch).

## Type checking

```bash
make typecheck
# or
uv run basedpyright
```

Strictness is `standard`. We deliberately suppress these:

- `reportMissingTypeStubs` — many third-party libs lack stubs.
- `reportUnknownMemberType` — OpenTelemetry / SQLAlchemy dynamic
  attributes trip this constantly.

If you add a new module, basedpyright will catch type errors on the
next run. The CI runs it on every PR.

## Database workflows

```bash
# Apply all migrations
make migrate

# Create a new migration (autogenerated)
make migrate-new msg="add foo column to bar"

# Inspect current state
.venv/bin/alembic current
.venv/bin/alembic history --verbose
```

**Downgrades are not tested in CI.** Treat them as destructive and
prefer forward-fixing.

For ad-hoc DB inspection:

```bash
docker compose exec db psql -U "$NEXUS_POSTGRES_USER" "$NEXUS_POSTGRES_DB"
```

## Worker

To run the TaskIQ worker locally (needed for backtest submissions
that go through the broker rather than FastAPI `BackgroundTasks`):

```bash
.venv/bin/taskiq worker engine.tasks.worker:broker
```

Or via Docker:

```bash
docker compose up -d worker
```

Worker logs are emitted with the same structlog config as the engine;
correlation ids from the producing request are preserved across the
broker hop.

## Frontend

```bash
cd frontend
npm install
npm run dev          # http://localhost:3000 with hot reload
npm run build        # production bundle in dist/
npm run lint         # ESLint
npm run test         # Vitest headless
npm run test:watch   # Vitest watch mode
```

The frontend talks to the engine at `http://localhost:8000` by
default. CORS must allow `http://localhost:3000` (it does, by
default).

## Strategies

Strategies are loaded from `strategies/<name>/`. Each strategy is a
directory containing:

- `strategy.manifest.yaml` — declarative metadata (resources,
  network whitelist, config schema).
- `strategy.py` — must export a class named `Strategy`.

Reference strategies live under `strategies/examples/` and
`strategies/mean_reversion_basic/`. See
[`../PLUGIN_DEV_GUIDE.md`](../PLUGIN_DEV_GUIDE.md) for the full
author guide.

## Common gotchas

- **`docker compose up` without env vars fails.** `docker-compose.yml`
  requires `NEXUS_POSTGRES_USER`, `NEXUS_POSTGRES_PASSWORD`,
  `NEXUS_POSTGRES_DB`. Copy `.env.example` to `.env`.
- **`make test` hangs.** pytest's `asyncio_default_fixture_loop_scope
  = "session"` (see `pyproject.toml:144`) means a single event loop
  serves all tests; if a fixture holds a connection open, the suite
  blocks. Run the offending test alone with `-v` to find it.
- **`alembic upgrade head` errors on fresh DB.** Make sure the
  container is healthy (`docker compose ps`) and the URL in
  `NEXUS_DATABASE_URL` matches.
- **OIDC discovery URL is unreachable.** The provider will log a
  warning and skip registration; the engine still starts, but the
  provider is missing from `/api/v1/auth/{provider}/authorize`.
- **Sandbox blocks an import your strategy needs.** Add it to the
  manifest's `dependencies` first; if it is fundamentally unsafe
  (subprocess, raw sockets), the answer is no — run the strategy in
  its own container and call the engine via API.

## Debugging

- **PDB.** Drop `import pdb; pdb.set_trace()` (works inside async
  code; for `asyncio`-aware stepping use `aiomonitor`).
- **structlog context.** `structlog.get_context(bindvars)` shows the
  current context locals for a request.
- **Slow query log.** Set `NEXUS_LOG_LEVEL=DEBUG` and look for
  `sqlalchemy.engine.Ticket` log lines (turns on statement logging).
- **WebSocket frames.** `websocat ws://localhost:8000/api/v1/ws` is
  the fastest way to drive the WS protocol by hand.

## Where to ask

- Architecture / "why" questions: this doc set + ADRs.
- "How do I…?" questions: `docs/PLUGIN_DEV_GUIDE.md` (strategies) or
  the in-source docstrings.
- Operational questions: `docs/operations/runbooks.md`.
- Found a security issue: see `SECURITY.md` (private disclosure).
