# Reference data API

Base path: `/api/v1/reference`. Source:
[`engine/api/routes/reference.py`](../../engine/api/routes/reference.py),
[`engine/reference/`](../../engine/reference/).

Instrument typeahead. Used by the dashboard's symbol picker.

## Endpoint

### `GET /api/v1/reference/suggest`

Return matching instruments for a free-form query. Two-stage lookup:

1. **Local index** — `engine.reference.search.SearchIndex`, seeded at
   startup from a static corpus in `engine/reference/seed.py`.
2. **Yahoo Finance search** — fall-back when the local index returns
   nothing. Calls `https://query2.finance.yahoo.com/v1/finance/search`
   with a 5 s timeout. Yahoo results are mapped through a
   `quoteType → asset_class` table.

The fall-back is best-effort: any network error returns an empty list,
never a 5xx.

**Auth**: none.

**Query params**:

| Name          | Type   | Default | Notes                                         |
|---------------|--------|---------|-----------------------------------------------|
| `q`           | string | required| 1–`SearchIndex.MAX_QUERY_LEN` chars.          |
| `limit`       | int    | 10      | Capped at 50.                                 |
| `asset_class` | string | null    | Filter to one asset class (e.g. `crypto`).    |

**Response**: `200 OK`:

```json
{
  "suggestions": [
    {
      "symbol": "AAPL",
      "name": "Apple Inc.",
      "display": "AAPL — Apple Inc.",
      "completion": "Apple Inc.",
      "score": 100,
      "record": {
        "id": "uuid",
        "primary_ticker": "AAPL",
        "primary_venue": "NASDAQ",
        "asset_class": "equity",
        "name": "Apple Inc.",
        "currency": "USD"
      }
    }
  ]
}
```

`400` if `q` is empty or exceeds the length cap.

## Notes

- The local index is a process singleton (`get_search_index()`). Tests
  inject a seeded fixture via FastAPI dependency overrides.
- The Yahoo fall-back respects the operator-supplied `User-Agent`
  `nexus-trade-engine/1.0`. Operators seeing 403s from Yahoo should
  verify their network path and Yahoo's terms of service.
- The `record.id` field is empty for Yahoo-sourced hits (we don't
  have a stable local id for them). Local hits always carry a UUID.
