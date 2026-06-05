# Strategies API

Mounted at `/api/v1/strategies`. Implementation:
`engine/api/routes/strategies.py`. Wrapped in
`Depends(require_legal_acceptance)`.

List, inspect, activate, deactivate, and hot-reload strategy plugins
that the engine has discovered on disk. The discovery path is
`engine/plugins/registry.py:discover_strategies`, which globs for
`strategies/*/manifest.yaml` and imports `strategy.py` from the same
directory.

## GET /

List every strategy the registry knows about, whether or not it is
currently loaded.

**Auth** — required. Legal acceptance required.

**Response**:
```json
{ "strategies": [ { "id": "...", "name": "...", "loaded": true }, ... ] }
```

## GET /{strategy_id}

Inspect a single strategy.

**Response**:
```json
{
  "id": "mean_reversion_basic",
  "name": "Mean Reversion Basic",
  "version": "0.1.0",
  "author": "nexus-team",
  "description": "Simple mean-reversion strategy using Bollinger Bands",
  "config_schema": { /* JSON schema */ },
  "data_feeds": [ {"feed_type": "ohlcv"} ],
  "watchlist": ["AAPL", "MSFT", "GOOGL"],
  "requires_network": false,
  "requires_gpu": false,
  "is_loaded": true
}
```

`requires_network` / `requires_gpu` are derived from the manifest
trust level — they affect sandbox configuration (network whitelist,
GPU access).

**Errors** — `404` if the strategy isn't registered.

## POST /{strategy_id}/activate

Instantiate and activate a strategy with the given configuration.

**Request body** `StrategyConfigRequest`:
```json
{ "params": { "window": 20, "num_std": 2.0, "position_size": 0.1 } }
```

**Response**:
```json
{
  "status": "activated",
  "strategy_id": "mean_reversion_basic",
  "name": "Mean Reversion Basic",
  "version": "0.1.0"
}
```

**Errors** — `404` if the strategy isn't registered; `500` if
instantiation throws (the exception message is returned in `detail`).

## POST /{strategy_id}/deactivate

Deactivate and unload a strategy.

**Response** — `{"status": "deactivated", "strategy_id": "..."}`.

## POST /{strategy_id}/reload

Hot-reload a strategy module from disk. Used during plugin
development; does not change activation state.

**Response** — `{"status": "reloaded", "strategy_id": "..."}`.

**Errors** — `500` if the reload fails (re-import raises).

## GET /{strategy_id}/health

Runtime health snapshot of an active strategy.

**Response** — `{"strategy_id": "...", "is_loaded": true}`.

Today this is a thin shim; richer sandbox metrics (evaluations/sec,
last error, memory use) are exposed in the sandbox wrapper but not
yet surfaced via HTTP. See [`limitations.md`](../limitations.md).

## Manifest reference

`strategies/<name>/manifest.yaml` is the unit of plugin discovery.
The schema lives in `engine/plugins/manifest.py`. Example:

```yaml
name: mean_reversion_basic
version: "0.1.0"
description: Simple mean-reversion strategy using Bollinger Bands
author: nexus-team
parameters:
  window: 20
  num_std: 2.0
  position_size: 0.1
symbols:
  - AAPL
  - MSFT
  - GOOGL
timeframe: 1d
```

For the strategy author guide, see
[`docs/PLUGIN_DEV_GUIDE.md`](../PLUGIN_DEV_GUIDE.md).

## Plugin sandboxing

Strategies are loaded into `engine/plugins/sandbox.py:StrategySandbox`,
which enforces:

1. Import restrictions (`engine/plugins/restricted_importer.py`).
2. Network whitelist via `engine/plugins/sandboxed_http.py`.
3. Resource limits (memory, file descriptors via `resource` on Linux).
4. Filesystem isolation (tmp working dir, read-only artifacts).
5. Process isolation — **planned**, not yet implemented.

Sandbox violations raise `RuntimeError` and surface as `500` on the
activate / reload endpoints, or as `failed` on the next backtest
result.
