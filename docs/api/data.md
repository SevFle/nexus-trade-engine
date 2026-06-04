# Market Data, Reference Search, Marketplace

> **Source:** [`engine/api/routes/market_data.py`](../../engine/api/routes/market_data.py),
> [`engine/api/routes/reference.py`](../../engine/api/routes/reference.py),
> [`engine/api/routes/marketplace.py`](../../engine/api/routes/marketplace.py),
> [`engine/data/providers/`](../../engine/data/providers/),
> [`engine/reference/`](../../engine/reference/)

## Market data — `/api/v1/market-data`

Market-data routes are **legal-gated**.

The data layer is pluggable. At startup,
[`engine/app.py:_configure_data_providers`](../../engine/app.py) reads
`NEXUS_DATA_PROVIDERS_CONFIG` (a YAML file) or falls back to a
default Yahoo Finance adapter. The full provider set:

| Provider | Class | Asset classes | Notes |
|----------|-------|---------------|-------|
| `yahoo` | [`YahooDataProvider`](../../engine/data/providers/yahoo.py) | Equity, ETF | Default; no API key. |
| `polygon` | [`PolygonDataProvider`](../../engine/data/providers/polygon.py) | Equity, ETF, crypto, forex | Paid. |
| `alpaca` | [`AlpacaDataProvider`](../../engine/data/providers/alpaca_data.py) | Equity, ETF | Free tier rate-limited. |
| `binance` | [`BinanceDataProvider`](../../engine/data/providers/binance.py) | Crypto | Spot only. |
| `coingecko` | [`CoinGeckoDataProvider`](../../engine/data/providers/coingecko.py) | Crypto | No key, severely rate-limited. |
| `oanda` | [`OandaDataProvider`](../../engine/data/providers/oanda.py) | Forex | Practice / live accounts. |

Each adapter implements the protocol in
[`engine/data/providers/base.py`](../../engine/data/providers/base.py).
Errors come back as `TransientProviderError`, `FatalProviderError`, or
`NoProviderAvailableError` — the route layer maps these to HTTP statuses.

### `GET /api/v1/market-data/{symbol}/bars`

- **Query params:** `interval` (default `1d`), `period` (default `1y`),
  `provider?`, `asset_class?`.
- **Asset-class inference:** if `asset_class` is not supplied, the
  route infers from the symbol shape (`BTC-USD` → crypto, `EURUSD=X`
  → forex, `BRK-B` → equity, slash-pair fiat `EUR/USD` → forex,
  slash-pair with crypto quote `BTC/USDT` → crypto). The full rules
  are in
  [`engine/api/routes/market_data.py:detect_asset_class`](../../engine/api/routes/market_data.py).
- **Pinning a provider:** the `provider` query param bypasses the
  registry's routing and forces a specific adapter. Used for
  parity-testing across providers.
- **200:** `BarsResponse { symbol, interval, period, asset_class,
  provider, bars: [Bar...] }`. NaN / non-finite values are *dropped*
  before serialisation (see `_safe_float` in
  [`engine/api/routes/market_data.py`](../../engine/api/routes/market_data.py))
  so the response is always valid JSON.
- **400:** invalid symbol (validated via `_SYMBOL_RE`, which rejects
  `..`, empty strings, and anything not matching
  [`engine/data/providers/base.py:SYMBOL_PATTERN`](../../engine/data/providers/base.py)).
- **404:** `provider` query param names a non-registered provider.
- **502 / 503:** upstream provider failure. `TransientProviderError`
  becomes 503, `FatalProviderError` becomes 400.

### `GET /api/v1/market-data/{symbol}/quote`

- Same query params (only `provider` / `asset_class` apply).
- **200:** `QuoteResponse { symbol, asset_class, provider, price }`.

## Reference search — `/api/v1/reference`

Public route (no auth) used by the frontend typeahead.

### `GET /api/v1/reference/suggest`

- **Query params:** `q` (1..256 chars), `limit` (1..50, default 10),
  `asset_class?` (`equity | etf | crypto | forex | future | option`).
- **200:** `{ suggestions: [{ symbol, name, display, completion,
  score, record: { id, primary_ticker, primary_venue, asset_class,
  name, currency } }] }`.
- **Resolution order:**
  1. The local in-memory `SearchIndex` seeded at startup from
     [`engine/reference/seed.py`](../../engine/reference/seed.py).
  2. If empty, fall back to Yahoo Finance's public search endpoint
     (`https://query2.finance.yahoo.com/v1/finance/search`) with a
     5-second timeout.
- **Failure mode:** if Yahoo times out, the route returns an empty
  list rather than 5xx — typeahead should never break the UI.

## Marketplace — `/api/v1/marketplace`

> **Status: partial.** Browse + categories are real; install / uninstall
> / rate return `{ status: "not_implemented" }`. Tracked in
> [../limitations.md](../limitations.md).

All routes are **legal-gated** and most are role-gated.

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET` | `/browse` | Bearer | Paginated: `category?`, `search?`, `sort_by?`, `page?`, `per_page?`. Today returns an empty list. |
| `GET` | `/categories` | Bearer | Static category list: algorithmic, ml, llm, hybrid, income, macro. |
| `POST` | `/install` | Bearer + `developer` role | `InstallRequest { strategy_id, version? }`. Stub. |
| `DELETE` | `/uninstall/{strategy_id}` | Bearer + `developer` role | Stub. |
| `POST` | `/{strategy_id}/rate` | Bearer | `rating` 1-5, optional `review`. Stub. |

### Why a stub

The marketplace depends on a packaging + signing story for strategy
plugins that does not exist yet (sandbox isolation is partial — see
[`engine/plugins/sandbox.py`](../../engine/plugins/sandbox.py)). The
routes are present so the frontend can wire its UI without a
chicken-and-egg block on the backend.

## Provider registry internals (read this if you're adding a provider)

1. Subclass `DataProvider` from
   [`engine/data/providers/base.py`](../../engine/data/providers/base.py).
2. Declare supported asset classes in the class.
3. Register in
   [`engine/data/providers/registry.py:DataProviderRegistry`](../../engine/data/providers/registry.py)
   via YAML or in code at startup.
4. The registry ranks providers by `(priority, asset_class)` and tries
   them in order. A 429 / 5xx from a higher-priority provider falls
   through to the next.
5. Health probes go through `GET /health/providers` — every adapter
   must implement `health()` returning a `ProviderHealthResult`.

Provider-level caching and rate-limit handling live in
[`engine/data/providers/_cache.py`](../../engine/data/providers/_cache.py)
and [`engine/data/providers/_resilience.py`](../../engine/data/providers/_resilience.py).
Use them — don't roll your own retry.
