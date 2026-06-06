# Tax API

Per-jurisdiction tax reporting. Implementation:
[`engine/api/routes/tax.py`](../../engine/api/routes/tax.py),
dispatch + summarisers: [`engine/core/tax/reports.py`](../../engine/core/tax/reports.py).

The engine is jurisdiction-neutral: callers POST a list of `Disposal`s
(proceeds, cost, dates, description) plus a two-letter jurisdiction
code. The dispatcher picks the right summariser and returns the
summary as a JSON dict or as a CSV attachment.

Why one endpoint instead of one per jurisdiction: the dispatcher
already switches on the code, and one endpoint avoids duplicating that
switch in the URL space.

## Endpoint summary

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/api/v1/tax/report/{code}`        | JWT | JSON summary |
| `POST` | `/api/v1/tax/report/{code}/csv`    | JWT | Same summary, CSV attachment |

## Path parameter

| Param | Pattern | Notes |
|---|---|---|
| `code` | `^[a-zA-Z]{2}$` (case-insensitive) | Jurisdiction. Currently supported: `US`, `GB`, `DE`, `FR`. |

## Schemas

```python
class DisposalRequest(BaseModel):
    description: str            # 1-200 chars
    acquired: date              # ISO-8601
    disposed: date              # ISO-8601
    proceeds: str               # Decimal as string (preserves precision)
    cost: str                   # Decimal as string

class TaxReportRequest(BaseModel):
    disposals: list[DisposalRequest] = []
```

The response shape is jurisdiction-specific. The route returns the
serialised dataclass tree as a JSON dict:

```json
{
  "jurisdiction": "US",
  "summary": { ... }   // US-specific shape
}
```

`Decimal`s are stringified (preserves precision through JSON),
`date`s become ISO strings, `Enum`s become their `.value`.

## CSV

`POST /api/v1/tax/report/{code}/csv` returns the same dispatch but as
a 2-row CSV (header + values) with
`Content-Disposition: attachment; filename="tax-report-US.csv"`.
Useful for handing off to a CPA. The implementation lives in
[`engine/core/tax/reports.py:flatten_summary_to_csv`](../../engine/core/tax/reports.py).

## Examples

```bash
# JSON
curl -X POST http://localhost:8000/api/v1/tax/report/US \
  -H 'authorization: Bearer <access>' \
  -H 'content-type: application/json' \
  -d '{
        "disposals": [
          {"description":"AAPL 100 sh",
           "acquired":"2023-02-01","disposed":"2024-03-15",
           "proceeds":"19500.00","cost":"15000.00"}
        ]
      }'

# CSV
curl -X POST http://localhost:8000/api/v1/tax/report/US/csv \
  -H 'authorization: Bearer <access>' \
  -H 'content-type: application/json' \
  -d '{"disposals":[...]}' -o tax-US.csv
```

## What the summarisers cover

| Code | Long-term / short-term | Wash-sale | Carry-over | Forms |
|------|------------------------|-----------|------------|-------|
| `US` | ✓ (12-month threshold) | ✓ (gh#158) | CGT carry-forward | 1099-B, 6781, Schedule D, §1256 carryback |
| `GB` | ✓ (30-day matching)    | —         | —          | —     |
| `DE` | ✓                      | —         | —          | —     |
| `FR` | ✓                      | —         | —          | —     |

Carry-over state is *per-jurisdiction* and **not persisted** in v1 —
the engine recomputes from the disposals you give it. Operators that
want persistent carry-forward can wrap this endpoint and store the
outputs themselves. See [`../known-limitations.md`](../known-limitations.md).

## Errors

| Status | When |
|---|---|
| `400` | Unknown jurisdiction `code`; `proceeds` / `cost` not a valid decimal; Pydantic validation failure. |
| `401` | Missing/invalid token. |

## Tests

The tax code is heavily tested — see
[`tests/test_tax_dispatcher.py`](../../tests/test_tax_dispatcher.py),
[`tests/test_tax_lots.py`](../../tests/test_tax_lots.py),
[`tests/test_wash_sale.py`](../../tests/test_wash_sale.py),
[`tests/test_schedule_d.py`](../../tests/test_schedule_d.py),
[`tests/test_form_1099b.py`](../../tests/test_form_1099b.py),
[`tests/test_form_6781*.py`](../../tests/), and
[`tests/test_section_1256_carryback.py`](../../tests/test_section_1256_carryback.py).
Touching the dispatcher requires updating both the dispatcher test and
the per-jurisdiction summariser test.
