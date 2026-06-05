# Deployment

How to deploy Nexus Trade Engine to a single host. This is the
shape we run; adjust to your environment. For local development
see [development setup](../development.md).

## Reference topology

```
                ┌────────────────────────┐
                │  Reverse proxy / TLS   │
                │  (nginx / Caddy / ALB) │
                └──────────┬─────────────┘
                           │
        ┌──────────────────┼──────────────────┐
        ▼                  ▼                  ▼
┌──────────────┐  ┌──────────────┐   ┌──────────────┐
│ app (x N)    │  │ worker (x N) │   │ frontend     │
│ uvicorn      │  │ taskiq worker│   │ vite preview │
│ :8000        │  │              │   │              │
└──────┬───────┘  └──────┬───────┘   └──────────────┘
       │                 │
       └────┬────────────┘
            ▼
   ┌──────────────────┐
   │ Valkey 8         │
   │ (sentinel x 3)   │
   └──────────────────┘
            │
            ▼
   ┌────────────────────────┐
   │ PostgreSQL 16 +        │
   │ TimescaleDB extension  │
   │ (primary + replica)    │
   └────────────────────────┘
```

For a small deployment (single user, single host) run one of each
and front them with the same nginx. For larger deployments scale
`app` and `worker` horizontally; Valkey and Postgres stay single-
primary.

## Required infrastructure

| Component         | Minimum                                              | Production                            |
|-------------------|------------------------------------------------------|---------------------------------------|
| CPU               | 2 cores                                              | 4+ for `app`, 8+ per `worker`         |
| RAM               | 4 GB                                                 | 8+ GB                                 |
| Disk              | 20 GB SSD                                            | 100 GB+ (TimescaleDB hypertables grow)|
| Postgres          | 16                                                   | 16 + TimescaleDB extension            |
| Valkey / Redis    | 8 (or Redis 7+ fork-compatible)                      | Sentinel x 3 for HA                   |
| Object storage    | —                                                    | S3 / GCS / MinIO (backups)            |
| Network egress    | To upstream market-data providers + outbound webhooks | Same, plus an IdP endpoint if SSO     |

## Environment variables

The full set is documented in
[`engine/config.py`](../../engine/config.py) and mirrored in
[`.env.example`](../../.env.example). The values **required** for
a deploy are below. Everything else has a safe default.

### Required

| Variable                      | What                                              |
|-------------------------------|---------------------------------------------------|
| `POSTGRES_USER`               | DB user (compose only).                           |
| `POSTGRES_PASSWORD`           | DB password (compose only).                       |
| `POSTGRES_DB`                 | DB name (compose only).                           |
| `NEXUS_DATABASE_URL`          | `postgresql+asyncpg://user:pw@host:5432/db`.      |
| `NEXUS_VALKEY_URL`            | `valkey://host:6379/0`.                           |
| `NEXUS_SECRET_KEY`            | 32-byte url-safe hex. Used for JWT HS256. **Required to start in non-test env.** |
| `NEXUS_APP_ENV`               | `production` for prod. Controls secure-cookie, HSTS, etc. |

### Strongly recommended

| Variable                      | What                                              |
|-------------------------------|---------------------------------------------------|
| `NEXUS_MFA_ENCRYPTION_KEY`    | Fernet key for TOTP secrets. Empty disables MFA enrollment. |
| `NEXUS_SENTRY_DSN`            | Sentry ingest URL. Empty disables Sentry.         |
| `NEXUS_OTLP_ENDPOINT`         | OTLP/gRPC or HTTP trace exporter URL.             |
| `NEXUS_CORS_ORIGINS`          | JSON list of allowed origins. Default is `["http://localhost:3000"]`. |
| `NEXUS_DATA_PROVIDERS_CONFIG` | Path to a providers YAML (Yahoo/Polygon/Alpaca/…). |
| `NEXUS_OPERATOR_NAME` / `_EMAIL` / `_URL` | Used in legal-doc substitution.       |
| `NEXUS_JURISDICTION`          | Used in legal docs and tax defaults.              |

### Tuning knobs

| Variable                          | Default   | When to bump                              |
|-----------------------------------|-----------|-------------------------------------------|
| `NEXUS_DATABASE_POOL_SIZE`        | 5         | High-concurrency `app`. Watch Postgres `max_connections`. |
| `NEXUS_DATABASE_MAX_OVERFLOW`     | 10        | Same.                                     |
| `NEXUS_WORKER_CONCURRENCY`        | 4         | CPU-bound backtests. 1× cores is a baseline. |
| `NEXUS_RATE_LIMIT_PER_MINUTE`     | 600       | Behind a trusted proxy.                   |
| `NEXUS_RATE_LIMIT_BURST`          | 60        | Same.                                     |

See [`engine/config.py`](../../engine/config.py) for the rest.

## First-time deploy

```bash
# 1. Provision a host with Docker + Docker Compose.
docker --version && docker compose version

# 2. Clone + cd
git clone https://github.com/your-org/nexus-trade-engine.git
cd nexus-trade-engine

# 3. Generate secrets and write .env
cp .env.example .env
openssl rand -hex 32 | awk '{print "NEXUS_SECRET_KEY="$1}' >> .env
openssl rand -base64 32 | awk '{print "NEXUS_MFA_ENCRYPTION_KEY="$1}' >> .env
# (fill in POSTGRES_*, NEXUS_DATABASE_URL, NEXUS_VALKEY_URL, NEXUS_APP_ENV)

# 4. Pull + build
docker compose build

# 5. Migrate
docker compose run --rm app alembic upgrade head

# 6. Bring up
docker compose up -d

# 7. Smoke test
curl -s http://127.0.0.1:8000/health        # {"status":"ok"}
curl -s http://127.0.0.1:8000/ready         # {"status":"ok","db":"ok","valkey":"ok"}
```

## Rollout process (for routine updates)

The default rollout is **zero-downtime by virtue of in-place container
restarts behind a load balancer**. The application is stateless (all
state in Postgres + Valkey), so draining + replacing one container at
a time is safe.

1. **Pre-flight:** `make test && make lint && make typecheck` on the
   branch. CI must be green.
2. **Build the new image:** tag the commit, build
   `ghcr.io/your-org/nexus-engine:$SHA`, push. Frontend image is
   built separately.
3. **Run migrations** against the primary Postgres. Always run
   migrations **before** rolling the app containers — the new code
   may reference new columns. Downward-compatible migrations are
   required for zero-downtime; see
   [database.md → migration policy](../architecture/database.md#migration-policy).
4. **Roll app containers one at a time:** `docker compose up -d
   --no-deps app` (or your orchestrator equivalent). Each container
   passes `/ready` before the next is restarted.
5. **Roll workers.** Workers are stateless too but interrupt
   in-flight tasks; restart during low-traffic windows.
6. **Smoke-test post-rollout:**
   - `curl https://your.host/ready` → `status: ok`.
   - Submit a small backtest via the SDK; verify it completes.
   - Send a webhook test ping; verify the delivery arrives.
7. **Watch for 15 minutes.** SLO dashboards, Sentry, error rate.

## Rollback

- **Code rollback:** re-deploy the previous image. The new migration
  may have already run; downward-compatible migrations mean the old
  code still works against the new schema.
- **Schema rollback:** run `alembic downgrade -1` against the
  primary. Only safe if the migration author wrote a real
  `downgrade()`; destructive migrations are documented in their
  file header. See [database.md](../architecture/database.md#migration-policy).
- **PITR** for catastrophic cases — see
  [backup-and-recovery.md](backup-and-recovery.md).

## Health-check & monitoring wiring

| Probe               | Wire to                                                |
|---------------------|--------------------------------------------------------|
| `/health`           | Container liveness. Restart on N consecutive failures. |
| `/ready`            | Ingress / load balancer routing.                       |
| `/metrics`          | Prometheus scrape, 30 s interval.                      |
| `/health/providers` | Status-page checker.                                   |

Wire Sentry + OpenTelemetry by setting `NEXUS_SENTRY_DSN` and
`NEXUS_OTLP_ENDPOINT`. Both are no-ops if unset.

## Capacity planning back-of-envelope

- One `app` container handles ~200 req/s at 50 ms p99 against warm
  caches on a 2-core / 4 GB host. Scale linearly with CPU.
- One `worker` container runs 1-2 simultaneous backtests per core,
  depending on strategy complexity and the data universe size.
- Postgres storage: 1 year of daily OHLCV for 10 000 symbols is
  ~10 GB raw, ~3 GB with TimescaleDB compression.
- Valkey memory: 100 MB for the broker + cache at modest load.
  Bump if you cache large bar ranges for many users.

## Operating the front-end dashboard

The React dashboard is a separate Vite build. We ship a
`frontend/Dockerfile` that runs `vite build` and serves the
output via nginx. Wire it behind the same reverse proxy on
`/`, with the engine on `/api/...`.

For self-hosted PWA usage (mobile install), the manifest is
already wired — see [ADR-0003](../adr/0003-mobile-app-strategy.md).
