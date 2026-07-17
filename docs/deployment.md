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
- **No frontend service in this stack.** The React SPA in
  [`frontend/`](../frontend/) is dev-only today — `frontend/Dockerfile`
  runs the Vite dev server and it is absent from this compose file. If
  you need a UI in production, build and serve the static bundle
  out-of-band; see the [React dashboard P1](known-limitations.md#react-dashboard).

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
| `NEXUS_RATE_LIMIT_VALKEY_ENABLED` | `false` | **Set `true` for multi-replica.** When on, limits are enforced globally via Valkey instead of per-pod (otherwise the effective limit becomes `per_minute × replica_count`). |
| `NEXUS_RATE_LIMIT_ROLE_TIERS` | `""` | JSON `{role:[per_minute,burst]}` for per-role overrides when a Bearer JWT is present (e.g. `{"viewer":[120,30],"admin":[6000,200]}`). |
| `NEXUS_DATABASE_POOL_SIZE` / `_MAX_OVERFLOW` | 5 / 10 | SQLAlchemy asyncpg pool sizing. Raise for high-concurrency replicas; each backtest/long query holds a connection. |
| `NEXUS_DATA_PROVIDERS_CONFIG` | `""` | Path to a YAML provider registration (see `config/data_providers.example.yaml`). |
| `NEXUS_LOG_FORMAT` | `console` | Switch to `json` for production log pipelines. |
| `NEXUS_LOG_LEVEL` | `INFO` | |
| `NEXUS_OTLP_ENDPOINT` | `""` | OTLP traces + metrics exporter URL. |
| `NEXUS_SENTRY_DSN` | `""` | Crash/error pipeline. Empty → `init_sentry()` is a no-op. See [Sentry PII scrubbing](observability/logging.md#error-tracking-sentry). |
| `NEXUS_SENTRY_TRACES_SAMPLE_RATE` | `0.0` | Sentry performance-trace sampling (`0.0` disables). |
| `NEXUS_OPERATOR_NAME/EMAIL/URL` | `"Nexus Trade Engine"` / `"legal@example.com"` / `"https://example.com"` | Substituted into legal docs at render time. |
| `NEXUS_JURISDICTION` | `"United States"` | Same. |
| `NEXUS_PLATFORM_FEE_PERCENT` | `30` | Same. |

### WebSocket (`NEXUS_WS_*`)

The active `WS /api/v1/ws` endpoint (SEV-275) is configurable end-to-end.
The defaults are safe for a single replica; review them before scaling.
Field names map to env as `NEXUS_WS_<UPPER>` (see
[`config.py`](../engine/config.py) and [`.env.example`](../.env.example)).

| Variable | Default | Notes |
|---|---|---|
| `NEXUS_WS_MAX_CONNECTIONS` | `5000` | Hard cap on concurrent connections per process. New connections past this are closed with code `1011`. |
| `NEXUS_WS_SEND_QUEUE_SIZE` | `256` | Per-connection bounded send queue. A full queue drops the message and closes the connection (`1008`) — deliberate backpressure, never unbounded memory growth. |
| `NEXUS_WS_MAX_SUBSCRIPTIONS_PER_CONNECTION` | `50` | Per-connection room cap (excludes the auto-joined `user:<id>` room). Exceeding it returns `429`. |
| `NEXUS_WS_HEARTBEAT_INTERVAL_SECONDS` | `30.0` | Server heartbeat cadence. Connections silent for `2 × interval` are reaped as stale. |
| `NEXUS_WS_IDLE_TIMEOUT_SECONDS` | `300.0` | Max time a connection may stay open without activity. |
| `NEXUS_WS_AUTH_TIMEOUT_SECONDS` | `5.0` | Window after `accept()` for the client to send its `auth` message (or supply `?token=`). |
| `NEXUS_WS_AUTH_RATE_LIMIT_PER_MINUTE` | `10` | Per-IP token bucket on auth attempts (defends the handshake against token-spray). |
| `TRUSTED_PROXIES` | *(unset)* | Comma-separated allow-list of reverse-proxy hops trusted to set `X-Forwarded-For`/`X-Real-IP` for WebSocket auth-rate-limiting. **CIDR-aware** (e.g. `10.0.0.0/8,172.16.0.0/12`). Read directly from the env (no `NEXUS_` prefix). When unset, the raw peer IP is used and forwarded headers are ignored — the correct default for a direct-internet deploy. See [WebSocket API](websocket.md). |
| `NEXUS_WS_EVENT_BRIDGE_CONCURRENCY` | `32` | Fan-out concurrency of the cross-replica `EventBusBridge`. |

<a id="websocket"></a>
#### Trusted-proxy IP resolution (auth rate-limiting)

The WebSocket auth rate-limit is **per client IP**. Behind a load
balancer, the raw `ws.client.host` is the LB, not the user — so the
[`_get_remote_ip`](../engine/api/ws/auth.py) resolver implements a
**trusted-proxy** model (gh#1497):

- The peer IP is used directly unless it appears in `TRUSTED_PROXIES`.
- When the peer *is* a trusted proxy, the **rightmost hop** of
  `X-Forwarded-For` (falling back to `X-Real-IP`) is trusted instead.
- `TRUSTED_PROXIES` is matched with **CIDR awareness**
  ([`is_trusted_proxy`](../engine/api/ip_utils.py)): `10.0.0.0/8` covers
  the whole VPC, not just the literal string `10.0.0.0/8`.
- Entries outside the trusted set are ignored, so an **untrusted** peer
  spoofing `X-Forwarded-For` changes nothing.

That last point is the whole reason for the fix. The prior code did a
literal string compare against `TRUSTED_PROXIES`; a single matched proxy
then trusted *any* `X-Forwarded-For` value, so any client could claim to
be any address and dodge the per-IP bucket. The CIDR match narrows trust
to your real proxy hops, and the rightmost-hop parse (`rsplit(",", 1)[-1]`)
means a pathologically long header can't force a huge allocation to read
one hop.

> Leave `TRUSTED_PROXIES` **unset** for a direct-internet deploy (no LB):
the raw peer IP is correct and forwarded headers are untrusted by
default. Set it to your LB/VPC CIDRs only when terminating TLS at a proxy.

The live socket registry is per-process; event *delivery* is
[already cross-replica](websocket.md#event-delivery) via the
`EventBusBridge` + Valkey pub/sub — see
[`known-limitations.md`](known-limitations.md). Auth is JWT-only (no
`nxs_*` API keys on WS).

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
   - `pytest --cov-fail-under=70` passes against a Postgres service.
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
