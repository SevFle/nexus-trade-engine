# Privacy / DSR API

GDPR / CCPA data-subject-request routes. Implementation:
[`engine/api/routes/privacy.py`](../../engine/api/routes/privacy.py),
service layer: [`engine/privacy/`](../../engine/privacy/).

A DSR (Data Subject Request) is one of `export`, `delete`, `rectify`,
`restrict`, `object`. The engine supports `export` and `delete` today;
the rest are recorded but not acted on (see
[`../known-limitations.md`](../known-limitations.md)).

Each request creates a row in `dsr_requests` with an `sla_due_at`
timestamp (default 30 days, per GDPR Art. 12). The `completed_at` and
`cancelled_at` columns record the lifecycle.

## Endpoint summary

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/api/v1/privacy/export`            | JWT | Synchronous export of caller's data |
| `POST` | `/api/v1/privacy/delete`            | JWT | Initiate account deletion (30-day grace) |
| `POST` | `/api/v1/privacy/delete/cancel`     | JWT | Cancel deletion during the grace window |
| `GET`  | `/api/v1/privacy/delete/status`      | JWT | Pending? + remaining grace |
| `GET`  | `/api/v1/privacy/requests`           | JWT | List caller's DSR history |
| `GET`  | `/api/v1/privacy/kinds`              | none | Allow-list of valid `kind` values |

## Schemas

```python
class DSRRequestSummary(BaseModel):
    id: UUID
    kind: str                   # export|delete|rectify|restrict|object
    status: str                 # pending|completed|cancelled
    note: str | None
    sla_due_at: datetime
    completed_at: datetime | None
    cancelled_at: datetime | None
    created_at: datetime

class ExportResponse(BaseModel):
    request: DSRRequestSummary
    data: dict[str, Any]        # all rows belonging to the user

class DeletionRequestBody(BaseModel):
    note: str | None = None     # ≤ 4000 chars

class DeletionStatusResponse(BaseModel):
    pending: bool
    sla_due_at: datetime | None
    request: DSRRequestSummary | None
```

## What `export` returns

`collect_user_data` in [`engine/privacy/export.py`](../../engine/privacy/export.py)
walks every table that has a `user_id` (or `portfolio_id`-owned) FK
and serialises the user's rows into a single nested dict. The
top-level keys are table names (`users`, `portfolios`, `orders`,
`positions`, `tax_lot_records`, `installed_strategies`,
`backtest_results`, `webhook_configs`, `webhook_deliveries`,
`api_keys`, `legal_acceptances`, `dsr_requests`, `refresh_tokens`).

`backtest_results` is included via an outer-join so orphaned rows
(where `portfolio_id IS NULL`) are still exported — see gh#157.

Sensitive fields are redacted:

- `users.hashed_password`, `users.mfa_secret_encrypted`,
  `users.mfa_backup_codes`
- `webhook_configs.signing_secret`
- `api_keys.key_hash`
- `refresh_tokens.token_hash`

## Deletion lifecycle

```
submitted ──▶ pending ──▶ (cancel window: 30 days) ──▶ executed
                    │
                    └──▶ cancelled
```

The grace window is enforced via `sla_due_at`. During the window:

- The user can still log in (their account is *not* locked).
- `GET /delete/status` shows time remaining.
- `POST /delete/cancel` flips the row to `cancelled`.

After `sla_due_at` passes, a scheduled task (planned, see
[`../known-limitations.md`](../known-limitations.md)) performs the
actual row deletion. **Today this requires operator intervention** —
the cron that runs the deletion does not exist yet.

Calling `POST /delete` twice while a request is already pending
returns `409 Conflict`.

## Examples

```bash
# Export
curl -X POST http://localhost:8000/api/v1/privacy/export \
  -H 'authorization: Bearer <access>' > export.json

# Request deletion
curl -X POST http://localhost:8000/api/v1/privacy/delete \
  -H 'authorization: Bearer <access>' \
  -H 'content-type: application/json' \
  -d '{"note":"leaving the platform"}'

# Status check
curl http://localhost:8000/api/v1/privacy/delete/status \
  -H 'authorization: Bearer <access>'

# Cancel during grace window
curl -X POST http://localhost:8000/api/v1/privacy/delete/cancel \
  -H 'authorization: Bearer <access>'

# History
curl http://localhost:8000/api/v1/privacy/requests \
  -H 'authorization: Bearer <access>'

# What kinds are accepted?
curl http://localhost:8000/api/v1/privacy/kinds
# => {"kinds": ["delete", "export", "object", "rectify", "restrict"]}
```

## Errors

| Status | When |
|---|---|
| `400` | `note` > 4000 chars on `/delete`. |
| `401` | Missing/invalid token. |
| `404` | `/delete/cancel` called when no pending deletion exists. |
| `409` | `/delete` called twice while first request is still pending. |

## Related

- [`docs/operations/backup-and-recovery.md`](../operations/backup-and-recovery.md)
  — what to restore if a deletion ran by mistake.
