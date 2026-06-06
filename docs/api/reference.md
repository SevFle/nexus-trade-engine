# Reference API (instrument search)

Typeahead-friendly instrument search. Implementation:
[`engine/api/routes/reference.py`](../../engine/api/routes/reference.py),
local search: [`engine/reference/search.py`](../../engine/reference/search.py).

The endpoint first hits the local `SearchIndex` (seeded at startup from
[`engine/reference/seed.py`](../../engine/reference/seed.py)). If the
local index has no matches, it falls through to Yahoo's public search
API and merges those results. Yahoo results are tagged with the
inferred asset class (equity / ETF / crypto / forex / etc.).

## Endpoint summary

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/api/v1/reference/suggest` | none | Typeahead suggestions |

This endpoint is intentionally unauthenticated so the typeahead box in
the React dashboard works pre-login (where the user is searching for
a ticker to explore before signing up). No user data is returned; the
output is purely instrument metadata.

## Query parameters

| Param         | Default | Constraints | Purpose |
|---------------|---------|-------------|---------|
| `q`           | —       | non-empty, ≤ 64 chars | Search query |
| `limit`       | 10      | 1-50        | Max suggestions |
| `asset_class` | unset   | one of `equity`, `etf`, `crypto`, `forex`, `future`, `option`, `mutualfund`, `index` | Optional filter |

## Schemas

```python
class Suggestion(BaseModel):
    symbol: str         # e.g. "AAPL"
    name: str           # e.g. "Apple Inc."
    display: str        # "AAPL — Apple Inc."
    completion: str     # what to show in the dropdown
    score: float        # 0-100, higher is better
    record: InstrumentRecord
```

`InstrumentRecord` is the canonical shape from
[`engine/reference/model.py`](../../engine/reference/model.py):

```python
class InstrumentRecord(BaseModel):
    id: UUID
    primary_ticker: str
    primary_venue: str
    asset_class: str
    name: str
    currency: str
```

## Examples

```bash
# Local match
curl 'http://localhost:8000/api/v1/reference/suggest?q=apple&limit=5'

# Filtered
curl 'http://localhost:8000/api/v1/reference/suggest?q=bitcoin&asset_class=crypto'

# Falls through to Yahoo
curl 'http://localhost:8000/api/v1/reference/suggest?q=NVDA'
```

## Local seed

At startup the engine calls `seed_index(index)` to populate the local
search index from a curated list in
[`engine/reference/seed.py`](../../engine/reference/seed.py). The seed
covers major US equities + ETFs; for everything else the route falls
through to Yahoo.

## Yahoo fallback

`_yahoo_search` issues a `GET
https://query2.finance.yahoo.com/v1/finance/search` with the user's
query. Failures (timeout, network error) are logged at `warning` and
return an empty list — the route never 5xx on Yahoo's behalf.

## Errors

| Status | When |
|---|---|
| `400` | Empty query; query > 64 chars. |
| (no 5xx) | Yahoo upstream failure is swallowed; the route returns `{suggestions: []}`. |

## Related

- [Market data API](market-data.md) — once you have a symbol.
- [`engine/reference/`](../../engine/reference/) — model, search,
  classification.
