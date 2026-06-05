# API keys API

Mounted at `/api/v1/auth/api-keys`. Implementation:
`engine/api/routes/api_keys.py`. Issuance logic:
`engine/api/auth/api_keys.py`.

Long-lived scoped tokens for headless automation, CI, and SDK clients.
Distinct from JWT: API keys are bcrypt-hashed at rest, identified by
their first 12 chars (`prefix`), and enforce a separate scope
vocabulary.

## Token format

```
nxs_<env>_<random>
```

- `nxs_` — fixed prefix used by `engine.api.auth.api_keys.is_engine_token`
  to dispatch at the auth boundary.
- `<env>` — operator-chosen label (default `live`). Alphanumeric +
  underscore. Useful for distinguishing `live` / `test` / `ci` keys.
- `<random>` — 32 hex characters (~128 bits of entropy).

The plaintext token is returned **exactly once** in the `POST`
response. The DB stores only the bcrypt hash + the 12-char prefix
used for human-readable identification.

## POST /auth/api-keys

Issue a new key. The plaintext token appears in the response under
`token`; persist it client-side — it cannot be retrieved again.

**Auth** — required.

**Request body** `ApiKeyCreateRequest`:
```json
{
  "name": "CI backtest runner",
  "scopes": ["trade"],
  "expires_at": "2026-12-31T00:00:00Z",
  "env": "ci"
}
```

| Field        | Type     | Default    | Notes                                           |
|--------------|----------|------------|-------------------------------------------------|
| `name`       | string   | required   | 1–255 chars                                     |
| `scopes`     | string[] | `["read"]` | Subset of `{read, trade, admin}`                |
| `expires_at` | datetime | null       | Null = no expiry                                 |
| `env`        | string   | `"live"`   | Alphanumeric + underscore, max 16 chars         |

**Response** `ApiKeyCreatedResponse` (201):
```json
{
  "id": "uuid",
  "name": "CI backtest runner",
  "prefix": "nxs_ci_aB3",
  "scopes": ["trade"],
  "last_used_at": null,
  "expires_at": "2026-12-31T00:00:00Z",
  "revoked_at": null,
  "created_at": "2026-06-05T12:00:00Z",
  "token": "nxs_ci_aB3deadbeef...fulltoken"
}
```

**Errors** — `400` if any scope is outside the allow-list.

## GET /auth/api-keys

List the caller's keys. The plaintext token is never returned.

**Auth** — required.

**Response** — `list[ApiKeySummary]` (same as above without `token`).

Rows are ordered by `created_at DESC`. Revoked keys are included for
audit; they are not filtered out.

## DELETE /auth/api-keys/{key_id}

Revoke a key. Idempotent — revoking an already-revoked key returns
204 with no state change.

**Auth** — required. Caller must own the key.

**Response** — `204 No Content`.

**Errors** — `404` if the key does not exist or belongs to another
user (we don't leak existence via 403).

## Scope semantics

| Scope   | Allows                                                |
|---------|-------------------------------------------------------|
| `read`  | GET-only routes                                       |
| `trade` | POST/PUT/PATCH to backtest, portfolio, webhooks, etc. |
| `admin` | Everything; treated as equivalent to the `admin` role |

Hierarchy: `admin > trade > read`. A scope satisfies a requirement if
any granted scope is at or above the required level
(`_SCOPE_HIERARCHY` in `engine/api/auth/dependency.py`).

JWT-authenticated sessions bypass scope checks entirely; their
permissions are gated by `require_role`.

## Verification flow at the boundary

```
Client → X-API-Key: nxs_live_aB3deadbeef... → get_current_user
  → is_engine_token(token)            # prefix check
  → find_active_by_token(db, token)   # SELECT prefix; bcrypt verify
  → touch_last_used(db, row)          # update last_used_at
  → request.state.api_key = row       # scope deps read this
```

Lookups are O(1) on `prefix` (unique-indexed column). Bcrypt is the
expensive step (~50 ms typical); the tradeoff is brute-force
resistance if the `api_keys` table is exfiltrated.
