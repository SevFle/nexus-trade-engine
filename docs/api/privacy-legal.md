# Privacy (GDPR / CCPA), Legal Acceptance, Tax Reports

> **Base paths:** `/api/v1/privacy`, `/api/v1/legal`, `/api/v1/tax`
>
> **Source:**
> [`engine/api/routes/privacy.py`](../../engine/api/routes/privacy.py),
> [`engine/api/routes/legal.py`](../../engine/api/routes/legal.py),
> [`engine/api/routes/tax.py`](../../engine/api/routes/tax.py),
> [`engine/privacy/`](../../engine/privacy/),
> [`engine/legal/`](../../engine/legal/),
> [`engine/core/tax/`](../../engine/core/tax/)

## Privacy — `/api/v1/privacy`

Data Subject Request (DSR) endpoints implementing GDPR Articles 15–21
and CCPA's analogous rights. Every DSR creates a `DSRequest` row with
an SLA due-date (30 days under GDPR Art. 12(3)). The shape is in
[`engine/db/models.py:DSRequest`](../../engine/db/models.py).

| Method | Path | Behaviour |
|--------|------|-----------|
| `POST` | `/export` | Synchronous export of every row tied to the caller, wrapped in an `ExportResponse`. The request itself is recorded as a completed DSR. |
| `POST` | `/delete` | Initiate account deletion. Returns **202** `DeletionStatusResponse` with `sla_due_at`. There is a 30-day grace window during which the user can cancel; actual hard-delete is performed by a scheduled job. **409** if a deletion is already pending. |
| `POST` | `/delete/cancel` | Cancel during the grace window. **404** if no pending deletion. |
| `GET` | `/delete/status` | Returns `{ pending, sla_due_at, request: null }`. |
| `GET` | `/requests` | Lists the caller's full DSR history. |
| `GET` | `/kinds` | `{ kinds: [...] }` — allow-list for clients. |

`DSR_KINDS` (in [`engine/privacy/__init__.py`](../../engine/privacy/__init__.py))
is the source of truth for what we treat as a valid DSR.

### Export shape

`collect_user_data` in
[`engine/privacy/export.py`](../../engine/privacy/export.py) walks every
table that has a `user_id` (or implicit user tie, like
`BacktestResult` via `portfolio_id`) and assembles a dict keyed by
table name. **Orphan rows are included via outer-join** (gh#157) so an
export is complete even if a portfolio was deleted mid-flight.

### Deletion behaviour

`request_deletion` in
[`engine/privacy/deletion.py`](../../engine/privacy/deletion.py) writes
a `DSRequest(kind="delete", status="pending", sla_due_at=...)`. The
scheduled job (in
[`engine/data/retention_cleanup.py`](../../engine/data/retention_cleanup.py))
sweeps `pending` rows past their grace window and hard-deletes the
user, cascading through `ON DELETE CASCADE` FKs.

> **Limitation:** there is no admin-side UI for DSRs that arrive out
> of band (postal letter, support email). Today an operator has to
> insert a `DSRequest` row by hand. Tracked in [../limitations.md](../limitations.md).

## Legal acceptance — `/api/v1/legal`

Legal documents (Terms, Privacy Policy, Disclaimer, etc.) live in
[`legal/`](../../legal/) as Markdown files with YAML front matter. They
are synced into the `legal_documents` table at engine startup by
[`engine/legal/sync.py`](../../engine/legal/sync.py). The sync is
idempotent: re-running it only inserts new versions.

| Method | Path | Behaviour |
|--------|------|-----------|
| `GET` | `/api/v1/legal/documents` | Lists all current documents, optionally filtered by `category`. Each item includes `accepted_by_user` (bool) when the request carries a valid Bearer token. |
| `GET` | `/api/v1/legal/documents/{slug}` | Full document body in Markdown after template substitution. Query `version` for a specific version. |
| `POST` | `/api/v1/legal/accept` | `AcceptRequest { document_slug, document_version }`. Records an acceptance row (immutable — see migration `006`). |
| `GET` | `/api/v1/legal/acceptances/me` | Lists the caller's acceptance history. |
| `GET` | `/api/v1/legal/attributions` | Data-provider attribution texts required by some vendors (Yahoo's "Data by Yahoo Finance", etc.). |

### Substitution placeholders

Document Markdown may contain these placeholders; the route replaces
them on read (not on write — the source file stays unmodified):

| Placeholder | Substituted from |
|-------------|------------------|
| `{{OPERATOR_NAME}}` | `NEXUS_OPERATOR_NAME` |
| `{{OPERATOR_EMAIL}}` | `NEXUS_OPERATOR_EMAIL` |
| `{{OPERATOR_URL}}` | `NEXUS_OPERATOR_URL` |
| `{{JURISDICTION}}` | `NEXUS_JURISDICTION` |
| `{{PLATFORM_FEE_PERCENT}}` | `NEXUS_PLATFORM_FEE_PERCENT` |
| `{{EFFECTIVE_DATE}}` | the document's `effective_date` column |

Markdown special characters in the substituted values are escaped — see
`_escape_markdown` in [`engine/api/routes/legal.py`](../../engine/api/routes/legal.py).

### `require_legal_acceptance` dependency

Most domain routes (`/portfolio`, `/strategies`, `/backtest`,
`/scoring`, `/marketplace`, `/market-data`, `/webhooks`) carry this
dependency. It checks that the caller has accepted every
`requires_acceptance=true` document at its current version. If not,
the route returns **403** with `detail: "legal_acceptance_required"`
and a body listing the missing slugs + versions.

This is **defense-in-depth** — the legal doc system does not enforce
financial regulations on its own. Operators are still responsible for
showing the right document at the right time in their UI.

## Tax reports — `/api/v1/tax`

Single dispatcher endpoint that takes jurisdiction-neutral disposals
and returns a per-jurisdiction summary. The actual aggregation lives
in [`engine/core/tax/reports/dispatcher.py`](../../engine/core/tax/reports/dispatcher.py).

| Method | Path | Behaviour |
|--------|------|-----------|
| `POST` | `/report/{code}` | JSON summary for jurisdiction `code`. `code` is case-insensitive, two letters. |
| `POST` | `/report/{code}/csv` | Same dispatch, returns a 2-row CSV (header + values). |

### Supported jurisdictions

| Code | Summariser | Forms / reports |
|------|------------|-----------------|
| `US` | [`engine/core/tax/jurisdictions/us.py`](../../engine/core/tax/jurisdictions/us.py) | Form 8949 / Schedule D, §1256 carryback, wash-sale adjustments, KEST estimation. |
| `GB` | [`engine/core/tax/jurisdictions/gb.py`](../../engine/core/tax/jurisdictions/gb.py) | HMRC CGT, BED-and-Breakfast rules. |
| `DE` | [`engine/core/tax/jurisdictions/de.py`](../../engine/core/tax/jurisdictions/de.py) | Vorabpauschale / Teilfreistellung. |
| `FR` | [`engine/core/tax/jurisdictions/fr.py`](../../engine/core/tax/jurisdictions/fr.py) | PFU (flat tax) + CGT carryover. |

Anything else returns **400** with `UnsupportedJurisdictionError`.

### Request shape

```json
{
  "disposals": [
    {
      "description": "100 AAPL",
      "acquired": "2024-03-01",
      "disposed": "2025-04-12",
      "proceeds": "19500.00",
      "cost": "14250.00"
    }
  ]
}
```

> **Money is sent as strings.** JSON numbers lose `Decimal` precision
> through most parsers; round-tripping as strings is the only safe
> shape. The route validates with `Decimal(value)` and rejects invalid
> strings with 400.

### Response shape

```json
{
  "jurisdiction": "US",
  "summary": { /* jurisdiction-specific dataclass as JSON */ }
}
```

The shape of `summary` depends on the jurisdiction; see the test
fixtures under [`tests/test_form_*`](../../tests/) for canonical
examples. The `_to_json` helper at the bottom of
[`engine/api/routes/tax.py`](../../engine/api/routes/tax.py) handles
`Decimal` → string, `date` → ISO, `Enum` → value.

### Carry-over state

Only US carry-over (`cgt_carryover.py`, wash-sale loss disallowance)
is currently persisted across requests. GB / DE / FR are stateless —
the caller re-submits the prior year's summary if they need carryover
to flow into the current year. Tracked as a P2 limitation.
