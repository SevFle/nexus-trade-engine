# Market data API

OHLCV bars and latest-quote endpoints backed by the pluggable provider
registry. Implementation:
[`engine/api/routes/market_data.py`](../../engine/api/routes/market_data.py).

The registry routes a request to a provider based on asset class and
priority (see `engine/data/providers/registry.py`). The caller can
also pin a specific provider via `?provider=yahoo` for parity testing.

## Endpoint summary

| Method | Path | Auth | Legal | Purpose |
|---|---|---|---|---|
| `GET` | `/api/v1/market-data/{symbol}/bars`  | JWT/API key | required | OHLCV history |
| `GET` | `/api/v1/market-data/{symbol}/quote` | JWT/API key | required | Latest price |

## Path / query parameters

| Param         | Where   | Default | Notes |
|---------------|---------|---------|-------|
| `symbol`      | path    | —       | Validated against `^[A-Z0-9.\-/]{1,16}$`. `..` is rejected. |
| `interval`    | query   | `1d`    | Provider-dependent. Yahoo accepts `1d`/`1wk`/`1mo` and intraday. |
| `period`      | query   | `1y`    | Provider-dependent. |
| `provider`    | query   | unset    | Pin a specific adapter (skip the registry). |
| `asset_class` | query   | auto-detected | Override inferred class. One of `equity`, `etf`, `crypto`, `forex`, `future`, `option`. |

## Asset-class detection

When `asset_class` is not provided, the route infers from the symbol
shape (`engine/api/routes/market_data.py:detect_asset_class`):

1. Yahoo-style forex suffix (`EURUSD=X`) → `forex`.
2. Dash pair with crypto quote (`BTC-USDT`, `ETH-USD`) → `crypto`.
3. Dash pair with anything else (`BRK-B`) → `equity`.
4. Slash pair, both sides fiat (`EUR/USD`) → `forex`.
5. Slash pair, base non-fiat (`BTC/USD`) → `crypto`.
6. Default → `equity`.

Crypto is checked before forex because the fiat-vs-crypto quote sets
overlap (otherwise `BTC/USD` would misclassify as forex).

## Schemas

```python
class Bar(BaseModel):
    timestamp: str   # ISO-8601 UTC
    open: float
    high: float
    low: float
    close: float
    volume: float

class BarsResponse(BaseModel):
    symbol: str
    interval: str
    period: str
    asset_class: str
    provider: str    # the adapter that served the request
    bars: list[Bar]

class QuoteResponse(BaseModel):
    symbol: str
    asset_class: str
    provider: str
    price: float
```

Provider values that include `NaN` or `None` for any OHLCV field are
silently dropped before serialisation (NaN would produce invalid
JSON). Empty result sets are returned as `"bars": []`, not an error.

## Examples

```bash
# Daily bars for AAPL
curl 'http://localhost:8000/api/v1/market-data/AAPL/bars?period=6mo&interval=1d' \
  -H 'authorization: Bearer <access>'

# Latest crypto quote
curl 'http://localhost:8000/api/v1/market-data/BTC-USD/quote' \
  -H 'authorization: Bearer <access>'

# Pin to yahoo for parity testing
curl 'http://localhost:8000/api/v1/market-data/AAPL/bars?provider=yahoo' \
  -H 'authorization: Bearer <access>'
```

## Errors

| Status | When |
|---|---|
| `400` | Invalid symbol, or `FatalProviderError` (e.g. unknown asset class for provider). |
| `401` | Missing/invalid token. |
| `403` | Legal acceptance incomplete. |
| `404` | `?provider=<name>` was specified but that adapter is not registered, or no price available for the symbol. |
| `501` | No registered provider supports the requested capability (`CapabilityNotSupportedError`). |
| `502` | Generic `ProviderError` from the adapter (only `/quote`). |
| `503` | All candidate adapters failed (`NoProviderAvailableError`) or transient upstream timeout (`TransientProviderError`). |

The 502 / 503 distinction matters for clients doing automatic retry:
**503 is retryable, 502 is not.** Transient failures (`TimeoutError`,
`TransientProviderError`) are logged at `warning` and surfaced as 503.

## Provider registry

The default engine registers `YahooDataProvider` at priority 99 for
`{equity, etf}`. Operators can register additional adapters via
`NEXUS_DATA_PROVIDERS_CONFIG` (YAML file with one entry per provider).
At runtime, the registry iterates candidate providers in priority order
and returns the first that supports the requested capability.

See [`engine/data/providers/registry.py`](../../engine/data/providers/registry.py)
for the priority-routing and tracing internals.

## Related

- [Reference API](reference.md) — symbol-search / typeahead that hits
  Yahoo's search API when the local index has no match.
- [`docs/architecture/plugins.md`](../architecture/plugins.md) — how
  to ship a new data provider.
