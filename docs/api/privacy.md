# Privacy / DSR API

Mounted at `/api/v1/privacy`. Implementation:
`engine/api/routes/privacy.py`. Domain logic: `engine/privacy/`.

GDPR / CCPA data-subject request handling. Four kinds are recognised
(`engine/privacy/__init__.py:DSR_KINDS`):

- `export`
- `delete`
- `rectify` (record-only; not yet implemented as a workflow)
- `restrict` (record-only)
- `object` (record-only)

Every request creates a `DSRequest` row with an SLA due-date
(`sla_due_at`) computed from the GDPR Art. 12 one-month window.

## POST /export

Synchronous export of the caller's data. Returns a JSON object
containing every row that references the user.

**Auth** — required.

**Response** `ExportResponse`:
```json
{
  "request": { /* DSRRequestSummary */ },
  "data": {
    "user": {...},
    "portfolios": [...],
    "orders": [...],
    "positions": [...],
    "tax_lot_records": [...],
    "backtest_results": [...],
    "webhook_configs": [...],
    "webhook_deliveries": [...],
    "scoring_snapshots": [...],
    "legal_acceptances": [...],
    "api_keys": [{ "prefix": "nxs_live_aB3", "scopes": ["read"], ... }]
  }
}
```

The export intentionally orphans nothing — `engine/privacy/export.py`
uses an outer join to include `BacktestResult` rows whose
`portfolio_id` is null (gh#157). API key hashes are not returned;
only the prefix, scope, and metadata.

A `DSRequest` row is recorded with `kind: "export"` and
`status: "completed"` in the same transaction.

## POST /delete

Initiate account deletion. Returns immediately with the grace
window; the actual hard-delete happens after
`sla_due_at` unless cancelled.

**Auth** — required.

**Request body** `DeletionRequestBody`:
```json
{ "note": "switching to self-hosted" }
```

`note` is optional, max 4000 chars. Stored on the `DSRequest` row.

**Response** `DeletionStatusResponse` (202):
```json
{
  "pending": true,
  "sla_due_at": "2026-07-05T12:00:00Z",
  "request": { /* DSRRequestSummary */ }
}
```

**Errors** — `409 Conflict` if a deletion is already pending.

## POST /delete/cancel

Cancel a pending deletion within the grace window.

**Response** `DeletionStatusResponse`:
```json
{ "pending": false, "sla_due_at": null, "request": { /* marked cancelled */ } }
```

**Errors** — `404` if there is no pending deletion.

## GET /delete/status

Check the current deletion status without modifying it.

**Response** `DeletionStatusResponse` — `request` is `null` on this
endpoint (the row isn't loaded).

## GET /requests

List the caller's DSR history, oldest-first.

**Response** `DSRListResponse`:
```json
{
  "requests": [
    {
      "id": "uuid",
      "kind": "export",
      "status": "completed",
      "note": null,
      "sla_due_at": "...",
      "completed_at": "...",
      "cancelled_at": null,
      "created_at": "..."
    }
  ]
}
```

## GET /kinds

Returns the allow-list of DSR kinds. OpenAPI clients use this to
validate `kind` against the server's vocabulary.

**Response** — `{"kinds": ["delete", "export", "object", "rectify",
"restrict"]}`.

## Notes

- The hard-delete job is operator-triggered via the scheduled
  retention worker (`engine/data/retention.py`). The grace period is
  enforced by `sla_due_at`; cancellations are honoured up until the
  row is actually deleted.
- `rectify` / `restrict` / `object` kinds are recorded but not
  workflowed — they exist for audit-trail completeness when an
  operator handles an out-of-band request.
- The 30-day grace window matches GDPR's one-month SLA. Operators in
  jurisdictions without a statutory SLA can shorten it by setting an
  earlier `sla_due_at` directly in the DB.
