# Deployment

## Infrastructure Requirements

### Minimum (Single Node)

| Component | Version | Purpose |
|-----------|---------|---------|
| PostgreSQL | 16 + TimescaleDB | Primary data store |
| Valkey | 8 (Redis-compatible) | Cache, event bus, task queue |
| Python | 3.11+ | Application runtime |
| Docker | 24+ | Container runtime (recommended) |

### Recommended (Production)

| Component | Specification |
|-----------|--------------|
| CPU | 2+ cores (engine is I/O-bound, not CPU-bound) |
| RAM | 4 GB minimum (8 GB for active live trading) |
| Disk | SSD, 20 GB minimum (OHLCV data grows ~500 MB/year per symbol at 1d bars) |
| Network | Low-latency to broker API (critical for live mode only) |

## Docker Compose (Production)

The production compose file (`docker-compose.yml`) defines four services:

```yaml
services:
  db:        # TimescaleDB on port 5432
  valkey:    # Valkey on port 6379
  app:       # FastAPI engine on port 8000
  worker:    # TaskIQ background worker
```

### Starting Production

```bash
# 1. Configure environment
cp .env.example .env
# Set: POSTGRES_PASSWORD, NEXUS_SECRET_KEY, NEXUS_DATABASE_URL, NEXUS_VALKEY_URL

# 2. Build images
docker compose build

# 3. Start services
docker compose up -d

# 4. Run migrations
docker compose exec app alembic upgrade head

# 5. Verify health
curl http://localhost:8000/health
curl http://localhost:8000/ready
```

### Container Architecture

The production Dockerfile uses multi-stage builds:

1. **Builder stage:** `uv:0.6-python3.12-bookworm-slim` — installs dependencies via `uv sync --frozen --no-dev`
2. **Runtime stage:** `distroless/python3-debian12:nonroot` — minimal attack surface, no shell, no package manager

The worker container uses the same image with a different entrypoint:
```
python -m taskiq worker engine.tasks.worker:broker
```

## Environment Configuration

### Required in Production

| Variable | How to Generate | Notes |
|----------|----------------|-------|
| `POSTGRES_PASSWORD` | `openssl rand -hex 24` | Must match in all services |
| `NEXUS_SECRET_KEY` | `openssl rand -hex 32` | JWT signing — rotate with `NEXUS_SECRET_KEY_PREVIOUS` |
| `NEXUS_APP_ENV` | Set to `production` | Enables production checks |
| `NEXUS_DATABASE_URL` | Construct from POSTGRES_* | Use `db` hostname in compose |
| `NEXUS_VALKEY_URL` | `valkey://valkey:6379/0` | Use `valkey` hostname in compose |
| `NEXUS_CORS_ORIGINS` | Your frontend URL(s) | JSON array: `["https://trade.example.com"]` |

### Production Hardening

```bash
# Logging
NEXUS_LOG_FORMAT=json          # Structured JSON for log aggregation
NEXUS_LOG_LEVEL=INFO           # Not DEBUG

# Rate limiting
NEXUS_RATE_LIMIT_PER_MINUTE=120  # Tighten for public deployments

# Security
NEXUS_MFA_ENCRYPTION_KEY=<fernet_key>  # Enable MFA in production

# Observability
NEXUS_OTLP_ENDPOINT=http://collector:4317  # OpenTelemetry collector
NEXUS_SENTRY_DSN=https://xxx@sentry.io/yyy  # Error tracking
```

### Docker Compose URL Wiring

Inside Docker Compose, services reference each other by service name:

```bash
NEXUS_DATABASE_URL=postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB}
NEXUS_VALKEY_URL=valkey://valkey:6379/0
```

All ports are bound to `127.0.0.1` — only accessible from the host, not
the public internet. Put a reverse proxy (nginx, Caddy, Traefik) in front
for TLS termination and public access.

## Reverse Proxy Configuration

The engine runs on port 8000 behind a reverse proxy. Key requirements:

1. **WebSocket support** — the `/api/v1/ws` endpoint needs HTTP upgrade
2. **Forwarded headers** — set `X-Forwarded-For`, `X-Forwarded-Proto`
3. **Body size limit** — engine caps at 1 MiB, but the proxy should too
4. **Timeout** — backtest result polling may take minutes; set proxy
   read timeout to 300s minimum

Example nginx snippet:

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 300s;
}

location /api/v1/ws {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
}
```

## Rollout Process

### Standard Release

Releases are managed by `release-please` (see [RELEASING.md](RELEASING.md)):

1. Merge PRs to `main` — conventional commit messages trigger version bumps
2. `release-please` opens a release PR with changelog
3. Merge the release PR → tag is created
4. CI builds and pushes Docker images

### Deploying

```bash
# Pull latest image
docker compose pull app worker

# Rolling update (zero-downtime with multiple app instances)
docker compose up -d --no-deps --build app
docker compose up -d --no-deps --build worker

# Run migrations (before new app starts serving)
docker compose exec app alembic upgrade head
```

### Database Migrations

Migrations are backwards-compatible by convention:
- Additive changes (new tables, new columns) are safe to run before deploy
- Destructive changes (drop column, rename table) require a two-step rollout:
  1. Deploy code that doesn't use the old column
  2. Run migration in a separate step

### Health Check Verification

After deploy, verify:

```bash
# Liveness
curl http://localhost:8000/health

# Readiness (checks DB + Valkey)
curl http://localhost:8000/ready

# System status
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/system/status
```

## Scaling

### Horizontal

The engine is stateless — multiple app instances can sit behind a load
balancer. The only shared state is PostgreSQL (connection pool) and
Valkey (pub/sub). Workers scale independently:

```bash
docker compose up -d --scale worker=3
```

### Vertical

For backtest-heavy workloads, increase worker memory and CPU:
```yaml
worker:
  deploy:
    resources:
      limits:
        cpus: '2'
        memory: 4G
```

## Backup & Recovery

See [operations/backup-and-recovery.md](operations/backup-and-recovery.md) for:

- `pg_basebackup` for full backups
- Logical dumps with `pg_dump`
- Point-in-time recovery (PITR)
- DR drill checklist

## Monitoring Stack

Grafana dashboards and Prometheus alerting rules ship in-repo:

```
observability/
  grafana/           # Pre-built dashboards
  prometheus/        # SLO alert rules
```

Key metrics to monitor:
- `http_request_duration_seconds` — API latency (p50, p95, p99)
- `event_bus.published` — event throughput
- `kill_switch.state` — must be 0.0 (disengaged)
- Database connection pool usage
- TaskIQ queue depth

See [operations/slos.md](operations/slos.md) for SLO targets.
