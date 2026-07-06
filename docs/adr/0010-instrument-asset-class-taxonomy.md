# ADR-0010: Instrument / asset-class taxonomy split

- **Status**: Accepted
- **Date**: 2026-07-06
- **Deciders**: Lead maintainer + core-engine reviewer
- **Tags**: core, instruments, multi-asset, data-model

## Context and Problem Statement

Nexus grew up as an **equity** engine. Every tradable thing was a free-form
`symbol` string (`"AAPL"`) and the database keys positions, orders, and
tax lots on that string. As multi-asset support landed (PR #1213), that
representation stopped being adequate: a crypto pair (`BTC/USDT`), a
forex pair (`EUR/USD`), an option (`AAPL_20260115_C_150.00`), and a
dated future are not "strings with a convention" — each carries a
**distinct, class-specific required field set** that must be validated
(strike/expiration/option-type/underlying for options;
base/quote for crypto and forex; multiplier and expiration for futures).
A plain string can neither enforce those invariants nor answer
derivable questions ("is this a derivative?", "what is the per-contract
notional?", "is this the *same* position as the perpetual on the same
pair?").

At the same time the data-routing layer already had an `AssetClass`
enum ([`engine/data/providers/base.py`](../../engine/data/providers/base.py))
used to pick which provider can serve a query. That taxonomy is
deliberately coarse: a single `CRYPTO` routes spot, perpetual, and dated
futures alike because one adapter (Binance) serves all three. We needed
typed instruments for *modeling* without forcing the *routing* taxonomy
to fan out to match it.

## Decision Drivers

- **Per-class invariants.** An option without a strike is a bug, and the
  type system should say so at construction time, not at fill time.
- **Independent evolution of the two taxonomies.** The routing enum
  changes when a provider's coverage changes; the modeling enum changes
  when the engine learns a new product. Coupling them couples their
  release cadences.
- **Backward compatibility.** Existing strategies and the entire DB
  schema speak `symbol`. The new model must be opt-in and never break a
  caller that passes a bare string.
- **No premature persistence.** Nothing in the running engine joins on
  an instrument identity today; `symbol` is the established join key.
  Adding an `instruments` table before anything reads it is speculative
  schema.

## Considered Options

1. **Single shared enum + plain symbol strings** (status quo ante) — keep
   one `AssetClass`, keep everything keyed by string.
2. **One unified `Instrument`, persisted as a table** — make instruments
   first-class DB entities; key positions/orders on `instrument_id`.
3. **Two enums** — a domain `InstrumentAssetClass` for modeling plus the
   existing data-routing `AssetClass`, bridged by an explicit
   `to_provider_class()` mapping; `Instrument` is a **non-persisted
   runtime value object** that the engine derives from each signal's
   `symbol`.

## Decision Outcome

Chosen option: **Option 3 — two enums with a value-object `Instrument`**,
because it enforces per-class invariants and decouples the two
taxonomies without coupling their release cadences or churning the
persistence layer before anything consumes it.

Concrete shape (source: [`engine/core/instruments.py`](../../engine/core/instruments.py)):

- **`InstrumentAssetClass`** (modeling) — 8 members: `equity`, `etf`,
  `crypto`, `crypto_perp`, `crypto_future`, `forex`, `option`, `future`.
  This is the *finer* taxonomy: spot, perpetual, and dated crypto
  futures are distinct because they are different *positions*.
- **`AssetClass`** ([`engine/data/providers/base.py`](../../engine/data/providers/base.py),
  routing) — 6 members: `equity`, `etf`, `crypto`, `forex`, `options`,
  `futures`. This is the *coarser* taxonomy: it answers "which provider
  can serve this?", not "what position is this?".
- **`Instrument.to_provider_class()`** is the explicit bridge between
  them (the three crypto flavors all collapse to routing `CRYPTO`;
  `option → OPTIONS`, `future → FUTURES`).
- **`Instrument`** is a `Pydantic` model (`validate_assignment=True`)
  with factories (`equity()`, `etf()`, `crypto()`, `crypto_perp()`,
  `forex()`, `option()`, `future()`) and a `from_string()` coercion that
  **defaults to equity** for any free-form symbol (so a slash-bearing
  `EUR/USD` is not silently misread as a crypto pair). Class invariants
  are enforced in `_enforce_class_invariants`; the `model_copy` override
  re-runs every validator on update so a copy can't smuggle in an
  invalid field set.
- **`uid`** is the stable per-instrument identity — distinct across
  spot/perp/dated-future on the same pair, and across option strikes —
  so positions in different products never collapse onto one key.

### Consequences

- **Positive** — per-class required fields are validated at construction;
  `is_derivative` / `contract_value` / `uid` are derivable from one
  typed object; the routing and modeling taxonomies can ship
  independently.
- **Positive** — backward compatible by construction. The engine's
  internal `Signal` ([`engine/core/signal.py`](../../engine/core/signal.py))
  carries an `instrument` field that is **auto-derived from `symbol`**
  via `Instrument.from_string` when the caller omits it, so every
  existing strategy keeps working unchanged.
- **Negative** — there are now **two enums that must be kept bridged**.
  A new modeling class with no routing equivalent (or vice versa) is a
  real edit, not a no-op. `to_provider_class` uses `assert_never` so an
  unmapped member fails loudly at the boundary.
- **Negative** — "what is being traded" now has **three representations**:
  the SDK `Signal.symbol` (string), the engine `Signal.instrument`
  (typed), and the persisted `symbol` column (string). They are
  consistent today because `instrument` is derived from `symbol`, but a
  future schema that persists instruments must reconcile all three.
- **Neutral** — the market-data route keeps its own string-shape
  `detect_asset_class()` heuristic
  ([`engine/api/routes/market_data.py`](../../engine/api/routes/market_data.py)),
  which returns the *routing* `AssetClass` directly without constructing
  an `Instrument`. That is correct for routing and avoids a model
  allocation per request, but it means the `Instrument` model is not yet
  on the request hot path.

## Status caveat (be honest)

`Instrument` / `InstrumentAssetClass` are **engine-internal today**:

- The **public SDK** ([`sdk/nexus_sdk/signals.py`](../../sdk/nexus_sdk/signals.py))
  `Signal` exposes only `symbol`; it does **not** export `Instrument` or
  `InstrumentAssetClass`. Strategy authors still emit string-symbol
  signals; the engine derives the typed instrument on ingest.
- Nothing is **persisted** — `engine/db/models.py` has no instrument
  table or `asset_class` column; positions/orders/tax lots remain keyed
  by string `symbol`.
- The model is fully unit-tested
  ([`tests/test_instruments.py`](../../tests/test_instruments.py),
  [`tests/test_instruments_coverage.py`](../../tests/test_instruments_coverage.py))
  but is not yet consumed by the live order/execution path.

See [`docs/known-limitations.md`](../known-limitations.md) and the
"Multi-asset instrument modeling" section of
[`docs/architecture/core-domains.md`](../architecture/core-domains.md).

## Pros and Cons of the Options

### Option 1 — Single shared enum + plain strings

- **Pros:** Zero new surface; no bridge to maintain.
- **Cons:** Cannot enforce per-class required fields; cannot derive
  `uid`/`is_derivative`/`contract_value`; routing and modeling share one
  taxonomy whether or not their lifecycles align.

### Option 2 — Unified persisted `Instrument` table

- **Pros:** Single source of truth; positions/orders/tax lots gain a
  stable foreign key independent of symbol formatting.
- **Cons:** Speculative schema — nothing joins on instrument identity
  yet, so the table would sit unwritten. Churns every persisted model
  and migration at once. Forces the routing taxonomy to fan out to match
  modeling (or vice versa) if coupled.

### Option 3 — Two enums + value-object `Instrument` (chosen)

- **Pros:** Enforces invariants; decouples taxonomies; opt-in and
  backward compatible; no persistence churn until a consumer exists.
- **Cons:** Two enums to bridge; three representations of "what is
  traded" until unified; the bridge mapping is hand-maintained.

## Links

- Feature PR: #1213 (`feat(core): add AssetType enum with conversions and
  instrument support`)
- Source: [`engine/core/instruments.py`](../../engine/core/instruments.py),
  [`engine/core/signal.py`](../../engine/core/signal.py),
  [`engine/data/providers/base.py`](../../engine/data/providers/base.py),
  [`engine/api/routes/market_data.py`](../../engine/api/routes/market_data.py)
- Related: [`docs/architecture/core-domains.md`](../architecture/core-domains.md),
  [`docs/data-model.md`](../data-model.md)
