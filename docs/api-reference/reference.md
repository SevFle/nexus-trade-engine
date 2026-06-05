# Reference data (instrument search)

Symbol suggest for the UI's typeahead. Source:
[`engine/api/routes/reference.py`](../../engine/api/routes/reference.py),
[`engine/reference/`](../../engine/reference/).

The index is seeded on app start by
[`engine/reference/seed.py`](../../engine/reference/seed.py) and
lives in process memory. The Yahoo seed (~10 000 tickers) is the
default; operators can swap in their own seed file via
`NEXUS_REFERENCE_SEED_PATH`.

## Endpoints

### `GET /api/v1/reference/suggest`

Typeahead query against the in-memory instrument index. Public.

**Query params:**

| Param    | Type   | Default | Notes                                  |
|----------|--------|---------|----------------------------------------|
| `q`      | string | required | Prefix search. Min length 1.          |
| `limit`  | int    | 10      | Capped at 50.                          |
| `type`   | string | —       | Filter: `stock`, `etf`, `crypto`, etc. |
| `exchange` | string | —     | Filter by MIC code (e.g. `XNYS`).      |

**Response** `200 OK`:

```json
{
  "query": "app",
  "results": [
    { "symbol": "AAPL", "name": "Apple Inc.",
      "type": "stock", "exchange": "XNAS", "country": "US" },
    { "symbol": "APP", "name": "AppLovin Corp.",
      "type": "stock", "exchange": "XNAS", "country": "US" }
  ]
}
```

## Index internals

- The index is built once at app start. Updates require a restart
  (or a manual call to `seed_index(get_search_index())`). A
  hot-reload endpoint is on the roadmap.
- Search is prefix-anchored on `symbol`; the `name` field is
  scored with a simple weighted-levenshtein to break ties.
- The index lives in
  [`engine/reference/search.py`](../../engine/reference/search.py).
  For very large universes (> 100k instruments) consider replacing
  it with a Tantivy or Tries-based backing store.
