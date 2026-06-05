# Market data

Quote and bar retrieval for symbols the configured providers know.
Source: [`engine/api/routes/market_data.py`](../../engine/api/routes/market_data.py).

**Legal gate:** all routes are mounted with
[`require_legal_acceptance`](../../engine/legal/dependencies.py).

The provider registry lives in
[`engine/data/providers/registry.py`](../../engine/data/providers/registry.py).
By default the engine registers a Yahoo adapter for equities and
ETFs. Additional providers (Polygon, Alpaca, CoinGecko, OANDA,
Binance) are wired via
[`config/data_providers.example.yaml`](../../config/data_providers.example.yaml).

## Endpoints

### `GET /api/v1/market-data/{symbol}/bars`

Fetch historical OHLCV bars.

**Path:** `symbol` (case-insensitive string).

**Query params:**

| Param       | Type   | Default | Notes                              |
|-------------|--------|---------|------------------------------------|
| `start`     | ISO date | required | Inclusive.                        |
| `end`       | ISO date | required | Inclusive.                        |
| `interval`  | string | `1d`    | Provider-dependent: `1m`, `5m`, `1h`, `1d`, `1w`. |
| `provider`  | string | (auto)  | Override; otherwise the highest-priority registered provider for the symbol's asset class. |

**Response** `200 OK` — `BarsResponse`:

```json
{
  "symbol": "AAPL",
  "interval": "1d",
  "provider": "yahoo",
  "bars": [
    { "timestamp": "2024-01-02T14:30:00Z", "open": 187.15,
      "high": 188.44, "low": 183.89, "close": 185.64,
      "volume": 82481200 }
  ]
}
```

`502 Bad Gateway` if every provider is unreachable. `503 Service
Unavailable` if no provider is configured for the symbol's asset
class. `404 Not Found` if the symbol is unknown.

### `GET /api/v1/market-data/{symbol}/quote`

Latest available quote.

**Response** `200 OK` — `QuoteResponse`:

```json
{
  "symbol": "AAPL",
  "price": 185.64,
  "bid": 185.62,
  "ask": 185.66,
  "volume": 82481200,
  "timestamp": "2024-01-02T20:00:00Z",
  "provider": "yahoo"
}
```

The shape of `bid`/`ask` depends on the provider — Yahoo returns
`None` for intraday quotes outside regular session, while Polygon
and Alpaca populate them.

## Attribution

The `provider` field on every response is **required** for
compliance. Operators must display it next to the data; the
`/api/v1/legal/attributions` endpoint returns the canonical text
per provider (see [legal](legal.md)). The
`data_provider_attributions` table stores the mapping persisted by
[`engine/legal/sync.py`](../../engine/legal/sync.py).

## Caching & rate-limit behaviour

The provider layer wraps each provider with:

- An in-memory cache keyed by `(symbol, interval, date_range)`.
  TTL is provider-specific (Yahoo is cached longer because the
  data is end-of-day). See
  [`_cache.py`](../../engine/data/providers/_cache.py).
- A circuit breaker + retry layer
  ([`_resilience.py`](../../engine/data/providers/_resilience.py))
  that backs off when a provider returns repeated 5xx.

The engine's own rate limit (`NEXUS_RATE_LIMIT_PER_MINUTE`) does
**not** protect upstream providers. If you run a high-traffic
deployment, set provider-specific API keys with their own quotas
and monitor the breaker metrics.
