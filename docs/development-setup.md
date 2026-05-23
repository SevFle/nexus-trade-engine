# Development Setup

## Prerequisites

- **Python 3.11+** (3.12 recommended — matches Docker/CI)
- **[uv](https://github.com/astral-sh/uv)** package manager
- **Docker & Docker Compose** — for PostgreSQL + Valkey
- **Node.js 20+** — for the React frontend
- **Git**

## Quick Start (Native)

```bash
# 1. Clone and enter the repo
git clone https://github.com/your-org/nexus-trade-engine.git
cd nexus-trade-engine

# 2. Copy environment config
cp .env.example .env
# Edit .env — at minimum set NEXUS_SECRET_KEY and POSTGRES_PASSWORD

# 3. Start infrastructure (Postgres/TimescaleDB + Valkey)
docker compose up -d db valkey

# 4. Install Python dependencies
uv sync --all-extras

# 5. Run database migrations
.venv/bin/alembic upgrade head

# 6. (Optional) Seed sample market data
.venv/bin/python scripts/seed_data.py

# 7. Start the engine
.venv/bin/uvicorn engine.app:create_app --factory --host 0.0.0.0 --port 8000
```

In a separate terminal:

```bash
cd frontend
npm install
npm run dev   # http://localhost:5173
```

## Quick Start (Docker)

```bash
cp .env.example .env
# Edit .env — set POSTGRES_PASSWORD and NEXUS_SECRET_KEY

make docker-dev          # starts full stack with hot-reload
make docker-dev-logs     # tail logs in another terminal
make docker-dev-down     # tear down (preserves DB volume)
```

| Service | Port | Purpose |
|---------|------|---------|
| `db` | 5432 | TimescaleDB / Postgres 16 |
| `valkey` | 6379 | Cache + event bus |
| `app` | 8000 | FastAPI engine (hot-reload) |
| `worker` | — | TaskIQ worker (hot-reload) |
| `frontend` | 5173 | Vite dev server with HMR |

### When Rebuilds Are Needed

- `pyproject.toml` or `uv.lock` changed → `make docker-dev-rebuild`
- `frontend/package.json` changed → delete `frontend-node-modules` volume, re-run
- `Dockerfile.dev` changed → `make docker-dev-build`

## Environment Variables

All configuration is read from environment variables with the `NEXUS_` prefix
(via `pydantic-settings`). See `engine/config.py` for the full list.

### Required

| Variable | Description |
|----------|-------------|
| `POSTGRES_PASSWORD` | Database password (required by docker-compose) |
| `NEXUS_SECRET_KEY` | JWT signing key — generate with `openssl rand -hex 32` |

### Database

| Variable | Default | Description |
|----------|---------|-------------|
| `NEXUS_DATABASE_URL` | `postgresql+asyncpg://nexus:nexus@localhost:5432/nexus` | Async connection string |
| `NEXUS_DATABASE_POOL_SIZE` | 5 | Connection pool size |
| `NEXUS_DATABASE_MAX_OVERFLOW` | 10 | Pool overflow |

### Cache & Queue

| Variable | Default | Description |
|----------|---------|-------------|
| `NEXUS_VALKEY_URL` | `valkey://localhost:6379/0` | Valkey/Redis URL |

### Auth

| Variable | Default | Description |
|----------|---------|-------------|
| `NEXUS_AUTH_PROVIDERS` | `local` | Comma-separated: local, google, github, oidc, ldap |
| `NEXUS_JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | 60 | Access token TTL |
| `NEXUS_JWT_REFRESH_TOKEN_EXPIRE_DAYS` | 7 | Refresh token TTL |
| `NEXUS_AUTH_LOCAL_ALLOW_REGISTRATION` | true | Enable email/password signup |
| `NEXUS_MFA_ENCRYPTION_KEY` | (empty) | Fernet key for TOTP secret encryption |

### OAuth (per provider)

| Variable | Description |
|----------|-------------|
| `NEXUS_GOOGLE_CLIENT_ID` | Google OAuth2 client ID |
| `NEXUS_GOOGLE_CLIENT_SECRET` | Google OAuth2 client secret |
| `NEXUS_GOOGLE_REDIRECT_URI` | Google OAuth2 redirect URI |
| `NEXUS_GITHUB_CLIENT_ID` | GitHub OAuth client ID |
| `NEXUS_GITHUB_CLIENT_SECRET` | GitHub OAuth client secret |
| `NEXUS_OIDC_DISCOVERY_URL` | OIDC provider discovery URL |
| `NEXUS_OIDC_CLIENT_ID` | OIDC client ID |
| `NEXUS_LDAP_SERVER_URL` | LDAP server URL |

### Trading & Cost Model

| Variable | Default | Description |
|----------|---------|-------------|
| `NEXUS_DEFAULT_EXECUTION_MODE` | `paper` | backtest, paper, or live |
| `NEXUS_DEFAULT_COMMISSION_PER_TRADE` | 0.0 | Commission per trade |
| `NEXUS_DEFAULT_SPREAD_BPS` | 5.0 | Default spread in basis points |
| `NEXUS_DEFAULT_SLIPPAGE_BPS` | 10.0 | Default slippage in basis points |
| `NEXUS_SHORT_TERM_TAX_RATE` | 0.37 | Short-term capital gains rate |
| `NEXUS_LONG_TERM_TAX_RATE` | 0.20 | Long-term capital gains rate |

### Data Providers

| Variable | Default | Description |
|----------|---------|-------------|
| `NEXUS_DATA_PROVIDERS_CONFIG` | (empty) | Path to YAML config for providers |
| `NEXUS_MARKET_DATA_PROVIDER` | `yahoo` | Default provider slug |

### Observability

| Variable | Default | Description |
|----------|---------|-------------|
| `NEXUS_LOG_LEVEL` | `INFO` | Log level |
| `NEXUS_LOG_FORMAT` | `console` | `console` (dev) or `json` (prod) |
| `NEXUS_OTLP_ENDPOINT` | (empty) | OpenTelemetry collector endpoint |
| `NEXUS_SENTRY_DSN` | (empty) | Sentry error tracking DSN |

### Plugins

| Variable | Default | Description |
|----------|---------|-------------|
| `NEXUS_PLUGIN_DIR` | `./strategies` | Strategy discovery directory |
| `NEXUS_PLUGIN_SANDBOX_ENABLED` | `true` | Enable 5-layer security sandbox |

## Running Tests

```bash
# Full test suite with coverage (80% gate)
make test

# Run a specific test file
uv run pytest tests/test_portfolio.py

# Run with verbose output
uv run pytest -v tests/test_order_manager.py

# Skip slow tests
uv run pytest -m "not slow"

# Run only integration tests
uv run pytest -m integration

# Generate HTML coverage report
uv run pytest --cov --cov-report=html
# Open htmlcov/index.html
```

Tests use an in-process test client with DB/Valkey mocks. No Docker
infrastructure required for unit tests.

## Database Migrations

```bash
make migrate                                    # run pending migrations
make migrate-new msg="add foo column to bar"    # autogenerate a revision
```

Migrations live in `engine/db/migrations/versions/`. Sequential numbering
(`001`, `002`, ...). Always include `upgrade()` and `downgrade()`.

## Linting & Type Checking

```bash
make lint        # ruff check + format check
make fix         # auto-fix lint issues + format
make typecheck   # basedpyright
```

CI runs all three. Fix before pushing.

## Installing the Strategy SDK

```bash
# From PyPI (when published)
pip install nexus-trade-sdk

# From source (for development)
cd sdk && pip install -e .
```

## Makefile Reference

| Target | Command |
|--------|---------|
| `make dev` | Start engine with hot-reload |
| `make test` | Run tests with coverage gate |
| `make lint` | Check linting + formatting |
| `make fix` | Auto-fix lint issues |
| `make typecheck` | Run type checker |
| `make migrate` | Run pending migrations |
| `make migrate-new msg="..."` | Create new migration |
| `make docker-up` | Start production stack |
| `make docker-down` | Stop production stack |
| `make docker-dev` | Start dev stack (hot-reload) |
| `make docker-dev-down` | Stop dev stack |
| `make docker-dev-logs` | Tail dev stack logs |
| `make docker-dev-rebuild` | Rebuild dev images from scratch |
