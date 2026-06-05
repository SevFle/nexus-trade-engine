# Reference / instrument search API

Mounted at `/api/v1/reference`. Implementation:
`engine/api/routes/reference.py`. Search index:
`engine/reference/search.py`. Static seed: `engine/reference/seed.py`.

Typeahead endpoint that returns matching instruments by symbol or
name. The implementation tries the Yahoo Finance search API first
(5s timeout) and falls back to a local in-memory `SearchIndex` if
Yahoo is unreachable or returns no results.

## GET /suggest

Typeahead query.

**Auth** — none required.

**Query params**

| Param         | Type   | Default | Notes                                  |
|---------------|--------|---------|----------------------------------------|
| `q`           | string | required| 1–128 chars                            |
| `limit`       | int    | 10      | 1–50                                   |
| `asset_class` | string | null    | Filter by `AssetClassLiteral`          |

**Response** — list of suggestions:
```json
[
  {
    "symbol": "AAPL",
    "name": "Apple Inc.",
    "display": "AAPL — Apple Inc.",
    "completion": "Apple Inc.",
    "score": 100,
    "record": {
      "id": "...",
      "primary_ticker": "AAPL",
      "primary_venue": "Nasdaq",
      "asset_class": "equity",
      "name": "Apple Inc.",
      "currency": "USD"
    }
  }
]
```

## Search backend

`engine/reference/search.py:SearchIndex` is a process-singleton
initialized at app startup (`engine/app.py:_seed_reference_index`).
The seed data is loaded by `engine/reference/seed.py:seed_index`,
which ships a static set of popular US equities + ETFs and is
idempotent (no-op if the index already has records).

The local index is the source of truth when Yahoo is unreachable;
Yahoo results are merged in when available. There is no persistence
— the index is rebuilt on every cold start.

## Yahoo fallback

`engine/api/routes/reference.py:_yahoo_search` queries
`https://query2.finance.yahoo.com/v1/finance/search` with a 5s
timeout and user agent `nexus-trade-engine/1.0`. Failures are
logged and silently fall through to the local index.

`quoteType` mapping:

| Yahoo `quoteType` | Engine `asset_class` |
|-------------------|----------------------|
| `EQUITY`          | `equity`             |
| `ETF`             | `etf`                |
| `MUTUALFUND`      | `etf`                |
| `CRYPTOCURRENCY`  | `crypto`             |
| `CURRENCY`        | `forex`              |
| `INDEX`           | `etf`                |
| `FUTURE`          | `future`             |
| `OPTION`          | `option`             |
| (other)           | `equity`             |

## OpenFIGI ingestion

`engine/reference/ingestion/openfigi.py` provides a client for the
OpenFIGI API used to enrich the local index with FIGI identifiers.
The ingestion path is not wired to a scheduled job today; operators
who want fresh FIGI data call it from a one-off task.
