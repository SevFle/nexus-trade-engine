# System status API

Base path: `/api/v1/system`. Source:
[`engine/api/routes/system.py`](../../engine/api/routes/system.py).

A headless-friendly summary of the running engine: version, uptime,
component health, and per-table counts. Intended for CI/CD probes,
operator scripts, and the dashboard's health card.

## Endpoint

### `GET /api/v1/system/status`

**Auth**: Bearer JWT or API key with `read`+ scope.

**Response**: `200 OK`:

```json
{
  "engine_version": "0.1.0",
  "uptime_seconds": 14235.117,
  "server_time": "2026-06-06T12:00:00Z",
  "components": [
    { "name": "database", "healthy": true, "detail": null }
  ],
  "counts": {
    "users": 12,
    "portfolios": 27,
    "backtests": 312,
    "webhooks_active": 4,
    "api_keys_active": 9
  }
}
```

Today only `database` is reported under `components`. Adding Valkey
and the configured market-data providers is on the to-do list — keep
the shape stable when adding entries (component name, healthy bool,
optional detail string).

If a count fails (table missing, permission denied), the value is
`-1`. This is best-effort telemetry, not a hard contract — don't
alert on the count, alert on the `healthy` flag.

## Engine version source

`_engine_version()` reads the installed distribution version from
`importlib.metadata.version("nexus-trade-engine")`. In a container
built from `Dockerfile`, this comes from the `pyproject.toml`
`version` field at build time. In editable dev installs, it may
return `0.0.0+unknown` — that is a packaging artifact, not a runtime
problem.
