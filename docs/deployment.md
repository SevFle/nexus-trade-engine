# Deployment

This document is what an operator needs to take a Nexus Trade
Engine release from the registry to production. It covers the
runtime contract (what the engine expects from infra), the release
shapes (image + compose + helm chart), the rollout procedure, and
the secrets model.

For day-2 operations (alerting, backups, DR) see
[`operations/`](operations/).

## Runtime contract

| Requirement          | Why |
|----------------------|-----|
| Postgres 16 with the TimescaleDB extension | Hypertables for `ohlcv_bars` and account equity. Vanilla PG works but storage cost is higher. |
| Valkey 8 (Redis-compatible) | TaskIQ broker + result backend; shared cache for rate-limit and provider resilience layers. |
| A secrets vault (Vault, AWS SM, GCP SM, etc.) | JWT signing key, MFA Fernet key, OAuth client secrets must not live in the image. |
| Egress to data providers | Default `YahooDataProvider` reaches out to `query1.finance.yahoo.com`. Operators blocking egress must register an alternate provider via `NEXUS_DATA_PROVIDERS_CONFIG`. |
| Egress to OIDC / OAuth providers | If `NEXUS_AUTH_PROVIDERS` lists `google`, `github`, `oidc`, or `ldap`, the engine will initiate outbound connections on login. |
| A reverse proxy with TLS termination | The image itself listens on plain HTTP (`0.0.0.0:8000`). TLS, HSTS, and HTTP/2 are the proxy's job. |
| Persistent volume for `pgdata` | Single named volume in `docker-compose.yml`. Production mounts this on a real disk. |

## Container image

The Dockerfile is a two-stage build:

1. **Builder:** `uv` on `python:3.12-bookworm-slim` — installs
   pinned deps from `uv.lock`, then the project itself.
2. **Runtime:** `gcr.io/distroless/python3-debian12:nonroot`. No
   shell, no package manager, ~80 MB.

The entrypoint is fixed:

```
uvicorn engine.app:create_app --factory --host 0.0.0.0 --port 8000
```

A separate container runs the TaskIQ worker:

```
python -m taskiq worker engine.tasks.worker:broker
```

Both share the same image. Override the entrypoint in compose /
k8s.

## Environment variables

All settings live in [`engine/config.py`](../engine/config.py) under
the `NEXUS_` prefix. The minimum a production deploy must set:

| Variable | Notes |
|---|---|
| `NEXUS_APP_ENV`         | `production` (controls `is_production` flag). |
| `NEXUS_SECRET_KEY`      | JWT signing key. Lifespan raises if empty outside test env. |
| `NEXUS_SECRET_KEY_PREVIOUS` | Optional, enables dual-key rotation. |
| `NEXUS_DATABASE_URL`    | `postgresql+asyncpg://...` — must use the `asyncpg` driver. |
| `NEXUS_VALKEY_URL`      | `valkey://...` or `redis://...`. |
| `NEXUS_MFA_ENCRYPTION_KEY` | Fernet url-safe base64 key. Empty disables MFA enrollment. |
| `NEXUS_AUTH_PROVIDERS`  | Comma-separated list. `local` is the minimum. |
| `NEXUS_AUTH_LOCAL_ALLOW_REGISTRATION` | `false` in production unless the operator intends open sign-up. |
| `NEXUS_LOG_FORMAT`      | `json` for prod (consumed by the log pipeline). |
| `NEXUS_LOG_LEVEL`       | `INFO` by default. |
| `NEXUS_OTLP_ENDPOINT`   | OTLP collector gRPC endpoint. Empty disables tracing. |
| `NEXUS_SENTRY_DSN`      | Empty disables Sentry. |
| `NEXUS_CORS_ORIGINS`    | JSON array (`["https://app.example.com"]`). |
| `NEXUS_OPERATOR_*`      | Operator identity injected into rendered legal docs. |
| `NEXUS_JURISDICTION`    | Used by the tax dispatcher and legal substitutions. |

The full list with defaults is in
[`engine/config.py`](../engine/config.py).

## Compose (single-host)

`docker-compose.yml` is the reference shape and works for small /
single-host deploys. Notable choices:

- **Ports bound to 127.0.0.1**, not 0.0.0.0 — production must put
  a reverse proxy in front. The bind was tightened in commit
  `5a065a4` after a SEV finding.
- `POSTGRES_PASSWORD` is required (`${POSTGRES_PASSWORD:?must be set}`)
  to fail-fast rather than silently use the dev default.
- The `app` container depends on `db` + `valkey` healthchecks.
- The `worker` container is the same image with a different
  entrypoint.

Rollout (single-host):

```bash
git pull
docker compose pull
docker compose run --rm app alembic upgrade head
docker compose up -d
docker compose logs -f app worker | head -50   # smoke
curl -fsS http://127.0.0.1:8000/ready
```

## Kubernetes (helm chart)

There is no in-repo chart yet — this is tracked as a follow-up in
[limitations.md](limitations.md). The minimum viable chart needs:

- `Deployment` for the API (uvicorn) with liveness = `/health`
  and readiness = `/ready`.
- `Deployment` for the worker with the same image but
  `args: ["python", "-m", "taskiq", "worker", "engine.tasks.worker:broker"]`.
- `StatefulSet` for Postgres OR a managed RDS / Cloud SQL instance.
- `Deployment` for Valkey OR a managed Redis / MemoryDB instance.
- `HorizontalPodAutoscaler` on the API for CPU > 70%.
- A `NetworkPolicy` that allows egress only to the DB, Valkey,
  OIDC providers, market-data providers, and Sentry / OTLP.
- A `PodDisruptionBudget` of `minAvailable: 1` on the API.
- An `ExternalSecret` (or equivalent) that materializes
  `NEXUS_SECRET_KEY`, `NEXUS_MFA_ENCRYPTION_KEY`, and OAuth
  secrets from the vault.

## Migrations

Migrations are sequential (`001_` through `012_` at present).
Backward compatibility is enforced: every revision has a real
`downgrade()`, and long-running migrations are split into
reversible steps so they can be applied without taking writes.

Rolling out a migration:

1. **Read the migration.** Look for new NOT NULL columns (these
   need backfill), table-rewrites (`ALTER TABLE … TYPE …`), and
   index creations (these can lock).
2. **Apply during a low-write window.** The chain is forward-only;
   the app starts in parallel with the migration, and the new code
   tolerates both old and new schema for one release.
3. **Verify.** `alembic current` against the DB, then hit
   `/api/v1/system/status` and look for the new tables / columns.

Rolling back: `alembic downgrade -1` is the supported path. For
migrations that drop columns, verify the prior release still works
with the older schema (we keep N-1 compatibility for one release).

## Release pipeline

The repo uses release-please (config in
[`release-please-config.json`](../release-please-config.json)):

1. Conventional commits land on `main`.
2. release-please opens a "chore(main): release X.Y.Z" PR.
3. Merging that PR:
   - Tags `vX.Y.Z`.
   - Triggers `.github/workflows/publish-images.yml` (self-hosted
     runner) which builds the distroless image and pushes it to
     ghcr.io.
   - Triggers the GitHub release with the changelog.
4. Operators pull the new tag and follow the rollout steps above.

See [`RELEASING.md`](RELEASING.md) for the manual flow when
release-please is skipped.

## Secrets

The engine never reads secrets from disk files. Anything in
`engine/config.py` that looks like a secret is sourced from an env
var. The recommended pattern:

- **Vault + ExternalSecretsOperator** (k8s): the operator creates a
  `Secret` per environment; the deployment mounts it as env.
- **AWS Secrets Manager / GCP Secret Manager + sidecar reloader**
  for compose / single-host.
- **`.env` file** is acceptable for dev only and is git-ignored.

Rotation:

| Secret | Cadence | Notes |
|---|---|---|
| `NEXUS_SECRET_KEY` | 90 days | Set `NEXUS_SECRET_KEY_PREVIOUS` to the old value during rotation; both are accepted until the next rotation. |
| `NEXUS_MFA_ENCRYPTION_KEY` | 1 year | Hard rotation — re-encrypts every user's TOTP secret on next login. Coordinate with users. |
| OAuth client secrets | Per provider policy | Set both old and new as separate env vars during overlap, swap when the provider deprecates the old. |
| `POSTGRES_PASSWORD` | Per DBA policy | Rotate via Postgres `ALTER ROLE` then restart the engine. |
| Webhook `signing_secret` | On compromise | Generated per-webhook by the engine; rotate via the API (delete + recreate). |

## Health checks

| Probe type | Path | What it checks |
|---|---|---|
| Liveness | `GET /health` | Process is up. Always returns 200. |
| Readiness | `GET /ready` | DB `SELECT 1` + Valkey `PING`. 200 even when degraded — look at the components. |
| Provider health | `GET /health/providers` | Upstream provider reachability. Use for synthetic monitoring, not as a readiness gate. |

## Capacity planning

Numbers from the load test in
[`operations/load-testing.md`](operations/load-testing.md):

| Metric                              | Single uvicorn worker (1 vCPU) |
|-------------------------------------|--------------------------------|
| Authenticated backtest submit (RPS) | ~120 sustained                  |
| `GET /api/v1/portfolio` (RPS)       | ~600 sustained                  |
| P99 latency at saturation           | 350 ms                          |
| Worker backtest throughput          | 4 concurrent, ~3s per symbol-year |

Scaling:

- **Horizontal** (API): add uvicorn workers (`--workers N`) up to
  vCPU count, then add pods. The app is stateless.
- **Horizontal** (worker): `NEXUS_WORKER_CONCURRENCY` controls
  TaskIQ concurrency per process. Add worker pods.
- **Vertical** (DB): hypertable queries are CPU-bound on Postgres;
  scale the DB before adding workers if backtest queue depth stays
  flat while CPU saturates.

## Rollback

Two flows depending on what's wrong:

**Bad app code, schema unchanged.**
```bash
docker compose pull <previous-tag>
docker compose up -d
```

**Bad migration.**
```bash
# 1. Roll back the app to the previous tag (so it matches the schema)
docker compose pull <previous-tag> && docker compose up -d
# 2. Downgrade the DB
docker compose run --rm app alembic downgrade -1
```

If `downgrade -1` is irreversible (data loss migration), restore
from the most recent backup instead — see
[`operations/backup-and-recovery.md`](operations/backup-and-recovery.md).

## Smoke test after any deploy

```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/health/providers | jq .status
curl -fsS http://127.0.0.1:8000/ready | jq
TOKEN=$(curl -fsS -X POST http://127.0.0.1:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"smoke@example.com","password":"..."}' | jq -r .access_token)
curl -fsS -H "authorization: Bearer $TOKEN" \
  http://127.0.0.1:8000/api/v1/auth/me
curl -fsS -H "authorization: Bearer $TOKEN" \
  http://127.0.0.1:8000/api/v1/system/status | jq .counts
```

Each of these touches a different dependency chain (no-auth,
provider registry, DB+Valkey, auth+DB, system+DB counts). If all
five pass, the deploy is good.
