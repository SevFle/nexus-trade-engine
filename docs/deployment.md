# Deployment

What an operator needs to know to run Nexus Trade Engine in
production. This doc is the launch-and-operate path; for day-2
operations see [`operations/`](operations/).

If you just want to try the engine locally, follow
[`development.md`](development.md) instead — Docker Compose handles
everything below automatically.

## Architecture assumptions

- **Single-region, single-tenant.** One Postgres, one Valkey, one app
  process (+ worker). HA / multi-region is out of scope today.
- **The engine runs behind a TLS-terminating reverse proxy.** The
  engine itself speaks HTTP; TLS is the operator's responsibility.
- **Live trading is optional.** Backtest + paper-trade work without
  broker credentials. See [`known-limitations.md`](known-limitations.md)
  for what's missing for live.

## Container images

Published to `ghcr.io/<owner>/nexus-trade-engine` on every GitHub
release (`publish-images.yml`). Multi-arch: `amd64`, `arm64`.

```bash
docker pull ghcr.io/<owner>/nexus-trade-engine:v0.5.0
# or, for "latest release"
docker pull ghcr.io/<owner>/nexus-trade-engine:latest
```

The image is built from the repo-root `Dockerfile`:

1. **Builder stage** — `uv:0.6-python3.12-bookworm-slim`, installs the
   lockfile (`uv sync --frozen --no-dev`), then copies the source.
2. **Runtime stage** — `distroless/python3-debian12:nonroot`. No
   shell, no package manager; minimal attack surface.

The container runs as `nonroot` (UID 65532) and expects to bind only
port 8000. Volume mounts are not required at runtime — all state lives
in Postgres or Valkey.

### Image tags

| Tag pattern | Meaning |
|---|---|
| `vX.Y.Z` | Release (SemVer). Pinned. |
| `latest` | Most recent release. |
| `sha-<git-sha>` | Pinned to a specific commit. |
| `main` | Last successful build of `main`. **Don't use in production.** |

## Required infrastructure

| Component | Min version | Purpose |
|---|---|---|
| Postgres | 16 | Primary data store. |
| TimescaleDB | latest-pg16 | Time-series extension. Optional but recommended for `ohlcv_bars`; vanilla Postgres works at the cost of storage. |
| Valkey | 8-alpine | TaskIQ broker + cache. Redis 7+ works as a drop-in. |
| Reverse proxy | any | TLS termination, `X-Forwarded-*` headers. |

The compose file at the repo root (`docker-compose.yml`) is the
reference deployment. It pins every image, binds ports to `127.0.0.1`
(so the operator must put something in front), and requires
`POSTGRES_PASSWORD` in the env.

## Environment variables

Every operator-tunable lives in
[`engine/config.py:Settings`](../engine/config.py). Field names map
to env vars by uppercasing + `NEXUS_` prefix. Required values:

| Variable | Why |
|---|---|
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | Required by `docker-compose.yml`. Generate the password with `openssl rand -hex 24`. |
| `NEXUS_SECRET_KEY` | JWT signing key. Generate with `openssl rand -hex 32`. **Required outside the test environment** — the lifespan aborts without it. |
| `NEXUS_DATABASE_URL` | Postgres URL with the `postgresql+asyncpg://` scheme. Compose assembles this from `POSTGRES_*`; for non-compose runs you must set it explicitly. |
| `NEXUS_VALKEY_URL` | `valkey://` or `redis://` URL. |
| `NEXUS_MFA_ENCRYPTION_KEY` | Fernet key (`base64 url-safe, 32 bytes decoded`). Empty disables MFA enrollment. Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. |

Strongly recommended:

| Variable | Notes |
|---|---|
| `NEXUS_APP_ENV=production` | Enables secure-cookie / Strict-CORS behaviours via `settings.is_production`. |
| `NEXUS_CORS_ORIGINS` | Comma-separated list. Default `http://localhost:3000`; tighten in prod. |
| `NEXUS_LOG_FORMAT=json` | Structured JSON logs (default `console`). |
| `NEXUS_LOG_LEVEL=INFO` | `DEBUG` is chatty. |
| `NEXUS_OTLP_ENDPOINT` | OTLP/gRPC or HTTP URL for traces. |
| `NEXUS_SENTRY_DSN` | Error reporting. |

Federated identity (set per provider you enable in `NEXUS_AUTH_PROVIDERS`):

| Provider | Env vars |
|---|---|
| Google | `NEXUS_GOOGLE_CLIENT_ID`, `NEXUS_GOOGLE_CLIENT_SECRET`, `NEXUS_GOOGLE_REDIRECT_URI` |
| GitHub | `NEXUS_GITHUB_CLIENT_ID`, `NEXUS_GITHUB_CLIENT_SECRET`, `NEXUS_GITHUB_REDIRECT_URI` |
| OIDC | `NEXUS_OIDC_DISCOVERY_URL`, `NEXUS_OIDC_CLIENT_ID`, `NEXUS_OIDC_CLIENT_SECRET`, `NEXUS_OIDC_REDIRECT_URI` |
| LDAP | `NEXUS_LDAP_SERVER_URL`, `NEXUS_LDAP_BIND_DN`, `NEXUS_LDAP_BIND_PASSWORD`, `NEXUS_LDAP_SEARCH_BASE` |

The full set (including tuning knobs like
`NEXUS_RATE_LIMIT_PER_MINUTE`, `NEXUS_WORKER_CONCURRENCY`,
`NEXUS_LEGAL_DOCUMENTS_DIR`) is documented inline in
[`engine/config.py`](../engine/config.py) and shipped as
[`/.env.example`](../.env.example).

## Secrets handling

- **Never commit secrets to git.** The repo's `.gitleaks.toml` runs
  in CI (see `.github/workflows/security.yml`).
- **Don't put JWT / Fernet / DB passwords in the image.** They go in
  the runtime env (compose `env_file`, k8s `Secret`, Vault, etc.).
- **Rotate `NEXUS_SECRET_KEY`** by setting both it and
  `NEXUS_SECRET_KEY_PREVIOUS` during the rotation window. Tokens
  signed by either key validate; new tokens are signed with the new
  key. Once all old tokens have expired, drop `_PREVIOUS`.
- **`NEXUS_MFA_ENCRYPTION_KEY`** cannot be rotated without re-encrypting
  every `users.mfa_secret_encrypted` column. Back up the old key
  before any rotation attempt; users with MFA enabled will need to
  re-enroll if you lose it.

## Database setup

```bash
# 1. Create the database (Compose does this for you)
createdb -h <host> -U <user> nexus

# 2. Connect the engine's URL, then migrate
NEXUS_DATABASE_URL=postgresql+asyncpg://<user>:<pw>@<host>:5432/nexus \
  alembic upgrade head

# 3. (optional) Enable TimescaleDB if you didn't use the timescale image
psql -h <host> -U <user> nexus -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"
```

The first app start will also call
[`engine/legal/sync.py`](../engine/legal/sync.py), which ingests every
Markdown file under `NEXUS_LEGAL_DOCUMENTS_DIR` (default `legal/`)
into the `legal_documents` table.

## Process topology

```
                ┌─────────────────────┐
                │  Reverse proxy/TLS  │
                └──────────┬──────────┘
                           │ HTTP
              ┌────────────┴────────────┐
              ▼                         ▼
   ┌──────────────────┐      ┌─────────────────────┐
   │  app (uvicorn)   │      │  worker (taskiq)    │
   │  port 8000       │      │  no port (consumer) │
   └────────┬─────────┘      └──────────┬──────────┘
            │                            │
            └────────┬───────────────────┘
                     ▼
        ┌────────────────────────┐
        │ Postgres + TimescaleDB │
        │ Valkey                 │
        └────────────────────────┘
```

The worker reads from the same Valkey broker the app writes to.
Horizontal scaling of the app layer is fine as long as WebSocket
clients are pinned to one instance (sticky sessions) — see
[`known-limitations.md`](known-limitations.md).

## Health and readiness

| Endpoint | Container probe | Behaviour |
|---|---|---|
| `GET /health` | liveness | Always `200 {"status": "ok"}`. |
| `GET /ready`  | readiness | `200 {"status": "ok|degraded", "db": ..., "valkey": ...}`. Route to ready-pod; pull non-ready out. |
| `GET /metrics` | Prometheus scrape | Text format. |

Sample k8s probe snippet:

```yaml
livenessProbe:
  httpGet: { path: /health, port: 8000 }
  initialDelaySeconds: 10
readinessProbe:
  httpGet: { path: /ready, port: 8000 }
  initialDelaySeconds: 5
  periodSeconds: 5
```

## Logging

In production set:

```
NEXUS_LOG_FORMAT=json
NEXUS_LOG_LEVEL=INFO
NEXUS_LOG_SAMPLING_INFO=1.0     # keep every INFO+
NEXUS_LOG_SAMPLING_DEBUG=0.01   # 1% of DEBUG (default)
```

The JSON schema is documented in
[`docs/observability/logging.md`](observability/logging.md). Sensitive
fields (`authorization`, `password`, `token`, `signing_secret`,
`mfa_secret`, `mfa_code`) are scrubbed before serialisation. See
[`engine/observability/redact.py`](../engine/observability/redact.py)
for the redactor.

## Metrics

Prometheus scrape endpoint at `/metrics`. Names match
[`docs/operations/slos.md`](operations/slos.md) "SLI Reference".

The Prometheus rule file at
[`observability/prometheus/slo-rules.yaml`](../observability/prometheus/slo-rules.yaml)
encodes the burn-rate alerts. Point your Prometheus / Alertmanager at
it.

## Rollout procedure

For a normal versioned release:

1. **CI passes on `main`.** The CI matrix
   (`.github/workflows/ci.yml`) gates on lint, typecheck, pytest with
   coverage ≥ 70%, and the security workflow.
2. **Release-please opens a release PR** when conventional commits
   accumulate. Merging that PR tags and publishes the image.
3. **Pull the new image** in your environment:
   ```bash
   docker compose pull app worker
   docker compose up -d app worker
   ```
4. **Watch the rollout.** Tail logs (`docker compose logs -f app`)
   and watch `/api/v1/system/status` for ~5 minutes. If the worker is
   processing backtests, watch the task pipeline SLO dashboard.
5. **Run any pending migrations** *before* starting the new image if
   the release notes call for it. By default migrations are
   forward-only; see [`architecture/database.md`](architecture/database.md)
   for the rollback policy.

### Rollback

1. `docker compose` doesn't have a built-in rollback. Re-pin the tag
   in your env file (e.g. `IMAGE_TAG=v0.4.2`) and `docker compose up -d`.
2. **Do not roll back the database.** Forward-only migrations are the
   rule. If a migration broke something, write a forward fix migration
   instead. The only exception is data-loss migrations — those ship
   with a `downgrade()` that operators can run with
   `alembic downgrade -1` after taking the app out of rotation.

## Capacity planning

Rough numbers from a single `nexus-trade-engine` instance on a 4-vCPU
/ 8 GB host (the dev compose stack profiled by `k6` in
[`docs/operations/load-testing.md`](operations/load-testing.md)):

- ~120 req/s sustained at p50 < 100 ms (excluding backtest runs).
- Backtest throughput is strategy-bound, not engine-bound — typical
  1-year daily-bar run finishes in 2-5 s of wall time per symbol.
- WebSocket connections: ~500 concurrent per process before the
  Python event loop saturates on broadcast.

Tune `NEXUS_WORKER_CONCURRENCY` (default 4) up if the task queue depth
grows. The worker process does not horizontally auto-scale today.

## Multi-region / HA

Out of scope today. The two specific gaps:

- **WebSocket manager is in-process.** A second replica will not see
  events published on the first. (See [`known-limitations.md`](known-limitations.md).)
- **No Postgres replication story is shipped.** Use a managed Postgres
  with replica promotion (RDS, Cloud SQL, Crunchy Bridge) if you need
  HA at the DB layer.

When HA matters, run the engine stateless behind a load balancer and
terminate WebSockets at a sticky-session layer (e.g. nginx `ip_hash`).
The Valkey-backed session bridge on the roadmap would remove the
stickiness requirement.

## Related

- [`development.md`](development.md) — local dev environment.
- [`operations/slos.md`](operations/slos.md) — what to alert on.
- [`operations/backup-and-recovery.md`](operations/backup-and-recovery.md)
  — backup strategy + PITR.
- [`RELEASING.md`](RELEASING.md) — release-please workflow.
