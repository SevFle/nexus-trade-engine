# Deployment

What production looks like, what infrastructure is required, and how
code gets from a merged PR to a running service.

## Target topology

The current production shape is intentionally simple — a single
host running two processes (engine + worker) plus two sidecars
(Postgres, Valkey). HA / multi-region is a future milestone (see
[`../tech-debt.md`](../tech-debt.md)).

```
┌──────────────────────────────────────────────────────────┐
│                       Operator host                       │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────┐  │
│  │   Reverse    │  │  Engine      │  │   TaskIQ       │  │
│  │   proxy /    │──│  uvicorn     │  │   worker       │  │
│  │   LB         │  │  :8000       │  │  (engine.tasks)│  │
│  └──────────────┘  └──────┬───────┘  └────────┬───────┘  │
│                           │                    │          │
│                           └────────┬───────────┘          │
│                                    │                      │
│                  ┌─────────────────┴────────────┐         │
│                  │                                │        │
│             ┌────▼─────┐                ┌─────────▼─────┐  │
│             │ Postgres │                │    Valkey     │  │
│             │ + Timesc.|                │   (Redis-compat)│ │
│             │  :5432   │                │     :6379     │  │
│             └──────────┘                └───────────────┘  │
└──────────────────────────────────────────────────────────┘
```

The reverse proxy terminates TLS, enforces any network ACLs, and
forwards to `127.0.0.1:8000`. We deliberately bind uvicorn to
`127.0.0.1` only — the proxy is the only ingress.

## Infrastructure requirements

### Postgres 16 + TimescaleDB

- **Version.** `timescale/timescaledb:latest-pg16` (matches
  `docker-compose.yml:4`).
- **CPU / memory.** 2 vCPU / 4 GB is the floor for a single-operator
  deployment doing <100 backtests/day. Production should be 4 vCPU /
  16 GB; backtests are CPU-bound on the metric and cost-model code.
- **Disk.** NVMe or SSD; TimescaleDB compresses well but OHLCV is
  write-heavy. 100 GB is plenty for 7 years of daily bars + typical
  account data.
- **Extensions.** `timescaledb` must be loaded at startup; add to
  `shared_preload_libraries`. The engine does not call
  `create_hypertable` itself today — operators running at scale
  should convert `ohlcv_bars` to a hypertable manually.
- **Backups.** Use `pg_basebackup` for physical backups; the script
  at `scripts/ops/pg_basebackup.sh` is the operator-friendly entry
  point. PITR via WAL archiving is supported; see
  [`../operations/backup-and-recovery.md`](../operations/backup-and-recovery.md).

### Valkey 8 (Redis-compatible)

- **Version.** `valkey/valkey:8-alpine` (matches `docker-compose.yml:24`).
- **CPU / memory.** Light. 1 vCPU / 1 GB for typical workloads.
- **Persistence.** AOF `appendfsync everysec` is a sensible default.
  The broker is replay-safe (TaskIQ reclaims in-flight jobs on
  restart), but losing the rate-limit state is a DoS risk.
- **Eviction.** Volatile keys only; the rate-limit and refresh-token
  buckets are TTL'd. Set `maxmemory-policy = volatile-lru` if memory
  is tight.

### Engine + worker hosts

- **CPU.** 4 vCPU minimum; backtests parallelise well. The worker
  concurrency is `NEXUS_WORKER_CONCURRENCY` (default 4); raise it to
  match CPU.
- **Memory.** 8 GB minimum. The default sandbox memory cap is 512 MB
  per evaluation; with `worker_concurrency=4`, peak is ~2 GB just for
  the sandbox.
- **Disk.** 20 GB for logs, ephemeral sandbox tmp dirs, and the
  wheelhouse.
- **Network.** Outbound HTTPS to data providers and webhook
  subscribers. Inbound from the reverse proxy only.

## Container images

Images are built from the repo's `Dockerfile`:

- **Stage 1 (builder)** — `uv:0.6-python3.12-bookworm-slim`. Builds
  a self-contained `.venv` from `uv.lock`.
- **Stage 2 (runtime)** — `gcr.io/distroless/python3-debian12:nonroot`.
  Smallest practical attack surface; no shell, no package manager.

Entrypoint: `uvicorn engine.app:create_app --factory --host 0.0.0.0
--port 8000`. The image is also used for the worker, with the
entrypoint overridden in `docker-compose.yml:43`.

To build and push:

```bash
docker compose build app worker
docker tag nexus-trade-engine-app:latest <registry>/nexus-trade-engine:<version>
docker push <registry>/nexus-trade-engine:<version>
```

The dev image (`Dockerfile.dev`) mounts the source tree for hot
reload. **Do not use it in production.**

## Environment configuration

A production deployment needs every variable in
[`../development/setup.md#environment-variables`](../development/setup.md#environment-variables)
that is not marked "dev-only". The absolute minimum:

```bash
# App
NEXUS_APP_ENV=production
NEXUS_APP_DEBUG=false
NEXUS_SECRET_KEY=<32-byte url-safe random>
NEXUS_CORS_ORIGINS=["https://dashboard.example.com"]

# DB
NEXUS_DATABASE_URL=postgresql+asyncpg://nexus:<pw>@db:5432/nexus
NEXUS_POSTGRES_USER=nexus
NEXUS_POSTGRES_PASSWORD=<pw>
NEXUS_POSTGRES_DB=nexus

# Valkey
NEXUS_VALKEY_URL=valkey://valkey:6379/0

# Auth
NEXUS_AUTH_PROVIDERS=local,google
NEXUS_MFA_ENCRYPTION_KEY=<fernet key>
NEXUS_GOOGLE_CLIENT_ID=...
NEXUS_GOOGLE_CLIENT_SECRET=...

# Observability
NEXUS_LOG_FORMAT=json
NEXUS_LOG_LEVEL=INFO
NEXUS_OTLP_ENDPOINT=http://otel-collector:4317
NEXUS_SENTRY_DSN=https://...

# Operator (legal templates)
NEXUS_OPERATOR_NAME="Your Fund LLC"
NEXUS_OPERATOR_EMAIL=legal@example.com
NEXUS_OPERATOR_URL=https://example.com
NEXUS_JURISDICTION="United States"
```

**Never commit `.env`.** The compose file's `${POSTGRES_PASSWORD:?must
be set in .env}` guard refuses to start without it.

## Secrets management

| Secret                    | Rotation procedure |
|---------------------------|--------------------|
| `NEXUS_SECRET_KEY`        | Set new value in `NEXUS_SECRET_KEY`, move the old value to `NEXUS_SECRET_KEY_PREVIOUS`. Outstanding JWTs continue to verify for one TTL window (default 60 min), then must be re-issued. |
| `NEXUS_MFA_ENCRYPTION_KEY`| Rotate by decrypting every user's `mfa_secret_encrypted` with the old key and re-encrypting with the new. There is no CLI for this today — operators who need rotation should write a one-off script against `engine/api/auth/mfa_service.py`. |
| OAuth client secrets      | Update env, restart engine. Outstanding tokens continue to validate until expiry. |
| `NEXUS_VALKEY_URL`        | Migrate by running both Valkeys in dual-write; not currently automated. |
| Webhook signing secrets   | Per-tenant, generated by the engine. User re-creates the webhook to rotate. |

## Rollout process

The release process is automated through release-please
(`release-please-config.json`). The TL;DR for operators:

1. **PR merged to `main`.** CI runs lint, typecheck, tests.
2. **release-please opens a release PR** when conventional commits
   accumulate. Merge the release PR to cut a tag.
3. **CI builds the image** for the tag and pushes to the registry
   (`publish-images` workflow).
4. **Operator deploys.** The exact mechanism is operator-specific
   (compose, k8s, nomad). The compose path:

   ```bash
   git pull
   export NEXUS_APP_VERSION=<tag>
   docker compose pull
   docker compose up -d --no-deps app worker
   docker compose exec app alembic upgrade head
   ```

   The migration step is **idempotent** but should be run before the
   new code serves traffic. New code can read old schema if a
   migration only adds columns; old code reading new schema is the
   dangerous case.

5. **Smoke test.** `curl https://<host>/health` and
   `curl https://<host>/ready`. Check `/api/v1/system/status` for
   component health and entity counts.

6. **Roll back if needed.**

   ```bash
   git checkout <previous-tag>
   docker compose up -d --no-deps app worker
   # Only run downgrade migrations if forward-fixing is impossible.
   .venv/bin/alembic downgrade -1
   ```

   Downgrades are *not* tested in CI — treat as destructive.

## Zero-downtime considerations

The current shape does **not** support zero-downtime deploys because
both replicas share a single Valkey and would race on rate-limit
counters. To get to zero-downtime:

1. Run at least two engine replicas behind the LB.
2. Use `/ready` for the LB health check; it returns `degraded` if DB
   or Valkey is unreachable.
3. Ship migrations as additive (new column nullable) in one release;
   backfill in code; drop the old column in a follow-up release.

The migration guide above (forward-fix is preferred) is the
operational consequence of that constraint.

## Health checks

| Endpoint                  | Purpose | What it checks |
|---------------------------|---------|----------------|
| `GET /health`             | Liveness | Process is up. |
| `GET /ready`              | Readiness | DB ping + Valkey ping. |
| `GET /health/providers`   | Data-provider health | Per-adapter ping. |
| `GET /api/v1/system/status` | Operator overview | Engine version, uptime, component status, entity counts. |

The LB should use `/ready` for routing decisions; the orchestrator
should use `/health` for restart decisions.

## Observability in production

- **Metrics** — Prometheus scraping `/metrics`. The
  `PrometheusBackend` is wired at app startup
  (`engine/app.py:129`). The Prometheus rule file at
  `observability/prometheus/slo-rules.yaml` defines SLO alerts.
- **Tracing** — OTLP exporter when `NEXUS_OTLP_ENDPOINT` is set.
  Collector sidecar (e.g. Jaeger, Tempo, or a commercial APM) is
  operator-supplied.
- **Logs** — JSON to stdout in production. The operator's log
  pipeline (Loki, CloudWatch, ELK) ingests from there. Sampling
  rates: INFO 100%, DEBUG 1% (tune via `NEXUS_LOG_SAMPLING_*`).
- **Sentry** — error tracking. Set `NEXUS_SENTRY_DSN`.

## Capacity planning

Back-of-the-envelope numbers from the current implementation:

| Workload                  | Throughput on a 4 vCPU / 8 GB host |
|---------------------------|------------------------------------|
| Authenticated GETs        | ~1500 req/s (DB-bound) |
| Portfolio write POSTs     | ~400 req/s (DB write) |
| Backtests (20-year daily) | ~120/day/worker (CPU-bound) |
| Webhook fan-out           | ~50/s (outbound HTTP bound) |

These are not benchmarked numbers; they are the heuristic ceiling
above which we have observed degradation in dev. The
[`../operations/load-testing.md`](../operations/load-testing.md) doc
has the harness for measuring your actual workload.

## Updating reference and legal data

- **Reference data** (instruments, exchanges). Seeded at startup by
  `engine/reference/seed.py`. To refresh, restart the engine.
- **Legal documents.** Synced from `legal/*.md` on startup
  (`engine/legal/sync.py:43`). Bump the version in front matter
  to require re-acceptance.

## Disaster recovery

RPO and RTO targets, restore drills, and PITR procedure are in
[`../operations/backup-and-recovery.md`](../operations/backup-and-recovery.md).
The DR drill checklist is in
[`../operations/dr-drill-checklist.md`](../operations/dr-drill-checklist.md).

Both should be run quarterly. The drill is the only way to know
whether backups actually work.

## Related

- [`../development/setup.md`](../development/setup.md) — local-dev
  workflow, full env var reference.
- [`../operations/runbooks.md`](../operations/runbooks.md) — diagnosis
  playbooks.
- [`../operations/slos.md`](../operations/slos.md) — what
  "production-acceptable" means.
- [`RELEASING.md`](../RELEASING.md) — release engineering detail.
