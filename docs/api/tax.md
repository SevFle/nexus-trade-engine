# Tax report API

Base path: `/api/v1/tax`. Source:
[`engine/api/routes/tax.py`](../../engine/api/routes/tax.py),
[`engine/core/tax/`](../../engine/core/tax/).

One POST endpoint per output format. Both take the same input — a
list of jurisdiction-neutral `Disposal` rows — and dispatch to the
per-jurisdiction summariser in `engine/core/tax/reports/dispatcher.py`.

Today the dispatcher supports US, GB, DE, FR. Anything else returns
`400`.

Money values are passed as **strings** to preserve `Decimal` precision
through JSON. The route validates each value parses as a decimal
before forwarding to the dispatcher.

## Endpoints

### `POST /api/v1/tax/report/{code}`

JSON summary of disposals under one jurisdiction.

**Auth**: Bearer JWT or API key.

**Path params**: `code` — two-letter jurisdiction slug
(case-insensitive): `US`, `GB`, `DE`, `FR`.

**Request body**:

```json
{
  "disposals": [
    {
      "description": "AAPL 100 shares",
      "acquired": "2023-01-15",
      "disposed": "2024-02-10",
      "proceeds": "18250.00",
      "cost": "13400.00"
    }
  ]
}
```

**Response**: `200 OK`:

```json
{
  "jurisdiction": "US",
  "summary": {
    "short_term_gain": "1200.00",
    "long_term_gain": "3650.00",
    "total_proceeds": "18250.00",
    "total_cost_basis": "13400.00"
  }
}
```

The `summary` shape is jurisdiction-specific. US returns short/long
split + wash-sale adjustments; GB returns HMRC-sectioned gains; DE
returns Abgeltungsteuer-relevant totals; FR returns PFU plus
allowance tracking.

`400` if `code` is unsupported.

### `POST /api/v1/tax/report/{code}/csv`

Same dispatch as above, but the summary is flattened into a 2-row CSV
(header + values). Useful for spreadsheet round-trips and CPA
workflows.

**Response**: `200 OK`, `Content-Type: text/csv; charset=utf-8`:

```
Description,Acquired,Disposed,Proceeds,Cost,Gain
AAPL 100 shares,2023-01-15,2024-02-10,18250.00,13400.00,4850.00
```

`Content-Disposition: attachment; filename="tax-report-{CODE}.csv"` is
set so the browser saves it rather than rendering inline.

## Carry-over and prior-year state

US returns include §1256 carry-back and wash-sale adjustments. GB,
DE, and FR summaries do not yet include loss carry-over across tax
years — the dispatcher is stateless across calls. Operators that need
carry-over must persist prior-year summaries on their side and feed
them back in.

This is tracked in [`docs/limitations.md`](../limitations.md).

## Decimal handling

`DisposalRequest` validates `proceeds` and `cost` as decimal-shaped
strings; non-decimal strings return `400`. The route converts to
`Decimal` *before* handing to the summariser, so the summariser sees
exact values — never floats.
