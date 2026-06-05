# Tax reports

Generate per-jurisdiction tax reports for realized P&L in a
portfolio. Source: [`engine/api/routes/tax.py`](../../engine/api/routes/tax.py).

The engine computes realized P&L from `tax_lot_records` (FIFO by
default; LIFO and HIFO available per portfolio config). Cost-basis
adjustments from wash sales are persisted on the lot rows
themselves — the report simply rolls them up.

## Endpoints

### `POST /api/v1/tax/report/{code}`

Render a tax report for a given jurisdiction code.

**Path:** `code` — one of `us_1040_sch_d` (Schedule D),
`us_form_8949`, `us_6781_part_ii` (Section 1256 — straddles),
`us_6781_part_iii` (Section 1256 — gains), `uk_cgt`,
`de_pfest`, `jp_carryover`. The full set is in
[`engine/core/tax/jurisdictions/`](../../engine/core/tax/jurisdictions/).

**Request body:**

```json
{
  "portfolio_id": "<uuid>",
  "tax_year": 2024,
  "cost_basis_method": "fifo"
}
```

`cost_basis_method` is `fifo` | `lifo` | `hifo`. Defaults to `fifo`
if omitted.

**Response** `200 OK`:

```json
{
  "jurisdiction": "us_1040_sch_d",
  "tax_year": 2024,
  "total_proceeds": 124500.00,
  "total_cost_basis": 87300.00,
  "total_gain": 37200.00,
  "short_term_gain": 12400.00,
  "long_term_gain": 24800.00,
  "wash_sale_deferrals": 1850.00,
  "sections": [
    {
      "label": "Part I — Short-Term",
      "rows": [
        { "description": "AAPL", "acquired": "2024-02-10",
          "sold": "2024-06-12", "proceeds": 12000.00,
          "cost_basis": 10800.00, "gain": 1200.00,
          "wash_sale": false }
      ]
    }
  ],
  "warnings": []
}
```

`warnings` may include `"missing_cost_basis"`, `"ambiguous_lot"`,
`"cross_year_wash_sale"`.

### `POST /api/v1/tax/report/{code}/csv`

Same parameters as the JSON variant, but returns a CSV suitable
for direct upload to tax-prep software.

**Response** `200 OK` — `text/csv`, `Content-Disposition:
attachment; filename="..."`.

## Supported jurisdictions

| Code               | Output                                          |
|--------------------|-------------------------------------------------|
| `us_1040_sch_d`    | Schedule D summary + line-item detail           |
| `us_form_8949`     | Form 8949 with the four box-A/B/C/D buckets     |
| `us_6781_part_ii`  | Form 6781 Part II (straddles, 60/40 treatment)  |
| `us_6781_part_iii` | Form 6781 Part III (Section 1256 contracts)     |
| `uk_cgt`           | HMRC SA108-shaped summary                       |
| `de_pfest`         | German Anlage KAP / Freistellungsauftrag summary |
| `jp_carryover`     | 損益通算 / 繰越控除 summary                       |

## Limitations

- The engine assumes the calendar year matches the tax year. For
  fiscal-year filers, manual filtering on `purchased_at` /
  `sold_at` is required until the API learns a `period` field.
- Crypto-to-fiat bridge transactions are not synthesized; if the
  lot table only has crypto-to-crypto trades the report will show
  zero realized gain.
- Wash-sale detection is per-symbol within `±30 days`. Cross-account
  wash sale (substantially identical security across brokerages) is
  out of scope until the engine aggregates multiple brokerage feeds.
