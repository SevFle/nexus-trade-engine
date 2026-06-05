# Authentication & MFA

Routes for login, registration, federated identity, MFA enrollment,
and API key management. Source:
[`engine/api/routes/auth.py`](../../engine/api/routes/auth.py),
[`engine/api/routes/mfa.py`](../../engine/api/routes/mfa.py),
[`engine/api/routes/api_keys.py`](../../engine/api/routes/api_keys.py).

For the auth model itself (roles, scopes, JWT vs API key) see
[index → Authentication model](index.md#authentication-model).

## Local account lifecycle

### `POST /api/v1/auth/register`

Create an account with email + password and immediately receive
tokens. Disabled when `NEXUS_AUTH_LOCAL_ALLOW_REGISTRATION=false`
or when the `local` provider is not in `NEXUS_AUTH_PROVIDERS`.

Minimum password length is 8 chars; check enforced in
[`engine/api/routes/auth.py:33`](../../engine/api/routes/auth.py:33).

**Request body** — `RegisterRequest`:

```json
{
  "email": "alice@example.com",
  "password": "8+ chars, secret",
  "display_name": "Alice"
}
```

**Response** `201 Created` — `TokenResponse`:

```json
{
  "access_token": "<jwt>",
  "refresh_token": "<opaque>",
  "token_type": "bearer",
  "expires_in": 3600
}
```

`409 Conflict` if email is already registered.

### `POST /api/v1/auth/login`

Exchange credentials for tokens. If the user has MFA enabled the
response is `200` with a `mfa_required: true` envelope instead of
tokens; the caller must then `POST /api/v1/auth/mfa/verify` with
the challenge token + TOTP code.

**Request body** — `LoginRequest`:

```json
{ "email": "alice@example.com", "password": "..." }
```

**Response** `200 OK` (no MFA) — `TokenResponse` (same shape as
register).

**Response** `200 OK` (MFA enrolled) — `MFARequiredResponse`:

```json
{ "mfa_required": true, "challenge_token": "<jwt-like>" }
```

`401 Unauthorized` on bad credentials. The error message is
deliberately generic to avoid account enumeration.

### `POST /api/v1/auth/refresh`

Rotate the refresh token. **Atomic single-use**: the row is
revoked in the same UPDATE that finds it. If the same token is
presented twice the second attempt triggers a replay alarm and
**revokes every other session for that user** (see
[`auth.py:183`](../../engine/api/routes/auth.py:183)).

**Request body** — `RefreshRequest`:

```json
{ "refresh_token": "<opaque>" }
```

**Response** `200 OK` — `TokenResponse`.

`401 Unauthorized` if the token is revoked, expired, or unknown.
The replay-detection branch returns `"Token reuse detected — all
sessions revoked"`.

### `POST /api/v1/auth/logout`

Revoke either the supplied refresh token (if any) or every active
refresh token for the calling user. JWTs themselves are not
revoked — they expire at their `exp` claim. If you need immediate
JWT invalidation you must lower `NEXUS_JWT_ACCESS_TOKEN_EXPIRE_MINUTES`.

**Auth:** JWT.

**Request body** — `RefreshRequest` (optional):

```json
{ "refresh_token": "<opaque>" }
```

**Response** `200 OK`:

```json
{ "status": "logged_out" }
```

### `GET /api/v1/auth/me`

Return the calling user's profile.

**Auth:** JWT or API key.

**Response** `200 OK` — `UserProfileResponse`:

```json
{
  "id": "uuid",
  "email": "alice@example.com",
  "display_name": "Alice",
  "role": "user",
  "auth_provider": "local",
  "is_active": true
}
```

## Federated login (OAuth2 / OIDC / LDAP)

Federated providers are mounted at
`/api/v1/auth/{provider}/...` and follow the same three-step
shape: `authorize` → external redirect → `callback`. Available
when the provider is listed in `NEXUS_AUTH_PROVIDERS`:

- `google` — Google OAuth2.
- `github` — GitHub OAuth2.
- `oidc` — generic OpenID Connect.
- `ldap` — direct bind against an LDAP server.

### `GET /api/v1/auth/{provider}/authorize`

Build the external authorize URL and stash a state cookie
(`oauth_state_<provider>`, 10-min TTL, httponly, SameSite=Lax,
Secure in production).

**Response** `200 OK`:

```json
{ "authorize_url": "https://accounts.google.com/...", "state": "..." }
```

`404 Not Found` if the provider is not registered.

### `GET /api/v1/auth/{provider}/callback`

Validate the `state` cookie, exchange the `code` for user info
via the provider, look up or provision the local user, and mint
tokens.

**Query params:** `code`, `state`.

**Response** `200 OK` — `TokenResponse`.

`401 Unauthorized` on state mismatch or rejected code. The error
message is generic.

### Role overwrite semantics

By default the engine **does not** overwrite a local user's role
based on what the IdP asserts. Set
`NEXUS_AUTH_OVERWRITE_ROLE_ON_LOGIN=true` to opt in (SEV-741). This
is a defense-in-depth knob — a misconfigured upstream IdP cannot
escalate or downgrade an already-granted role without operator
opt-in.

## MFA endpoints (`/api/v1/auth/mfa/*`)

TOTP-based, RFC 6238. Secrets are Fernet-encrypted at rest with
`NEXUS_MFA_ENCRYPTION_KEY` (32-byte url-safe base64). MFA
enrollment is a two-call sequence:

### `POST /api/v1/auth/mfa/enroll`

**Auth:** JWT. Generates a new TOTP secret (not yet active) and
returns it as an `otpauth://` URL plus the bcrypt-hashed challenge
token needed for the confirm step.

**Response** `200 OK` — `EnrollResponse`:

```json
{
  "secret": "JBSWY3DPEHPK3PXP",
  "otpauth_url": "otpauth://totp/Nexus:alice@example.com?secret=...&issuer=Nexus"
}
```

### `POST /api/v1/auth/mfa/enroll/confirm`

**Auth:** JWT. Activate the secret from the previous step by
proving the user can generate a valid code. Generates a one-time
set of backup codes.

**Request body:**

```json
{ "code": "123456" }
```

**Response** `200 OK` — `ConfirmResponse`:

```json
{
  "mfa_enabled": true,
  "backup_codes": ["112233", "445566", ...]
}
```

### `POST /api/v1/auth/mfa/verify`

**Auth:** none — the caller presents a `challenge_token` from
`POST /auth/login`. Exchange a TOTP code (or backup code) for
real session tokens.

**Request body:**

```json
{ "challenge_token": "<from /login>", "code": "123456" }
```

**Response** `200 OK` — `TokenResponse`. `401` on bad code.

### `POST /api/v1/auth/mfa/disable`

**Auth:** JWT + valid `code`. Clears `mfa_enabled`,
`mfa_secret_encrypted`, and `mfa_backup_codes`.

### `POST /api/v1/auth/mfa/backup-codes/regen`

**Auth:** JWT + valid `code`. Generates a new backup-code set;
the previous set is invalidated atomically.

## API keys (`/api/v1/auth/api-keys`)

Long-lived scoped credentials for headless clients (CI, the SDK,
the planned MCP server). Tokens are bcrypt-hashed; the plaintext
is shown exactly once on creation. See
[`engine/api/auth/api_keys.py`](../../engine/api/auth/api_keys.py).

### `POST /api/v1/auth/api-keys`

**Auth:** JWT. **No scope check** — only the user themselves can
mint their own keys.

**Request body** — `ApiKeyCreateRequest`:

```json
{
  "name": "ci-runner",
  "scopes": ["read", "trade"],
  "expires_at": "2027-01-01T00:00:00Z",
  "env": "ci"
}
```

`scopes` defaults to `["read"]`. Allowed values: `read`, `trade`,
`admin`. `env` is an opaque tag for the operator's own labeling
(max 16 chars, alphanumeric + `_`).

**Response** `201 Created` — `ApiKeyCreatedResponse`:

```json
{
  "id": "uuid",
  "name": "ci-runner",
  "prefix": "nxs_abc12345",
  "scopes": ["read", "trade"],
  "last_used_at": null,
  "expires_at": "2027-01-01T00:00:00Z",
  "revoked_at": null,
  "created_at": "2026-06-05T12:00:00Z",
  "token": "nxs_abc12345...full-secret..."
}
```

The `token` field will not appear again. Store it now.

### `GET /api/v1/auth/api-keys`

List the calling user's keys (metadata only — never tokens).

### `DELETE /api/v1/auth/api-keys/{key_id}`

Revoke a key by setting `revoked_at`. The row is retained for
audit; lookups reject revoked rows. `204 No Content`.

## Token formats

- **JWT access token** — HS256, signed with `NEXUS_SECRET_KEY`. Claims:
  `sub` (user UUID), `email`, `role`, `provider`, `exp` (1h default).
  Rotation supported via `NEXUS_SECRET_KEY_PREVIOUS`.
- **Refresh token** — opaque 64-byte url-safe, stored as SHA-256
  hex (`token_hash`). Lifetime:
  `NEXUS_JWT_REFRESH_TOKEN_EXPIRE_DAYS` (default 7).
- **MFA challenge token** — short-lived signed token issued by
  `engine.api.auth.mfa_service.issue_challenge`. Default TTL
  `NEXUS_MFA_CHALLENGE_TTL_SECONDS = 300`.
- **API key** — `nxs_<12-char-prefix><secret>`. The prefix is
  stored in plaintext for UI display; the full token is bcrypt-hashed.

## Operational knobs

| Env var                              | Default                | Effect                                            |
|--------------------------------------|------------------------|---------------------------------------------------|
| `NEXUS_SECRET_KEY`                   | (none, required)       | HS256 sign key. **Engine refuses to start without it outside test env.** |
| `NEXUS_SECRET_KEY_PREVIOUS`          | (none)                 | Previous key; accepted for verification during rotation. |
| `NEXUS_JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | 60                  | Access-token TTL.                                 |
| `NEXUS_JWT_REFRESH_TOKEN_EXPIRE_DAYS`   | 7                   | Refresh-token TTL.                                |
| `NEXUS_AUTH_PROVIDERS`               | `local`                | Comma-separated list of providers to mount.       |
| `NEXUS_AUTH_LOCAL_ALLOW_REGISTRATION` | `true`                | Whether `POST /register` is enabled.              |
| `NEXUS_AUTH_OVERWRITE_ROLE_ON_LOGIN`  | `false`               | Allow federated IdP to overwrite local role.      |
| `NEXUS_MFA_ENCRYPTION_KEY`            | (none)                | Fernet key for TOTP secrets at rest. Empty disables MFA. |
| `NEXUS_MFA_CHALLENGE_TTL_SECONDS`     | `300`                 | Challenge-token TTL.                              |
| `NEXUS_MFA_BACKUP_CODES_COUNT`        | `10`                  | How many backup codes to issue per (re)gen.       |

For provider-specific env vars (Google client id, LDAP bind DN,
etc.) see [`engine/config.py`](../../engine/config.py).
