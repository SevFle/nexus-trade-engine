# Tax API

Mounted at `/api/v1/tax`. Implementation: `engine/api/routes/tax.py`.
Domain logic: `engine/core/tax/reports/`.

Two endpoints that take a list of jurisdiction-neutral disposals and
return the per-jurisdiction summary, as JSON or CSV. The dispatcher
in `engine/core/tax/reports/dispatcher.py` routes `code` to the right
summariser.

Today's supported jurisdictions:

| Code | Module                                              | Report                              |
|------|-----------------------------------------------------|-------------------------------------|
| `US` | `engine/core/tax/reports/form_1099b.py`             | Form 1099-B + Schedule D            |
| `US` | `engine/core/tax/reports/carryover.py`              | Capital-loss carryover (US IRC §1212)|
| `US` | `engine/core/tax/reports/form_6781.py`              | Form 6781 (Section 1256 contracts)  |
| `GB` | `engine/core/tax/reports/cgt_carryover.py`          | HMRC CGT carryover                  |
| `DE` | `engine/core/tax/reports/` (DE placeholder)         | German Abgeltungsteuer (basic)      |
| `FR` | `engine/core/tax/reports/` (FR placeholder)         | French PFU (basic)                  |

Pass-through jurisdictions (PR, VI, etc.) use the `US` dispatcher.

## POST /report/{code}

Compute a JSON summary for a list of disposals.

**Auth** — required.

**Path** — `code` (two-letter jurisdiction slug, case-insensitive).

**Request body** `TaxReportRequest`:
```json
{
  "disposals": [
    {
      "description": "100 AAPL",
      "acquired": "2023-01-15",
      "disposed": "2024-06-30",
      "proceeds": "19500.00",
      "cost": "14200.00"
    }
  ]
}
```

Money values are **strings** to preserve `Decimal` precision through
JSON. `proceeds` and `cost` must parse as `Decimal`; the field
validator rejects anything else with a 400.

**Response**:
```json
{
  "jurisdiction": "US",
  "summary": { /* jurisdiction-specific dataclass, serialized */ }
}
```

The `summary` object's shape depends on the jurisdiction — see the
relevant module's dataclass definitions for the exact fields.
`engine/api/routes/tax.py:_to_json` walks the dataclass tree and
emits JSON-safe primitives (`Decimal` → string, `date` → ISO string,
enum → value).

**Errors** — `400 Bad Request` if `code` is unsupported or a money
field doesn't parse.

## POST /report/{code}/csv

Same dispatch as above; returns the summary as a 2-row CSV (header +
values) suitable for spreadsheet round-trips and CPA workflows.

**Response** — `200 OK` with `Content-Type: text/csv; charset=utf-8`
and `Content-Disposition: attachment;
filename="tax-report-<CODE>.csv"`.

The CSV shape is the flattened form of the JSON summary. See
`engine/core/tax/reports/dispatcher.py:flatten_summary_to_csv`.

## Notes on design

- **Stateless.** No persistence on the request body. Operators who
  want annual tax inputs stored build a separate model layer on top.
- **No multi-currency conversion.** All disposals in a request must
  be in the same currency as the proceeds/cost fields imply.
- **Carry-over is operator-managed for non-US.** US capital-loss
  carry-over is computed by the dispatcher; other jurisdictions
  return only the current-year summary and expect the operator to
  carry losses forward manually.

## Form 8949 / Schedule D / 1099-B (US)

`engine/core/tax/reports/form_1099b.py:generate_1099b_rows` produces
one row per disposal with:

- short-term vs long-term classification (12-month threshold)
- basis adjustment (wash-sale disallowed loss, if applicable — see
  `engine/core/tax/wash_sale.py`)
- realized gain / loss

Wash-sale detection runs against the prior 30 days of purchases on
the same symbol (US IRC §1091).

## Form 6781 (Section 1256 contracts)

`engine/core/tax/reports/form_6781.py` handles the 60/40 split: 60%
long-term, 40% short-term, regardless of holding period. Used for
regulated futures contracts and certain options.

## Carryover

`engine/core/tax/reports/carryover.py` applies unused capital losses
from prior years against current-year gains. The cap is
`DEDUCTIBLE_CAP_DEFAULT` ($3,000 single / $1,500 MFS) per IRC §1211.
Annual net operating loss is the input; unused losses are returned as
`CapitalLossCarryover(short_term=..., long_term=...)`.
