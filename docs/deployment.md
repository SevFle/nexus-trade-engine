# Deployment

Nexus Trade Engine is a single-tenant self-hosted deployment: one
operator runs their own stack against their own Postgres + Valkey.
This doc covers what you need to stand it up in production, and how
to roll out a new version safely.

For local dev, see [`development.md`](development.md) instead.

## Architecture in production

```
            ┌──────────────────┐
            │  Reverse proxy   │  TLS termination, HTTP/2
            │  (Caddy / Nginx) │
            └────────┬─────────┘
                     │
       ┌─────────────┼─────────────┐
       ▼             ▼             ▼
 ┌──────────┐  ┌──────────┐  ┌──────────┐
 │   app    │  │  worker  │  │   app    │  ← horizontal; stateless
 │ uvicorn  │  │  taskiq  │  │ uvicorn  │
 └────┬─────┘  └────┬─────┘  └────┬─────┘
      │             │             │
      └──────┬──────┴──────┬──────┘
             ▼             ▼
       ┌──────────┐  ┌──────────┐
       │ Postgres │  │  Valkey  │
       │ + TimescaleDB │  broker + cache
       └──────────┘  └──────────┘
```

- `app` and `worker` use the same image (`Dockerfile`) — different
  entrypoints. Scale them independently.
- Postgres and Valkey run as separate processes / VMs / managed
  services — they are *not* embedded. The compose file at the repo
  root is for development; production runs them out-of-band.
- No sticky sessions. JWT auth means any replica can serve any
  request.

## Required infrastructure

| Component | Min version | Why |
|---|---|---|
| Postgres | 16 | JSONB, `RETURNING`, perf. Add `TimescaleDB` extension if you use the OHLCV hypertable. |
| Valkey / Redis | 7 (Valkey 8 preferred) | TaskIQ broker + per-process cache. |
| Container runtime | Docker 24+ | distroless final image, no shell. |
| Reverse proxy | any | TLS termination, `/health` → app, `/ready` LB pool, `/metrics` → Prometheus. |
| Prometheus + Alertmanager | any | SLOs in [`operations/slos.md`](operations/slos.md) are wired against these rule names. |
| Log sink | any | structlog emits JSON when `NEXUS_LOG_FORMAT=json`. |
| OTel collector | optional | OTLP traces from `NEXUS_OTLP_ENDPOINT`. |
| Sentry | optional | `NEXUS_SENTRY_DSN`. |

## Image

Built by [`Dockerfile`](../Dockerfile):

- **Builder**: `uv:0.6-python3.12-bookworm-slim` → installs the
  lockfile (`uv sync --frozen --no-dev`) and copies the source.
- **Runtime**: `gcr.io/distroless/python3-debian12:nonroot`. No shell,
  no package manager. Runs as `nonroot` (UID 65532).
- `ENTRYPOINT` is `uvicorn engine.app:create_app --factory --host 0.0.0.0 --port 8000`.

The worker overrides the entrypoint:
`python -m taskiq worker engine.tasks.worker:broker`.

Build is reproducible from `uv.lock` — never build from a dirty tree
in CI.

## Environment variables

Every setting is declared in [`engine/config.py`](../engine/config.py).
Naming convention: field `foo_bar` → env `NEXUS_FOO_BAR`.

### Required in production

| Variable | Notes |
|---|---|
| `NEXUS_APP_ENV` | Set to `production`. Switches the `is_production` property (used for cookie `Secure`, etc). |
| `NEXUS_SECRET_KEY` | JWT signing key. Must be set; the lifespan hard-fails otherwise. |
| `NEXUS_DATABASE_URL` | `postgresql+asyncpg://…`. |
| `NEXUS_VALKEY_URL` | `valkey://…` or `redis://…`. |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | Used by `docker-compose.yml` (mandatory `?`-suffix enforcement). |
| `NEXUS_MFA_ENCRYPTION_KEY` | Fernet key (`cryptography.fernet.Fernet.generate_key()`). Empty disables MFA enrollment — fine for dev, unacceptable for prod. |

### Strongly recommended

| Variable | Default | Notes |
|---|---|---|
| `NEXUS_SECRET_KEY_PREVIOUS` | `""` | Enables dual-key rotation window for JWT verification. |
| `NEXUS_AUTH_PROVIDERS` | `local` | CSV subset of `local,google,github,oidc,ldap`. Each adds its own `*_CLIENT_ID`/`*_CLIENT_SECRET`/etc. |
| `NEXUS_CORS_ORIGINS` | `["http://localhost:3000"]` | JSON array literal in env: `["https://app.example.com"]`. |
| `NEXUS_RATE_LIMIT_PER_MINUTE` / `_BURST` | 600 / 60 | Per-IP. Tune for known frontends. |
| `NEXUS_DATA_PROVIDERS_CONFIG` | `""` | Path to a YAML provider registration (see `config/data_providers.example.yaml`). |
| `NEXUS_LOG_FORMAT` | `console` | Switch to `json` for production log pipelines. |
| `NEXUS_LOG_LEVEL` | `INFO` | |
| `NEXUS_OTLP_ENDPOINT` | `""` | OTLP traces + metrics exporter URL. |
| `NEXUS_SENTRY_DSN` | `""` | |
| `NEXUS_OPERATOR_NAME/EMAIL/URL` | `"Nexus Trade Engine"` / `"legal@example.com"` / `"https://example.com"` | Substituted into legal docs at render time. |
| `NEXUS_JURISDICTION` | `"United States"` | Same. |
| `NEXUS_PLATFORM_FEE_PERCENT` | `30` | Same. |

Never put secrets in a committed `.env`. Compose reads `.env` from
the working directory; CI injects via the secrets manager.

## Database setup

```bash
# 1. Create role + DB
createuser -P nexus
createdb -O nexus nexus

# 2. Enable TimescaleDB (optional, for ohlcv_bars)
psql -d nexus -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"

# 3. Run migrations
alembic upgrade head

# 4. Seed reference data (instruments list)
python scripts/seed_data.py
```

First boot will also call `sync_legal_documents()` from the lifespan
to upsert the rows under `legal_documents` from the markdown files in
`legal/`.

## Rollout process

We use a blue/green-style rolling deploy behind the LB. Steps assume
you have healthchecks wired to `/ready` (DB + Valkey).

1. **Build & push**: CI builds the image once per merge to `main`,
   tagged `:sha-<git>` and `:v<semver>` on releases. Push to your
   registry.
2. **Pre-flight** (manual or CI job):
   - `basedpyright` clean (the type-check gate).
   - `ruff check` + `ruff format --check` clean.
   - `pytest --cov-fail-under=85` passes against a Postgres service
     (coverage gate policy: [`docs/coverage-ramp.md`](coverage-ramp.md)).
3. **Migrations**: run `alembic upgrade head` against the live DB
   *before* rolling the new image. Migrations are written to be
   backwards-compatible for one release (see
   [`architecture/database.md`](architecture/database.md)) — the
   running app version must tolerate the new schema state.
4. **Roll app replicas**: drain one at a time, swap image, wait for
   `/ready` to return `ok`, re-add to LB pool.
5. **Roll workers**: same image, different entrypoint. Restart one at
   a time. TaskIQ absorbs the queue; no special drain needed unless
   you're shipping a task signature change.
6. **Smoke**:
   - `GET /health` → `{"status":"ok"}` on every replica.
   - `GET /api/v1/system/status` → engine version matches the new tag.
   - `GET /health/providers` → all expected providers `up`.
   - Submit a one-line backtest, poll `GET /backtest/results/{id}` until `completed`.

## Rollback

1. **Code**: roll the image back to the previous tag.
2. **Migrations**: only if the new migration proves dangerous. Run
   `alembic downgrade -1` *after* the old image is back (migrations
   are written to be backwards-compatible for one release, so this
   should be rare).
3. **Secrets**: if the rollout included a secret rotation
   (`NEXUS_SECRET_KEY` swap), keep the old key in
   `NEXUS_SECRET_KEY_PREVIOUS` until all outstanding JWTs have
   expired.

## Capacity planning (rules of thumb)

| Workload | App replicas | Workers | Postgres vCPU | Valkey |
|---|---|---|---|---|
| Solo operator (<100 backtests/day) | 1 | 1 | 2 | 256 MB |
| Small team (~1k backtests/day) | 2 | 2 | 4 | 1 GB |
| Mid (live trading, multi-strategy) | 3+ | 3+ | 8+ | 2 GB |

Worker concurrency is governed by `NEXUS_WORKER_CONCURRENCY` (default
4). Each concurrent backtest holds ~100 MB (Polars frames + metrics),
so size workers for `concurrency × 100 MB` headroom minimum.

## Security hardening checklist

Before exposing to the internet:

- [ ] `NEXUS_APP_DEBUG=false` (default).
- [ ] `NEXUS_SECRET_KEY` set to ≥32 bytes of randomness.
- [ ] `NEXUS_MFA_ENCRYPTION_KEY` set; MFA enroll tested.
- [ ] `NEXUS_CORS_ORIGINS` restricted to your frontend origin.
- [ ] Reverse proxy terminates TLS only; app behind it.
- [ ] `compose.yml` ports bound to `127.0.0.1` if app + proxy share a
      host (already the case in the repo).
- [ ] Postgres and Valkey *not* exposed publicly.
- [ ] `NEXUS_RATE_LIMIT_*` tuned; no client bypasses.
- [ ] `/metrics` either firewalled or behind auth in the proxy.
- [ ] Sentry / OTel sink secured.
- [ ] Backups encrypted at rest (see [`operations/backup-and-recovery.md`](operations/backup-and-recovery.md)).
- [ ] GDPR deletion runbook rehearsed (see [`operations/dr-drill-checklist.md`](operations/dr-drill-checklist.md)).

## Release artifacts

- `release-please-config.json` drives release-please; merges to `main`
  that follow Conventional Commits produce a GitHub Release with
  semver tag.
- Images are published by the `publish-images` workflow to GHCR.
- The CHANGELOG (`CHANGELOG.md`) is generated; do not hand-edit.
- See [`RELEASING.md`](RELEASING.md) for the manual rollback / hotfix
  flow.
