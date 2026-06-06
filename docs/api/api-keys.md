# API keys

Base path: `/api/v1/auth/api-keys`. Source:
[`engine/api/routes/api_keys.py`](../../engine/api/routes/api_keys.py),
[`engine/api/auth/api_keys.py`](../../engine/api/auth/api_keys.py).

API keys are long-lived bearer tokens intended for headless clients
(CI, trading bots, the MCP server). They are scoped (not role-bound)
and revocable independently of the user account.

## Token shape

```
nxs_<env>_<32-hex-chars>
```

- `nxs_` — fixed prefix that lets the auth dependency distinguish an
  API key from a JWT at the boundary.
- `<env>` — operator-chosen label (e.g. `live`, `test`, `staging`).
  Baked into the token at issue time. Cannot be changed.
- `<32-hex-chars>` — 128 bits of `secrets.token_hex` randomness.

The first 12 characters (`nxs_<env>_<3>`) are stored in plaintext in
`api_keys.prefix` for human-readable identification and as the DB
lookup key. The full token is bcrypt-hashed into `api_keys.key_hash`.
The plaintext is returned to the operator **exactly once**, on
creation.

## Scopes

| Scope  | Allowed verbs / routes                                          |
|--------|------------------------------------------------------------------|
| `read` | `GET` / `HEAD` everywhere an authenticated route is allowed.     |
| `trade`| + `POST`/`PUT`/`DELETE` on backtest, portfolio, webhooks, etc.   |
| `admin`| Equivalent to the `admin` role. Supersedes `read` and `trade`.   |

Scope enforcement is at the route level via `require_api_scope(...)`.
JWT-authenticated requests bypass scope checks (they are gated by
role instead).

## Endpoints

### `POST /api/v1/auth/api-keys`

Issue a new API key. Returns the plaintext token in the response body.

**Auth**: Bearer JWT or API key with `admin` scope.

**Request body**

```json
{
  "name": "ci-runner",
  "scopes": ["read", "trade"],
  "expires_at": "2026-12-31T00:00:00Z",
  "env": "live"
}
```

| Field        | Type             | Default   | Notes                                            |
|--------------|------------------|-----------|--------------------------------------------------|
| `name`       | string           | required  | 1–255 chars.                                     |
| `scopes`     | array of strings | `["read"]`| Subset of `{read, trade, admin}`.                |
| `expires_at` | string ISO-8601  | null      | Null = no expiry.                                |
| `env`        | string           | `"live"`  | 1–16 chars, `^[A-Za-z0-9_]+$`. Baked into token. |

**Response**: `201 Created`:

```json
{
  "id": "uuid",
  "name": "ci-runner",
  "prefix": "nxs_live_aB3",
  "scopes": ["read", "trade"],
  "last_used_at": null,
  "expires_at": "2026-12-31T00:00:00Z",
  "revoked_at": null,
  "created_at": "2026-06-06T12:00:00Z",
  "token": "nxs_live_aB3...full-token-here"
}
```

The `token` field is omitted on every subsequent read. Save it now or
rotate.

### `GET /api/v1/auth/api-keys`

List the caller's API keys. Always excludes the secret.

**Auth**: Bearer JWT or API key.

**Response**: `200 OK`:

```json
[
  {
    "id": "uuid",
    "name": "ci-runner",
    "prefix": "nxs_live_aB3",
    "scopes": ["read", "trade"],
    "last_used_at": "2026-06-06T12:00:00Z",
    "expires_at": "2026-12-31T00:00:00Z",
    "revoked_at": null,
    "created_at": "2026-06-01T00:00:00Z"
  }
]
```

Only the caller's own keys are returned. There is no admin-side
"list-all-keys" endpoint today.

### `DELETE /api/v1/auth/api-keys/{key_id}`

Soft-revoke an API key by setting `revoked_at`. The key stops working
immediately; the row is retained for audit.

**Auth**: Bearer JWT or API key.

**Path params**: `key_id` — UUID.

**Response**: `204 No Content`. `404` if the key does not exist or is
not owned by the caller.

## Verifying an API key

The auth dependency (`engine/api/auth/dependency.py`) does this on
every request:

1. Pull the credential from `Authorization: Bearer` (or
   `X-API-Key`). If the value starts with `nxs_`, treat as an API
   key.
2. Split the token: `prefix = token[:12]`, `bcrypt_input = token` (the
   *full* token, not just the tail — so a guessed tail can't be
   reused across environments).
3. Look up the row by `prefix`. Reject if missing, revoked, or
   expired.
4. bcrypt-verify the full token against `key_hash`. Reject if no
   match.
5. Bump `last_used_at`.

## Limitations

- **No global key listing.** An `admin` user cannot list another
  user's keys. Add an admin-scoped endpoint if a help-desk workflow
  needs it.
- **No per-route scope subdivision.** `trade` covers all write verbs
  on every writable route. Fine-grained scopes (`portfolio:write`,
  `webhook:write`) are deferred.
- **bcrypt on every request.** Each authenticated API-key request
  pays ~50 ms of bcrypt cost. Acceptable today; if it shows up in
  flame graphs, cache the verified-prefix in Valkey with a short TTL.
