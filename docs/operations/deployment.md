# Deployment

How to run Nexus Trade Engine outside a laptop. Covers the container
image, the compose stack, the environment variables an operator must
set, the rollout process, and the things you have to bring yourself
(reverse proxy, log shipper, secrets vault).

This document is the **operator** view. For local dev see
[`development.md`](../development.md). For SLOs and on-call see
[`slos.md`](slos.md) and [`runbooks/`](runbooks/).

## Container image

[`Dockerfile`](../../Dockerfile) is multi-stage:

1. **Builder** — `ghcr.io/astral-sh/uv:0.6-python3.12-bookworm-slim`.
   Installs dependencies with `uv sync --frozen --no-dev` (uses the
   committed `uv.lock` for reproducibility), copies the application
   source, re-syncs to ensure the lock matches.
2. **Runtime** — `gcr.io/distroless/python3-debian12:nonroot`. Copies
   `/app` from the builder. `EXPOSE 8000`. Entrypoint:
   `uvicorn engine.app:create_app --factory --host 0.0.0.0 --port 8000`.

The distroless base has no shell. `kubectl exec ... -- /bin/sh` will
fail — use ephemeral debug containers if you need a shell.

Same image runs the TaskIQ worker (`docker-compose.yml` overrides
the entrypoint to `python -m taskiq worker engine.tasks.worker:broker`).

### Build

```bash
make docker-build                  # uses Dockerfile directly
docker compose build               # uses docker-compose.yml
```

There is no separate prod compose; the same `docker-compose.yml`
runs in dev (with bind mounts via `docker-compose.dev.yml`) and prod
(with built images).

## Stack

[`docker-compose.yml`](../../docker-compose.yml) defines four
services. All ports are bound to `127.0.0.1` — production traffic
comes through a reverse proxy, not directly from the public internet.

| Service | Image | Ports | Notes |
|---|---|---|---|
| `db` | `timescale/timescaledb:latest-pg16` | `127.0.0.1:5432` | Persistent volume `pgdata`. Healthcheck: `pg_isready`. |
| `valkey` | `valkey/valkey:8-alpine` | `127.0.0.1:6379` | Healthcheck: `valkey-cli ping`. Used by TaskIQ broker, event bus, rate limiter. |
| `app` | Builds from `Dockerfile` | `127.0.0.1:8000` | Env from `.env`. `depends_on: { db, valkey }` with `condition: service_healthy`. |
| `worker` | Same image | — | Entry point: `taskiq worker engine.tasks.worker:broker`. |

Compose requires `POSTGRES_PASSWORD` to be set via env (security fix
`5a065a4`). The dev compose (`docker-compose.dev.yml`) adds:

- Bind mounts for `engine/`, `frontend/`, `legal/`, etc.
- `uvicorn --reload` and `taskiq ... --reload` flags.
- A `frontend` service running Vite on `127.0.0.1:5173`.
- `WATCHFILES_FORCE_POLLING=true` / `CHOKIDAR_USEPOLLING=true` for
  cross-platform hot-reload.

## Required environment variables

`engine/config.py` defines all settings under the `NEXUS_` prefix
(see [`engine/config.py`](../../engine/config.py)). The minimum set
to run in production:

| Variable | Default | Why you must change it |
|---|---|---|
| `NEXUS_SECRET_KEY` | `""` | Engine refuses to start without it outside test env (`engine/app.py:131-133`). Use a 32+ byte random string. |
| `NEXUS_DATABASE_URL` | `postgresql+asyncpg://nexus:nexus@localhost:5432/nexus` | Production DB DSN. Must include credentials. |
| `NEXUS_VALKEY_URL` | `valkey://localhost:6379/0` | TaskIQ broker, rate limit, event bus. |
| `NEXUS_APP_ENV` | `development` | Set to `production` to enable JSON logs, disable debug, tighten allowed CORS. |
| `NEXUS_MFA_ENCRYPTION_KEY` | `""` | Fernet key (url-safe base64, 32 bytes). Empty disables MFA enrollment. |
| `POSTGRES_PASSWORD` | (none) | Required by docker-compose for the `db` service. |
| `NEXUS_AUTH_PROVIDERS` | `local` | Comma-separated. Add `google,github,oidc,ldap` as needed; each has its own required vars. |

### Recommended production knobs

| Variable | Default | Suggested |
|---|---|---|
| `NEXUS_RATE_LIMIT_PER_MINUTE` | 600 | Tune by traffic. Per-IP at the proxy. |
| `NEXUS_JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | 60 | Lower (15-30) if you have a refresh flow in the UI. |
| `NEXUS_LOG_FORMAT` | `console` | `json` in production. |
| `NEXUS_LOG_SINK` | `stdout` | `file` or `otlp` if you have a collector. |
| `NEXUS_OTLP_ENDPOINT` | `""` | URL of OTel collector if you want traces. |
| `NEXUS_SENTRY_DSN` | `""` | Sentry project DSN. |
| `NEXUS_CORS_ORIGINS` | `["http://localhost:3000"]` | Your frontend origin(s). |

### Auth-provider variables (only if you enable them)

Google: `NEXUS_GOOGLE_CLIENT_ID`, `NEXUS_GOOGLE_CLIENT_SECRET`,
`NEXUS_GOOGLE_REDIRECT_URI`.

GitHub: `NEXUS_GITHUB_CLIENT_ID`, `NEXUS_GITHUB_CLIENT_SECRET`,
`NEXUS_GITHUB_REDIRECT_URI`.

OIDC: `NEXUS_OIDC_DISCOVERY_URL`, `NEXUS_OIDC_CLIENT_ID`,
`NEXUS_OIDC_CLIENT_SECRET`, `NEXUS_OIDC_REDIRECT_URI`,
`NEXUS_OIDC_ROLE_CLAIM` (default `roles`).

LDAP: `NEXUS_LDAP_SERVER_URL`, `NEXUS_LDAP_BIND_DN`,
`NEXUS_LDAP_BIND_PASSWORD`, `NEXUS_LDAP_SEARCH_BASE`,
`NEXUS_LDAP_ROLE_MAPPING` (JSON). Requires the `ldap` extra:
`pip install nexus-trade-engine[ldap]` (`python-ldap`).

## Secrets handling

`.env` is gitignored. The engine reads it via Pydantic-Settings
(`env_file=".env"`). For production:

1. Generate `NEXUS_SECRET_KEY` and `NEXUS_MFA_ENCRYPTION_KEY` once.
   Store in your secrets manager (Vault, AWS Secrets Manager, etc.).
2. Inject into the container at runtime via Docker secrets, k8s
   secrets, or environment variables — never bake into the image.
3. Rotate `NEXUS_SECRET_KEY` by setting both `NEXUS_SECRET_KEY` (new)
   and `NEXUS_SECRET_KEY_PREVIOUS` (old) for the duration of token
   overlap. JWT decode tries both (`engine/api/auth/jwt.py:16-54`).

`NEXUS_AUTH_OVERWRITE_ROLE_ON_LOGIN` defaults to `false`. Setting it
to `true` lets upstream IdPs assign local roles. See decision #12 in
[`architecture/decisions.md`](../architecture/decisions.md) and the
SEV-741 fix in commit `5525d0f`.

## Infrastructure you bring

The engine ships as a self-contained service but assumes:

| Concern | Operator brings |
|---|---|
| Edge termination | Reverse proxy (nginx, Caddy, Traefik). The engine binds 127.0.0.1 only. |
| TLS | At the proxy. The engine speaks HTTP. |
| Static asset serving | The built frontend (`frontend/dist/`) — serve via the same proxy. |
| Log aggregation | Loki / ELK / CloudWatch. Engine emits JSON to stdout when `NEXUS_LOG_FORMAT=json`. |
| Metrics scraping | Prometheus hitting `/metrics`. |
| Trace collection | OTel Collector at `NEXUS_OTLP_ENDPOINT`. |
| Error tracking | Sentry at `NEXUS_SENTRY_DSN`. |
| DB backups | `pg_dump` cron or managed-snapshot. See [`backup-and-recovery.md`](backup-and-recovery.md). |
| Alert routing | Alertmanager or equivalent. See [`slos.md`](slos.md) and `observability/prometheus/slo-rules.yaml`. |

## First-time bootstrap

```bash
# 1. Clone and create .env (minimum required vars)
git clone <repo-url> nexus-trade-engine
cd nexus-trade-engine
cp .env.example .env  # then edit

# 2. Build the image
docker compose build

# 3. Start the database first (so the app can migrate)
docker compose up -d db valkey

# 4. Run migrations
docker compose run --rm app alembic upgrade head

# 5. Start the full stack
docker compose up -d
```

The engine's lifespan hook
([`engine/app.py:122-151`](../../engine/app.py)) will:
- Reject startup if `NEXUS_SECRET_KEY` is missing (production only).
- Connect to Valkey.
- Build the auth registry from `NEXUS_AUTH_PROVIDERS`.
- Bootstrap data providers (Yahoo default, or from
  `NEXUS_DATA_PROVIDERS_CONFIG` YAML).
- Seed the in-memory reference index (~340 instruments).
- Sync legal documents from `NEXUS_LEGAL_DOCUMENTS_DIR` into the DB.

## Rollout process

There is no built-in blue-green or canary. A typical rollout:

1. **Pull the new image**:
   ```bash
   docker compose pull
   ```
2. **Check for pending migrations** — read the release notes (every
   release that includes a migration calls it out in
   [`CHANGELOG.md`](../../CHANGELOG.md)).
3. **Apply migrations on the existing app before rolling**. The
   engine tolerates a migration that's one version ahead of the code
   (we add columns before code reads them), so this is safe:
   ```bash
   docker compose run --rm app alembic upgrade head
   ```
4. **Restart the app and worker**:
   ```bash
   docker compose up -d app worker
   ```
   Compose stops the old container only after the new one is healthy
   (because of `depends_on: service_healthy`). For true zero-downtime,
   run two replicas behind the proxy.
5. **Watch the dashboards** for ~10 minutes. The SLO burn-rate alerts
   page fast (5 min) if something is wrong.

### Rollback

Migrations should have a working `downgrade()`. If a release has to
be reverted:

1. Roll back the code (`docker compose ... :<previous-tag>`).
2. Run `alembic downgrade -1` for each migration that landed in the
   failed release.
3. Confirm `/ready` is `200 OK`.

If `downgrade()` is data-losing (rare; flagged in the migration
file), restore from the most recent `pg_dump` instead.

## Scaling

The single-process engine can handle ~600 req/min on a modest VM
(default rate limit). To go further:

- **Horizontal scaling of `app`**: run more replicas behind the proxy.
  - The rate limiter is in-process (per-replica), so effective rate
    is `replicas × NEXUS_RATE_LIMIT_PER_MINUTE`. Tighten at the proxy.
  - WebSocket manager is single-process — see known-issues.
  - Webhook dispatcher runs in every replica; you'll get duplicate
    deliveries unless you adopt a Redis-based lock or move dispatch
    to the worker only.
- **Horizontal scaling of `worker`**: run more TaskIQ workers. The
  broker is Valkey/Redis, so multiple workers compete for jobs.
- **Vertical scaling of `db`**: TimescaleDB benefits from more RAM
  for caching. Compression + continuous aggregates let you serve
  analytical queries without expanding the hot set.
- **Read replicas**: not currently used. The async session factory is
  bound to one engine; adding a read-only DSN would require a small
  refactor in `engine/db/session.py`.

## Health and readiness

| Endpoint | Use |
|---|---|
| `GET /health` | Liveness probe — process is up. |
| `GET /ready` | Readiness probe — DB and Valkey reachable. |
| `GET /health/providers` | Per-data-provider status. |
| `GET /metrics` | Prometheus scrape. |

Wire `/health` to the orchestrator's liveness probe and `/ready` to
the readiness probe so the proxy doesn't send traffic to a container
that's still warming up.

## Updating legal documents

Legal documents live in [`/legal`](../../legal) by default. The
engine syncs them at startup via
[`engine/legal/sync.py`](../../engine/legal/sync.py). To roll out a
new Terms version:

1. Update `/legal/terms-of-service.md` (front-matter `version:` and
   `effective_date:`).
2. Restart the engine (or hit a future re-sync endpoint).
3. Once the new row exists in `legal_documents`, the
   `require_legal_acceptance` dependency will start returning
   `451 legal_re_acceptance_required` to users who haven't accepted
   the new version. **Today that dependency is a no-op** — see
   known-issues; the row is still upserted for audit.

## What's intentionally not covered here

- **Multi-tenant hosting.** Not a goal; the engine models one
  operator's data per database.
- **Live trading.** The `LiveBackend` is a stub
  (`engine/core/execution/live.py:55-59`). When it ships, this doc
  will gain a "broker credentials" section.
- **Strategy marketplace hosting.** Marketplace routes return stubs
  today. When the marketplace ships, this doc will gain the
  registry / package-download flow.
