# ADR-0005: Pluggable market-data providers

- **Status**: Accepted
- **Date**: 2026-04-29
- **Deciders**: lead maintainer + 1 reviewer
- **Tags**: data, providers, observability

## Context and Problem Statement

Algorithmic trading engines live or die on the quality, latency, and
coverage of their market data. No single provider covers every
asset class, region, and latency budget:

- **Yahoo** is free and great for equities/ETFs backtests, but has
  no per-tick data and rate-limits heavily.
- **Polygon** has tick-level equities + options, but is paid.
- **Alpaca** bundles broker + data, useful when the same vendor is
  on both sides of a live trade.
- **Binance / CoinGecko** for crypto.
- **OANDA** for forex.

A single-vendor choice would either lock the engine into one asset
class or saddle every deployment with API keys it doesn't use. We
need a registry that can route per-symbol to the right adapter and
fall back gracefully when a provider is unavailable.

## Decision Drivers

- **Asset-class coverage** — the engine must serve equity, ETF,
  crypto, forex, options, and futures strategies from a single
  surface. No single provider covers all of these well.
- **Cost** — operators in different regions and at different scales
  have very different provider budgets. The engine must work
  end-to-end on the free tier (Yahoo) and progressively get better
  as paid providers are wired in.
- **Operational isolation** — a provider outage should not take the
  whole engine down. We need health checks, circuit breakers, and
  fallback.
- **Parity testing** — when onboarding a new provider we need to
  be able to run it side-by-side with the existing one and diff
  the OHLCV bars they produce.

## Considered Options

1. **Pluggable registry** — every provider implements
   `IDataProvider`; a `DataProviderRegistry` picks one per request
   based on asset class, capability, and priority.
2. **Single-provider with config-time selection** — one
   `NEXUS_MARKET_DATA_PROVIDER=yahoo|polygon|alpaca|...` env var,
   one adapter loaded at startup.
3. **Hybrid — single provider per asset class** — config-time
   mapping `{equity: polygon, crypto: binance, forex: oanda}`,
   resolved at startup.

## Decision Outcome

Chosen option: **Option 1 — pluggable registry**, because it is the
only one that supports runtime fallback (provider A is down → try
provider B) and runtime parity testing (pin both A and B for the
same symbol and diff the responses).

### Consequences

- **Positive** — operators start with Yahoo for free, add paid
  providers later, and never have to touch strategy code. Asset
  classes are detected from the symbol shape (`AAPL` → equity,
  `BTC-USD` → crypto, `EURUSD=X` → forex) and routed automatically.
- **Positive** — provider outages are surfaced via
  `/health/providers` and degraded gracefully: a transient Yahoo
  failure becomes HTTP 503 on the data route but leaves the rest
  of the engine running.
- **Positive** — parity tests are first-class: any client can pin a
  provider via `?provider=polygon` and compare the response with
  the same call pinned to `?provider=alpaca`.
- **Negative** — the registry is a process-wide singleton. Tests
  must call `reset_registry_for_tests()` between cases to avoid
  bleed-through; failing to do so produces flaky tests.
- **Negative** — adding a new provider requires touching
  `engine/data/providers/__init__.py` (re-export) and shipping a
  YAML config block. Not zero-effort, but contained.

## Pros and Cons of the Options

### Option 1 — pluggable registry (chosen)

- **Pros:** matches every driver; supports runtime fallback,
  health checks, and parity testing; strategies don't change when
  providers change.
- **Cons:** more moving parts; tests need explicit setup.

### Option 2 — single provider with config-time selection

- **Pros:** dead-simple config; one env var.
- **Cons:** no fallback; no multi-asset-class support without
  re-implementing option 1; no parity testing.

### Option 3 — hybrid, one provider per asset class

- **Pros:** simpler than option 1 at runtime; covers multi-asset.
- **Cons:** no runtime fallback within an asset class; switching
  providers requires a deploy; no parity testing.

## Implementation notes

The registry lives at
[`engine/data/providers/registry.py`](../../engine/data/providers/registry.py).
Each adapter lives in its own module (`yahoo.py`, `polygon.py`,
`alpaca_data.py`, `binance.py`, `coingecko.py`, `oanda.py`). All
adapters share:

- A common error hierarchy: `ProviderError` → `TransientProviderError`
  vs `FatalProviderError` → maps cleanly to 503 vs 400.
- A common capability enumeration: `ohlcv`, `latest_price`,
  `streaming`, `historical`, etc. The registry raises
  `CapabilityNotSupportedError` if no provider with the requested
  capability is registered.
- A common health-check protocol: `health() -> HealthCheckResult`,
  surfaced at `/health/providers`.

Configuration is via YAML at `config/data_providers.example.yaml`,
loaded at startup by `_configure_data_providers()` in
[`engine/app.py`](../../engine/app.py). Operators who don't ship a
config get a single Yahoo adapter by default.

## Links

- Implementation: [`engine/data/providers/`](../../engine/data/providers/).
- API routes: [`engine/api/routes/market_data.py`](../../engine/api/routes/market_data.py),
  [`engine/api/routes/health.py`](../../engine/api/routes/health.py).
- Config sample: [`config/data_providers.example.yaml`](../../config/data_providers.example.yaml).
- Related: ADR-0001 (scaffold tech choices — chose `httpx` as the
  outbound HTTP client all providers use).
