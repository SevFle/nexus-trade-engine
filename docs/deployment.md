# Deployment

> **Audience:** operators running Nexus Trade Engine on their own
> infrastructure. The project does **not** ship a hosted SaaS — every
> deployment is operator-owned.

This doc covers what the engine needs to run, how a release goes from
git tag to a running pod, and the rollout / rollback procedures.
On-call runbooks live in [`operations/runbooks/`](operations/runbooks/);
backup / DR drills live in [`operations/`](operations/).

## 1. Deployment topology

A production deployment is three processes plus two stateful services:

```mermaid
flowchart LR
    LB[Reverse proxy<br/>nginx / Caddy / ALB] --> API[uvicorn<br/>engine.app:create_app]
    LB --> WS[uvicorn<br/>--loop uvloop]
    API <--> DB[(Postgres 16<br/>+ TimescaleDB)]
    API <--> VK[(Valkey 8<br/>Redis-compatible)]
    Worker[taskiq worker<br/>engine.tasks.worker:broker] <--> DB
    Worker <--> VK
    API --> OT[OTel Collector]
    API --> Sentry[(Sentry)]
    Worker --> OT
    Worker --> Sentry
    Frontend[Vite build<br/>static files] -.served by.-> LB
```

- **API:** the FastAPI app. Stateless. Run as N processes (one per
  core) behind a reverse proxy.
- **Worker:** TaskIQ worker for background jobs. Stateless. Run 1..N
  replicas, sized to the broker depth.
- **Frontend:** static files produced by `npm run build`. Serve from
  the reverse proxy directly; do **not** go through the API for assets.
- **Postgres:** TimescaleDB on PG 16. Single primary; read replicas
  are optional but recommended for analytical queries.
- **Valkey:** the cache + TaskIQ broker. AOF persistence on; without
  it, a broker restart drops in-flight tasks.

The reference `docker-compose.yml` ships this exact shape with two
containers (`app` + `worker`) plus `db` and `valkey`. It is suitable
for small deployments. Production deployments should run on Kubernetes
or equivalent with proper backup / monitoring.

## 2. Container image

The Dockerfile is a two-stage build:

1. **Builder:** `ghcr.io/astral-sh/uv:0.6-python3.12-bookworm-slim`,
   resolves `uv.lock`, installs the project.
2. **Runtime:** `gcr.io/distroless/python3-debian12:nonroot`, copies
   the venv + source. No shell, no package manager.

```bash
docker build -t nexus-trade-engine .
docker run --rm -p 8000:8000 nexus-trade-engine
```

The image exposes port 8000 and runs
`uvicorn engine.app:create_app --factory --host 0.0.0.0 --port 8000`.
For the worker, override the entrypoint:

```bash
docker run --rm nexus-trade-engine \
  python -m taskiq worker engine.tasks.worker:broker
```

Multi-arch (amd64 + arm64) images are published to GHCR on every
GitHub release; see `.github/workflows/publish-images.yml`. Pull from
`ghcr.io/<owner>/nexus-trade-engine:<tag>`.

## 3. Required environment variables

These are the **must-set** variables for a production deploy. The full
list lives in [`engine/config.py`](../engine/config.py); sane defaults
are documented there.

| Variable | Why |
|----------|-----|
| `NEXUS_SECRET_KEY` | HMAC key for JWT signing. **Engine startup fails without it** outside the test env (see [`engine/app.py:lifespan`](../engine/app.py)). Generate with `python -c "import secrets; print(secrets.token_urlsafe(64))"`. |
| `NEXUS_MFA_ENCRYPTION_KEY` | Fernet key (url-safe base64, 32 bytes decoded) for MFA TOTP secrets at rest. Empty disables MFA enrollment. Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. |
| `NEXUS_DATABASE_URL` | `postgresql+asyncpg://user:pass@host:5432/db`. Must point at a PG16 + TimescaleDB instance. |
| `NEXUS_VALKEY_URL` | `valkey://host:6379/0` or `redis://...`. The engine uses the `valkey` client lib which speaks both protocols. |
| `NEXUS_APP_ENV` | `production` enables Secure cookies, raises the JWT-refresh-reuse detection. |
| `NEXUS_CORS_ORIGINS` | JSON-array or comma-separated list. **Do not use `*` in production** — the engine echoes the request origin and `*` plus `allow_credentials=True` is invalid per the CORS spec. |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | Required by `docker-compose.yml` (the compose file uses `${VAR:?...}` and refuses to start otherwise). |

Strongly recommended:

| Variable | Effect |
|----------|--------|
| `NEXUS_SENTRY_DSN` | Enables Sentry. Both API and worker forward exceptions. |
| `NEXUS_OTLP_ENDPOINT` | OTLP/gRPC or HTTP endpoint for traces + metrics. Empty = no export. |
| `NEXUS_LOG_FORMAT=json` | Structured logs for log aggregators (Loki / Elastic / Datadog). |
| `NEXUS_LOG_SINK=stdout` | Where structlog writes. `file` requires `NEXUS_LOG_FILE_PATH`. |
| `NEXUS_AUTH_PROVIDERS` | Comma-separated set of `local,google,github,oidc,ldap`. Defaults to `local`. |

## 4. Reverse proxy

The engine binds `0.0.0.0:8000` by default (`NEXUS_APP_HOST` /
`NEXUS_APP_PORT`). Put a TLS-terminating reverse proxy in front. The
compose file binds ports to `127.0.0.1` for defense-in-depth — the
engine should never be the Internet-facing process.

Required forwarded headers:

- `X-Forwarded-For` (used by the rate limiter for client identity —
  see `NEXUS_TRUSTED_PROXY_DEPTH` discussion in
  [`engine/api/rate_limit.py`](../engine/api/rate_limit.py)).
- `X-Forwarded-Proto` (FastAPI uses it for redirect URL synthesis).
- `Host` (standard).

WebSocket upgrade must be supported for `/api/v1/ws`.

## 5. Database setup

1. **Create the database.** TimescaleDB extension must be installed on
   the PG cluster:
   ```sql
   CREATE DATABASE nexus;
   \c nexus
   CREATE EXTENSION IF NOT EXISTS timescaledb;
   ```
2. **Run migrations.** From the engine container or a one-off job:
   ```bash
   alembic upgrade head
   ```
3. **Seed the reference index** (optional — startup does this in-memory
   anyway, but persisting it speeds cold starts):
   ```bash
   python scripts/seed_data.py
   ```
4. **Verify.** `GET /ready` on the running engine should return
   `{ status: "ok", db: "ok", valkey: "ok" }`.

Migration policy is in [architecture/database.md](architecture/database.md).
**Always test migrations on a restored prod backup before applying** —
Alembic autogenerate misses anything not expressed in the ORM.

## 6. Worker setup

```bash
python -m taskiq worker engine.tasks.worker:broker \
    --workers $NEXUS_WORKER_CONCURRENCY
```

- The broker is `taskiq-redis` backed by Valkey. If Valkey is down, the
  worker cannot pick up jobs and the API will surface 503 on `/ready`
  (the readiness check pings Valkey).
- Workers must be on the **same image version** as the API. The task
  payload schema is internal; version skew causes silent deserialisation
  failures.
- One worker process per host is usually enough; size by broker depth,
  not CPU.

## 7. Release process

Releases are driven by [release-please](https://github.com/googleapis/release-please).
The full flow is in [RELEASING.md](RELEASING.md); the short version:

1. PR titles follow [Conventional Commits](https://www.conventionalcommits.org/).
2. Merging to `main` lets release-please open / advance a "release PR".
3. Merging the release PR cuts a tag and publishes a GitHub Release.
4. The `publish-images` workflow fires on the release event, builds
   multi-arch images, and pushes them to GHCR with the new tag +
   `latest`.

### Rollout steps

1. Pull the new image: `docker compose pull` (or update the manifest
   in K8s).
2. **Run migrations first:** `alembic upgrade head` against the prod
   DB. The engine refuses to start if the schema is older than the
   code expects (this is convention, not enforcement — we trust the
   operator to follow it).
3. Roll the **worker** pods. Wait for them to drain their queues.
4. Roll the **API** pods behind the load balancer (rolling deploy with
   a readiness gate on `/ready`).
5. Smoke-test: `/health`, `/health/providers`, `/api/v1/system/status`,
   a login.

### Rollback

1. Roll back the image to the previous tag.
2. **Do not roll back migrations** unless the new migration is
   irreversible. Alembic `downgrade` is supported per-revision but
   data-lossy migrations are explicitly destructive in their
   `downgrade()` — read the migration file first.
3. If you must downgrade: stop the API + worker, `alembic downgrade
   <prev>`, then start the previous-image processes.

## 8. Backup & recovery

See [`operations/backup-and-recovery.md`](operations/backup-and-recovery.md)
for the operator-side runbook. Summary:

- **DB:** daily `pg_dump`, archived off-host. PITR via WAL-G / pgBackRest
  for ≤5-min RPO.
- **Valkey:** AOF + RDB snapshots. Worker state is fungible (re-enqueue
  from the API's `pending` rows), so RPO here is best-effort.
- **Files:** if you allow strategy plugins via filesystem (`engine/plugins/`),
  back up the directory. Plugins in container images are immutable.

DR drill cadence is in [`operations/dr-drill-checklist.md`](operations/dr-drill-checklist.md).

## 9. Hardening checklist

Before exposing the engine to the Internet:

- [ ] `NEXUS_APP_ENV=production`.
- [ ] `NEXUS_SECRET_KEY` and `NEXUS_MFA_ENCRYPTION_KEY` rotated and
  stored in a secrets manager (not in `.env`).
- [ ] `NEXUS_CORS_ORIGINS` set to the operator's UI origins.
- [ ] `NEXUS_AUTH_LOCAL_ALLOW_REGISTRATION=false` if registration is
  invite-only.
- [ ] Reverse proxy terminates TLS only; the engine is on `127.0.0.1`.
- [ ] `NEXUS_SENTRY_DSN` and `NEXUS_OTLP_ENDPOINT` populated.
- [ ] Rate-limit overrides reviewed (`NEXUS_RATE_LIMIT_PER_MINUTE`,
  `NEXUS_RATE_LIMIT_BURST`).
- [ ] Backups verified by restore — see
      [`operations/dr-drill-checklist.md`](operations/dr-drill-checklist.md).
- [ ] `pgdata` volume on a snapshot schedule.
- [ ] `/metrics` scraped by Prometheus; alerts wired per
      [`operations/slos.md`](operations/slos.md).
- [ ] Legal documents reviewed for the operator's jurisdiction;
  `NEXUS_OPERATOR_NAME` / `EMAIL` / `URL` / `JURISDICTION` populated.
- [ ] Data-provider credentials (Polygon, Alpaca, Binance, OANDA) in
  the secrets manager if used.

## 10. What this doc does **not** cover

- **Live broker integration.** No live-trade rollout procedure yet —
  see [limitations.md](limitations.md).
- **Multi-region / HA.** Single-primary Postgres, single Valkey
  shard. HA designs are vendor-specific (Cloud SQL / RDS / etc.) and
  out of scope here.
- **Frontend deploy.** The React app builds to static files; serve
  them from any CDN / nginx. There is no SSR.
