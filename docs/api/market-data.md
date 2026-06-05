# Market data API

Mounted at `/api/v1/market-data`. Implementation:
`engine/api/routes/market_data.py`. Wrapped in
`Depends(require_legal_acceptance)`.

Thin transport layer over the pluggable provider registry
(`engine/data/providers/`). The registry picks an adapter by symbol
shape + asset class, with optional pinning to a specific provider for
parity testing.

## Asset-class detection

If the caller doesn't supply `?asset_class=`, the engine infers it
from the symbol shape (`engine/api/routes/market_data.py:detect_asset_class`).

| Shape                | Detected as | Example        |
|----------------------|-------------|----------------|
| `XXX=X` suffix       | `forex`     | `EURUSD=X`     |
| `BASE-QUOTE`, QUOTE in crypto set | `crypto` | `BTC-USD` |
| `BASE-QUOTE`, otherwise | `equity` | `BRK-B`        |
| `BASE/QUOTE`, both fiat | `forex`  | `EUR/USD`      |
| `BASE/QUOTE`, base non-fiat + crypto quote | `crypto` | `BTC/USDT` |
| Anything else        | `equity`    | `AAPL`         |

Detection is conservative: equities are the fallback. Override with
the `asset_class` query param when you know better (`equity`, `etf`,
`crypto`, `forex`, `future`, `option`).

## GET /{symbol}/bars

Fetch OHLCV bars.

**Auth** — required. Legal acceptance required.

**Path params** — `symbol` (validated via `SYMBOL_PATTERN` from
`engine/data/providers/base.py`; rejects `..`, control chars, etc.).

**Query params**

| Param         | Type   | Default | Constraints     |
|---------------|--------|---------|-----------------|
| `interval`    | string | `1d`    | 1–8 chars       |
| `period`      | string | `1y`    | 1–8 chars       |
| `provider`    | string | null    | Pin a provider  |
| `asset_class` | string | null    | Override inference |

**Response** `BarsResponse`:
```json
{
  "symbol": "AAPL",
  "interval": "1d",
  "period": "1y",
  "asset_class": "equity",
  "provider": "yahoo",
  "bars": [
    { "timestamp": "2025-06-05T00:00:00Z", "open": 195.0,
      "high": 197.5, "low": 194.2, "close": 196.8, "volume": 52340000 }
  ]
}
```

`provider` echoes the *actual* provider that served the request (not
the pinned one if the pin failed silently — pin failures raise 4xx).

**Errors**
- `400 Bad Request` — invalid symbol or fatal provider error (e.g.
  symbol delisted).
- `404 Not Found` — no price available (quote only).
- `501 Not Implemented` — registry supports the asset class but no
  registered adapter can serve OHLCV.
- `503 Service Unavailable` — every candidate adapter failed (no
  providers configured, or all upstream returned 5xx/timeout).

### NaN handling

Bars with non-finite OHLCV values are silently dropped before
serialization. NaN in JSON is invalid; we drop rather than emit
poisoned rows. Providers that return null for delisted symbols see
the same treatment.

## GET /{symbol}/quote

Latest price for a symbol.

**Query params** — `provider`, `asset_class` (same semantics as bars).

**Response** `QuoteResponse`:
```json
{
  "symbol": "AAPL",
  "asset_class": "equity",
  "provider": "yahoo",
  "price": 196.83
}
```

**Errors** — same set as `/bars`, plus `404` if the provider returned
null (no recent trade).

## Provider registry

Six providers ship with the engine. The YAML config at
`NEXUS_DATA_PROVIDERS_CONFIG` (default `""` — Yahoo-only) wires them
up at startup via `engine/data/providers/config.py:configure_from_file`.

| Provider  | Module                                  | Asset classes                | Capabilities |
|-----------|-----------------------------------------|------------------------------|--------------|
| Yahoo     | `engine/data/providers/yahoo.py`        | Equity, ETF                  | OHLCV, quote |
| Polygon   | `engine/data/providers/polygon.py`      | Equity, ETF, Crypto, Forex   | OHLCV, quote |
| Alpaca    | `engine/data/providers/alpaca_data.py`  | Equity                       | OHLCV, quote |
| Binance   | `engine/data/providers/binance.py`      | Crypto                       | OHLCV, quote |
| CoinGecko | `engine/data/providers/coingecko.py`    | Crypto                       | quote only   |
| OANDA     | `engine/data/providers/oanda.py`        | Forex                        | OHLCV, quote |

The registry orders by `priority` (lower = preferred) and skips
adapters that don't claim the asset class. The trace returned in
`provider` lets you confirm which adapter actually served the call.

## Example YAML config

See [`config/data_providers.example.yaml`](../../config/data_providers.example.yaml)
for the full schema. Minimum viable config:

```yaml
providers:
  yahoo:
    priority: 99
    asset_classes: [equity, etf]
  polygon:
    priority: 10
    asset_classes: [equity, etf, crypto, forex]
    api_key: "${POLYGON_API_KEY}"
```

## Health check

`GET /health/providers` (no auth) returns per-provider health:

```json
{
  "status": "ok | degraded | down",
  "providers": {
    "yahoo": { "status": "up", "latency_ms": 42, "detail": null },
    "polygon": { "status": "down", "latency_ms": null,
                 "detail": "401 Unauthorized" }
  }
}
```

`status` is `down` only when *every* registered provider is down.
Individual outages surface as `degraded`.
