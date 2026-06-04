# Deployment

This document covers the operational concerns of running Nexus
Trade Engine outside a developer laptop: infrastructure
requirements, environment configuration, rollout process, and
roll-back. For local development, see
[development.md](development.md).

## Infrastructure requirements

### Mandatory

| Component | Purpose | Minimum | Tested with |
|---|---|---|---|
| **PostgreSQL 16** | Primary data store | 4 vCPU, 8 GB RAM | TimescaleDB `latest-pg16` image |
| **Valkey 8** (Redis fork) | TaskIQ broker, rate-limit cache, future pub/sub | 1 vCPU, 1 GB RAM | `valkey/valkey:8-alpine` |
| **Engine container** | FastAPI app | 1 vCPU, 1 GB RAM | `python:3.12-slim`-based, see [`Dockerfile`](../Dockerfile) |
| **Worker container** | TaskIQ workers | 1 vCPU, 1 GB RAM | Same image as engine, different entrypoint |

### Recommended

| Component | Purpose |
|---|---|
| **TimescaleDB extension** | Hypertable compression on `ohlcv_bars`. Engine runs on vanilla Postgres but pays the storage cost. |
| **Reverse proxy** (nginx / Caddy / Cloud LB) | TLS termination, request body limits, static asset cache, `/metrics` ACL. |
| **OpenTelemetry collector** | Receives OTLP traces from the engine. Optional — engine falls back to console tracing if `NEXUS_OTLP_ENDPOINT` is empty. |
| **Sentry** | Error tracking. Optional — `NEXUS_SENTRY_DSN` empty disables. |
| **Prometheus** | Scrapes `/metrics`. Optional but the alert rules in `observability/prometheus/` are written against it. |

### Hard limits baked into the engine

- Request body capped at **1 MiB** by `BodySizeLimitMiddleware`.
- Per-IP rate limit: **600 req/min, 60 burst** (overrides exist
  for `/api/v1/client/errors`: 30 req/min).
- Backtest results kept in-memory **1 hour** (TTL sweep on each
  poll).
- Refresh tokens: 7-day TTL (`NEXUS_JWT_REFRESH_TOKEN_EXPIRE_DAYS`).
- Access tokens: 1-hour TTL (`NEXUS_JWT_ACCESS_TOKEN_EXPIRE_MINUTES`).

## Environment variables

The full set lives in
[`engine/config.py`](../engine/config.py). The minimum that must
be set for a production deploy:

| Variable | Why it matters |
|---|---|
| `NEXUS_APP_ENV=production` | Flips a handful of safe-for-dev defaults (Secure cookies, strict CORS, etc.). |
| `NEXUS_SECRET_KEY` | **Required.** HMAC key for JWTs. Generate with `openssl rand -hex 32`. |
| `NEXUS_SECRET_KEY_PREVIOUS` | Optional dual-key window for key rotation. |
| `NEXUS_MFA_ENCRYPTION_KEY` | Fernet key (`openssl rand -base64 32`). Required to enable MFA enrollment; empty disables. |
| `NEXUS_DATABASE_URL` | `postgresql+asyncpg://user:pass@host:5432/db`. |
| `NEXUS_VALKEY_URL` | `valkey://host:6379/0`. Worker uses `redis://` derived from this. |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | Used by `docker-compose.yml` to provision the DB container. |
| `NEXUS_AUTH_PROVIDERS` | Comma-separated list of enabled providers: `local`, `google`, `github`, `oidc`, `ldap`. Default `local`. |
| `NEXUS_CORS_ORIGINS` | JSON array of allowed origins (default `["http://localhost:3000"]`). |

Provider-specific variables (`NEXUS_GOOGLE_CLIENT_ID`,
`NEXUS_OIDC_DISCOVERY_URL`, `NEXUS_LDAP_SERVER_URL`, ...) are
no-ops unless the corresponding provider is in
`NEXUS_AUTH_PROVIDERS`.

`.env.example` is the canonical reference; copy it to `.env` and
fill in the required values.

## Container topology

```mermaid
flowchart TB
  LB[Reverse proxy<br/>TLS termination]

  subgraph Engine
    APP[app container<br/>uvicorn :8000]
    WORKER[worker container<br/>taskiq worker]
  end

  PG[(Postgres + TimescaleDB)]
  VAL[(Valkey)]

  LB --> APP
  APP --> PG
  APP --> VAL
  WORKER --> PG
  WORKER --> VAL
```

Both containers are built from the same image (see
[`Dockerfile`](../Dockerfile)):

- `app` runs `uvicorn engine.app:create_app --factory --host
  0.0.0.0 --port 8000`.
- `worker` runs `taskiq worker engine.tasks.worker:broker`.

They share environment (`.env` mounted or env vars injected) and
both connect to the same Postgres + Valkey instances. Scale them
independently — the worker is the bottleneck for long backtests,
the app is the bottleneck for interactive API traffic.

## Database preparation

1. **Create the database** (compose does this for you via
   `POSTGRES_*` env vars):
   ```sql
   CREATE USER nexus WITH PASSWORD '<random>';
   CREATE DATABASE nexus OWNER nexus;
   ```
2. **Install TimescaleDB extension** (optional but recommended):
   ```sql
   CREATE EXTENSION IF NOT EXISTS timescaledb;
   ```
3. **Run migrations**:
   ```bash
   docker compose run --rm app alembic upgrade head
   ```
   Migrations are sequential (`001_*` through `012_*`). The chain
   is forward-only in production — `downgrade()` exists for every
   revision but operators are encouraged to roll forward via a
   new migration rather than revert.
4. **Seed reference data** (optional):
   ```bash
   docker compose run --rm app python scripts/seed_data.py
   ```

## Rollout process

The engine follows a standard blue/green or rolling deploy. The
shape that works in compose today (single replica) is the
minimum; the engine is horizontally scalable for stateless
traffic (API), but the worker and the scheduler need a bit more
care (see [known-limitations.md](known-limitations.md)).

### Pre-flight

1. **Backup the database**. See
   [operations/backup-and-recovery.md](operations/backup-and-recovery.md).
2. **Run the test suite** against the candidate image:
   ```bash
   docker compose -f docker-compose.dev.yml run --rm app pytest
   ```
3. **Dry-run pending migrations**:
   ```bash
   alembic upgrade head --sql
   ```
   Inspect the SQL. Migrations should never require downtime; if
   one does, split it into additive + cutover steps.

### Deploy

1. **Pull the new image**:
   ```bash
   docker compose pull
   ```
2. **Apply migrations** before rolling the app/worker containers
   (so the new code sees the new schema):
   ```bash
   docker compose run --rm app alembic upgrade head
   ```
3. **Roll the containers**:
   ```bash
   docker compose up -d --no-deps app worker
   ```
4. **Smoke-test the new version**:
   ```bash
   curl -fsS http://localhost:8000/health
   curl -fsS http://localhost:8000/ready
   curl -fsS http://localhost:8000/health/providers
   ```

### Roll-back

1. **Roll the containers** back to the previous image:
   ```bash
   docker compose up -d --no-deps app worker  # previous tag
   ```
2. **Migrations** are **not** auto-rolled back. If a migration
   introduced a breaking schema change, write a forward-fix
   migration that restores compatibility with the old code path.
   Reverting a migration by running `alembic downgrade -1` is
   supported but is a last resort — the data loss path is
   irreversible.

## Scaling

| Component | Horizontal? | Notes |
|---|---|---|
| `app` (API) | Yes | Stateless. Put behind a load balancer; sessions are JWT-based so no sticky-session requirement. |
| `worker` | Yes | TaskIQ workers compete for work on the Valkey list queue. |
| `scheduler` | **No** | TaskIQ's scheduler runs in-process. Multi-replica deploys must designate a single scheduler replica or trigger jobs externally. See [known-limitations.md](known-limitations.md). |
| Postgres | Vertical only | Read replicas are fine for analytics. Writes are single-node by design. |
| Valkey | Single-node for dev | Sentinel/Cluster supported by the client lib; not currently wired. |

## Health and readiness

`/health` is the liveness probe — always returns 200 if the
process is up. Use it for "should we restart this container".

`/ready` is the readiness probe — checks DB and Valkey. Use it
for "should the load balancer route traffic here".

`/health/providers` is operational — surfaces the per-provider
health of every market-data adapter. Wire it into your dashboard.

## Observability

- **Logs**: structured JSON when `NEXUS_LOG_FORMAT=json` (the
  production default), or `console` for human reading. All logs
  carry the correlation id from `CorrelationIdMiddleware`.
- **Traces**: OTLP via `NEXUS_OTLP_ENDPOINT`. Empty disables.
- **Metrics**: Prometheus format at `/metrics`. The active
  `MetricsBackend` is set by the lifespan hook in
  [`engine/app.py`](../engine/app.py); swap to OpenTelemetry /
  StatsD by reassigning `set_metrics()` after `create_app()`
  returns.
- **Errors**: Sentry via `NEXUS_SENTRY_DSN`. Empty disables.

See [operations/slos.md](operations/slos.md) for the SLOs and
alert rules.

## Security checklist

Before exposing the engine to anything beyond localhost:

- [ ] `NEXUS_SECRET_KEY` set (32+ random bytes).
- [ ] `NEXUS_MFA_ENCRYPTION_KEY` set if MFA is enabled.
- [ ] `NEXUS_CORS_ORIGINS` set to the actual frontend origin(s)
      only. Wildcard is unacceptable in production.
- [ ] Reverse proxy terminates TLS, sets HSTS, sets
      `X-Forwarded-Proto`.
- [ ] Postgres and Valkey bound to private network only — the
      `127.0.0.1:` port mapping in `docker-compose.yml` is the
      example; production should remove the publish entirely.
- [ ] `/metrics` ACL'd at the network edge (or behind
      auth-gated reverse proxy).
- [ ] Refresh-token rotation is on by default — verify by
      hitting `POST /api/v1/auth/refresh` twice with the same
      token and confirming the second call returns 401 with
      `Token reuse detected`.
- [ ] `NEXUS_RATE_LIMIT_*` tuned for your traffic. Defaults are
      generous; tighten if you see abusive clients.
- [ ] Legal documents (Terms, Privacy, EULA, Risk) reviewed by
      counsel and the versions bumped in `legal/`. The engine
      gates trading routes on acceptance, so the legal review
      must happen before open beta.

## Secrets management

The engine reads secrets from environment variables only — there
is no Vault / SSM / Secrets Manager integration. Operators wire
their preferred secret store into the container runtime:

- **Docker Compose**: `.env` file with `chmod 600`.
- **Kubernetes**: secrets mounted as env vars.
- **Nomad / ECS / systemd**: native secret env-var injection.

Rotation:

- `NEXUS_SECRET_KEY` — set `NEXUS_SECRET_KEY_PREVIOUS` to the
  current value, then roll `NEXUS_SECRET_KEY` to the new value.
  Both keys validate during the overlap window; revert by
  clearing `..._PREVIOUS` once all old tokens have expired.
- `NEXUS_MFA_ENCRYPTION_KEY` — currently single-key. Multi-key
  support is on the roadmap.

## Common pitfalls

- **Forgot to run migrations** — the new code expects columns
  the old schema doesn't have. Symptom: 500s on first request
  with stacktrace pointing to a missing column.
- **Worker running old image** — the app enqueues tasks the
  worker can't deserialize. Symptom: tasks permanently pending.
  Always roll `app` and `worker` together.
- **CORS misconfiguration** — `NEXUS_CORS_ORIGINS` is a JSON
  array, not a comma-separated string. Wrong: `https://a,https://b`.
  Right: `["https://a","https://b"]`.
- **`NEXUS_SECRET_KEY` left empty** — the engine refuses to
  start in non-test mode when the key is missing. The error
  message is explicit; don't paper over it.
