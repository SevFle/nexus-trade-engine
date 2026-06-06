# Privacy / DSR API

Base path: `/api/v1/privacy`. Source:
[`engine/api/routes/privacy.py`](../../engine/api/routes/privacy.py),
[`engine/privacy/`](../../engine/privacy/).

GDPR / CCPA data-subject request surface. Lets the caller export
their data, request account deletion, and audit the request history.
Every request produces an audited `dsr_requests` row with an SLA
clock attached.

## DSR kinds

| Kind       | Meaning                                                | Currently implemented |
|------------|--------------------------------------------------------|------------------------|
| `export`   | Download all data the engine holds for the caller.    | Yes (synchronous JSON).|
| `delete`   | Request hard-delete of the caller's account.          | Yes (30-day grace).    |
| `rectify`  | Request correction of inaccurate data.                | No (manual).           |
| `restrict` | Request processing restriction.                       | No (manual).           |
| `object`   | Object to specific processing.                        | No (manual).           |

`GET /api/v1/privacy/kinds` returns the allow-list.

## Endpoints

### `POST /api/v1/privacy/export`

Synchronous export of the caller's data. Persists a `dsr_requests`
row, walks every user-owned table, and returns the assembled JSON.

**Auth**: Bearer JWT or API key.

**Response**: `200 OK`:

```json
{
  "request": {
    "id": "uuid",
    "kind": "export",
    "status": "completed",
    "note": null,
    "sla_due_at": "2026-07-06T12:00:00Z",
    "completed_at": "2026-06-06T12:00:00Z",
    "cancelled_at": null,
    "created_at": "2026-06-06T12:00:00Z"
  },
  "data": {
    "user": { "id": "uuid", "email": "...", "...": "..." },
    "portfolios": [...],
    "backtest_results": [...],
    "webhook_configs": [...],
    "legal_acceptances": [...],
    "api_keys": [...]
  }
}
```

The `data` payload includes orphaned `BacktestResult` rows via an
outer-join (gh#157) so legacy data without a `portfolio_id` is
included.

### `POST /api/v1/privacy/delete`

Schedule account deletion. Sets a 30-day grace window during which
the caller can cancel. After the window expires, a separate sweeper
job performs the hard delete (or the operator runs the cleanup
manually — the sweeper is operator-deployed today).

**Request body** (optional):

```json
{ "note": "User requested via in-app flow." }
```

**Response**: `202 Accepted`:

```json
{
  "pending": true,
  "sla_due_at": "2026-07-06T12:00:00Z",
  "request": { "...": "see DSRRequestSummary" }
}
```

`409` if a deletion is already pending.

### `POST /api/v1/privacy/delete/cancel`

Cancel a pending deletion. No-op (returns `404`) if there is no
pending request.

**Response**: `200 OK`:

```json
{
  "pending": false,
  "sla_due_at": null,
  "request": { "...": "DSRRequestSummary with status=cancelled" }
}
```

### `GET /api/v1/privacy/delete/status`

Poll the deletion state without listing every DSR.

**Response**: `200 OK`:

```json
{
  "pending": false,
  "sla_due_at": null,
  "request": null
}
```

### `GET /api/v1/privacy/requests`

List every DSR the caller has ever filed, newest first. Used by the
in-app "my privacy requests" page.

**Response**: `200 OK`:

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

### `GET /api/v1/privacy/kinds`

Static allow-list of DSR kinds. Useful for OpenAPI clients that want
to validate input without hard-coding the set.

**Auth**: none.

**Response**: `200 OK`:

```json
{ "kinds": ["delete", "export", "object", "rectify", "restrict"] }
```

## SLA enforcement

Every DSR row carries an `sla_due_at` set to `created_at + 30 days`
(GDPR Art. 12(3)). The engine itself does not auto-resolve overdue
requests — it surfaces the field so operator tooling (or a future
sweeper) can. Operators should monitor for `WHERE status='pending'
AND sla_due_at < now()` and act.

## Notes

- Deletion is irreversible. The 30-day window is the only safety net.
- The export response can be large (megabytes) for power users. The
  body-size limit middleware (1 MiB cap on *inbound* requests) does
  not apply to outbound responses, but reverse proxies with default
  caps may truncate; configure yours accordingly.
- Async tarball downloads with signed URLs are an explicit follow-up;
  see `routes/privacy.py` module docstring.
