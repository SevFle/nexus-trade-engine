# Market data API

Base path: `/api/v1/market-data`. Source:
[`engine/api/routes/market_data.py`](../../engine/api/routes/market_data.py),
[`engine/data/providers/`](../../engine/data/providers/).

Fetch OHLCV bars or a latest quote for one symbol. The request is
routed through the data-provider registry: based on the symbol's
inferred asset class, the registry picks the highest-priority adapter
that supports the requested capability.

This router is mounted with `require_legal_acceptance`.

## Asset-class inference

The route infers `asset_class` from the symbol when the caller does
not pin it via query param. Order matters — first match wins:

| Pattern                                  | Inferred `asset_class` |
|------------------------------------------|------------------------|
| `USDJPY=X`, `EUR/USD=X` (Yahoo suffix)   | `forex`                |
| `BTC-USD`, `ETH-USDT`                    | `crypto`               |
| `BRK-B` (dash with non-crypto quote)     | `equity`               |
| `BTC/USD` (slash, base *not* fiat, quote is crypto) | `crypto`     |
| `EUR/USD` (slash, both fiat)             | `forex`                |
| Anything else                            | `equity`               |

Override by passing `?asset_class=<equity | etf | crypto | forex | future | option>`.

## Endpoints

### `GET /api/v1/market-data/{symbol}/bars`

Fetch historical OHLCV bars.

**Auth**: Bearer JWT or API key with `read`+ scope.

**Path params**: `symbol` — validated against
`engine/data/providers/base.SYMBOL_PATTERN`. `..` and other
path-traversal shapes are rejected before the registry sees the
value.

**Query params**:

| Name          | Type   | Default | Notes                                                                                   |
|---------------|--------|---------|-----------------------------------------------------------------------------------------|
| `interval`    | string | `1d`    | Bar interval (`1m`, `5m`, `1h`, `1d`, …). 1–8 chars. Provider-dependent support.        |
| `period`      | string | `1y`    | Lookback period (`1mo`, `3mo`, `1y`, `5y`, …). 1–8 chars.                              |
| `provider`    | string | null    | Pin a specific provider (e.g. `polygon`). Bypasses registry routing. 1–32 chars.        |
| `asset_class` | string | inferred| Override the inferred asset class. 1–16 chars.                                          |

**Response**: `200 OK`:

```json
{
  "symbol": "AAPL",
  "interval": "1d",
  "period": "1y",
  "asset_class": "equity",
  "provider": "yahoo",
  "bars": [
    {
      "timestamp": "2023-01-03T00:00:00+00:00",
      "open": 130.28,
      "high": 130.90,
      "low": 124.17,
      "close": 125.07,
      "volume": 112117500.0
    }
  ]
}
```

Rows with non-finite floats (`NaN`, `Infinity`) are silently dropped
rather than emitting invalid JSON. Provider errors are mapped:

| Exception                  | HTTP | Meaning                                                  |
|----------------------------|------|----------------------------------------------------------|
| `CapabilityNotSupportedError` | 501 | No registered provider supports this capability.      |
| `NoProviderAvailableError` | 503  | Every candidate provider failed.                         |
| `FatalProviderError`       | 400  | Provider rejected the request (bad symbol, etc.).        |
| `TransientProviderError` / `TimeoutError` | 503 | Provider was up but errored or timed out. |

### `GET /api/v1/market-data/{symbol}/quote`

Latest known price for one symbol. Cheap path intended for the
dashboard ticker.

**Auth**: Bearer JWT or API key with `read`+ scope.

**Query params**: same `provider` and `asset_class` overrides as
above.

**Response**: `200 OK`:

```json
{
  "symbol": "AAPL",
  "asset_class": "equity",
  "provider": "yahoo",
  "price": 178.45
}
```

`404` if the provider returned no price for the symbol. `502` for
generic provider errors.

## Provider configuration

Providers are registered at app startup in `_configure_data_providers`
(`engine/app.py`). The default flow:

1. If `NEXUS_DATA_PROVIDERS_CONFIG` points at a YAML file, load that.
   Format example: [`config/data_providers.example.yaml`](../../config/data_providers.example.yaml).
2. Otherwise, register a `YahooDataProvider` as the catch-all for
   `equity` and `etf`.

Adding a provider = adding an entry to the YAML config + ensuring
the relevant env vars (API keys, etc.) are present.

## Health

Per-provider health is exposed at `GET /health/providers`. Use it
before troubleshooting a 503.
