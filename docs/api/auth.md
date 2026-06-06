# Auth API

Base path: `/api/v1/auth`. Source: [`engine/api/routes/auth.py`](../../engine/api/routes/auth.py),
[`engine/api/auth/`](../../engine/api/auth/).

## Authentication model

Two layers:

1. **Auth provider** — *who* the user is. Pluggable. Selected at
   startup via `NEXUS_AUTH_PROVIDERS` (comma-separated). Built-in
   providers: `local`, `google`, `github`, `oidc`, `ldap`. Each
   implements the `AuthProvider` protocol in
   [`engine/api/auth/base.py`](../../engine/api/auth/base.py).
2. **RBAC + scopes** — *what* they can do. Stored on `users.role`
   (role) and `api_keys.scopes` (scope). Enforced by
   [`engine/api/auth/dependency.py`](../../engine/api/auth/dependency.py).

### Roles

`ROLE_HIERARCHY` (from `dependency.py`):

| Role                | Level | Implied capability                                       |
|---------------------|-------|----------------------------------------------------------|
| `viewer`            | 0     | Read-only.                                               |
| `user`              | 1     | Default for self-service signup. Read + manage own data. |
| `retail_trader`     | 2     | Run backtests, manage own portfolios.                    |
| `quant_dev`         | 3     | + install / uninstall strategies from marketplace.       |
| `developer`         | 4     | + manage webhooks, marketplace publish.                  |
| `portfolio_manager` | 5     | + manage other users' portfolios (future).               |
| `admin`             | 6     | Everything.                                              |

Higher levels subsume lower. `require_role("developer")` admits
`developer`, `portfolio_manager`, and `admin`.

### Scopes (API keys only)

| Scope  | Allowed                                                              |
|--------|----------------------------------------------------------------------|
| `read` | `GET` / `HEAD` only.                                                 |
| `trade`| + write to backtest, portfolio, webhooks, etc.                       |
| `admin`| Equivalent to the `admin` role; supersedes `read` and `trade`.       |

JWT-authenticated requests are *full-scope*; scopes apply only when
the request was authenticated by an API key.

## Endpoints

### `POST /api/v1/auth/register`

Create a local user. Only available when the `local` provider is
enabled *and* `NEXUS_AUTH_LOCAL_ALLOW_REGISTRATION=true`.

**Request body**

```json
{
  "email": "user@example.com",
  "password": "secret-password",
  "display_name": "Optional display name"
}
```

Password requirements: ≥ 8 characters (see `MIN_PASSWORD_LENGTH` in
`routes/auth.py`). Stored as bcrypt.

**Response**: `201 Created` → `TokenResponse`.

### `POST /api/v1/auth/login`

Local login. If MFA is enabled for the user, returns a challenge
token; otherwise returns the access + refresh tokens directly.

**Request body**

```json
{ "email": "user@example.com", "password": "secret-password" }
```

**Response (no MFA)**: `200 OK`:

```json
{
  "access_token": "<JWT>",
  "refresh_token": "<opaque>",
  "token_type": "bearer",
  "expires_in": 3600
}
```

**Response (MFA enrolled)**: `200 OK`:

```json
{ "mfa_required": true, "challenge_token": "<short-lived JWT>" }
```

The client then `POST /api/v1/auth/mfa/verify` with the challenge and
a TOTP code to receive the token pair.

### `POST /api/v1/auth/refresh`

Rotate a refresh token. Single-use: the presented refresh token is
atomically revoked, and a new pair is issued. **If a revoked token is
ever re-presented, every refresh token for that user is revoked
immediately** (replay defence).

**Request body**

```json
{ "refresh_token": "<opaque>" }
```

**Response**: `200 OK` → `TokenResponse`, or `401` if the token is
unknown, expired, or replayed.

### `GET /api/v1/auth/me`

Return the caller's profile.

**Auth**: Bearer JWT or API key.

**Response**: `200 OK`:

```json
{
  "id": "uuid",
  "email": "user@example.com",
  "display_name": "User",
  "role": "user",
  "auth_provider": "local",
  "is_active": true
}
```

### `POST /api/v1/auth/logout`

Revoke the presented refresh token (if any), or all of the caller's
outstanding refresh tokens if no body is supplied.

**Auth**: Bearer JWT or API key.

**Request body** (optional):

```json
{ "refresh_token": "<opaque>" }
```

**Response**: `200 OK`:

```json
{ "status": "logged_out" }
```

### `GET /api/v1/auth/{provider}/authorize`

Build the OAuth / OIDC `authorize_url` for the named provider. Returns
a state cookie scoped to `/api/v1/auth` so the callback can verify
the round-trip.

**Path params**: `provider` ∈ `{google, github, oidc}`.

**Response**: `200 OK`:

```json
{ "authorize_url": "https://accounts.google.com/...", "state": "<random>" }
```

The client redirects the browser to `authorize_url`. After the user
authenticates, the IdP redirects back to `GET /api/v1/auth/{provider}/callback`.

### `GET /api/v1/auth/{provider}/callback`

Complete the OAuth / OIDC round-trip. Validates the `state` cookie,
exchanges the `code` for user info via the provider, upserts the
`User` row (or rejects if the IdP-asserted role would silently
escalate and `NEXUS_AUTH_OVERWRITE_ROLE_ON_LOGIN=false`), and issues
the token pair.

**Query params**: `code`, `state`.

**Response**: `200 OK` → `TokenResponse`, or `401` if the state cookie
is missing / mismatched, or if the IdP refused the code.

## MFA endpoints (`/api/v1/auth/mfa/*`)

Source: [`engine/api/routes/mfa.py`](../../engine/api/routes/mfa.py).

### `POST /api/v1/auth/mfa/enroll`

Begin TOTP enrollment. Returns the secret (base32) and an
`otpauth://` URI the client can render as a QR code. The secret is
*not* persisted yet — that happens on `/enroll/confirm`.

**Auth**: Bearer JWT.

**Response**: `200 OK`:

```json
{
  "secret": "JBSWY3DPEHPK3PXP",
  "otpauth_uri": "otpauth://totp/Nexus:user@example.com?secret=..."
}
```

**Conflict**: `409` if MFA is already enabled.

### `POST /api/v1/auth/mfa/enroll/confirm`

Confirm enrollment by submitting a valid TOTP code derived from the
secret. Persists the Fernet-encrypted secret + hashed backup codes.

**Auth**: Bearer JWT.

**Request body**:

```json
{ "secret": "JBSWY3DPEHPK3PXP", "code": "123456" }
```

**Response**: `200 OK`:

```json
{ "backup_codes": ["xx-xx", "xx-xx", "..."] }
```

Backup codes are 10 random strings, hashed at rest. Each one is
single-use. Returned in plaintext exactly once.

### `POST /api/v1/auth/mfa/verify`

Complete an MFA-gated login. Submit the challenge token (from
`/login`) plus a TOTP code or backup code.

**Request body**:

```json
{ "challenge_token": "<short-lived JWT>", "code": "123456" }
```

**Response**: `200 OK` → `TokenResponse`. `401` on invalid code or
expired challenge.

### `POST /api/v1/auth/mfa/disable`

Disable MFA for the caller. Requires re-verifying both the password
and a current TOTP code (defence against session hijack → MFA-off
attack).

**Request body**:

```json
{ "password": "secret", "code": "123456" }
```

**Response**: `200 OK`:

```json
{ "status": "disabled" }
```

### `POST /api/v1/auth/mfa/backup-codes/regen`

Generate a fresh set of backup codes (the previous set is discarded).

**Request body**:

```json
{ "code": "123456" }
```

**Response**: `200 OK` → `ConfirmResponse` (same shape as
`/enroll/confirm`).

## LDAP notes

The LDAP provider (`engine/api/auth/ldap.py`) does not register
endpoints under `/api/v1/auth/ldap/`. LDAP is treated as a
*server-side* authentication mechanism for inbound basic-auth or
form-post credentials; it cannot be initiated from the browser via
this API. Operators that want LDAP-only auth put a reverse proxy in
front that handles bind-on-behalf and emits a JWT.

## Configuration

Auth-related env vars (all prefixed `NEXUS_`):

| Var                                | Purpose                                                          |
|------------------------------------|------------------------------------------------------------------|
| `SECRET_KEY`                       | JWT signing key. **Required** outside `test`.                    |
| `SECRET_KEY_PREVIOUS`              | Previous signing key, honoured during rotation.                  |
| `JWT_ACCESS_TOKEN_EXPIRE_MINUTES`  | Default `60`.                                                    |
| `JWT_REFRESH_TOKEN_EXPIRE_DAYS`    | Default `7`.                                                     |
| `AUTH_PROVIDERS`                   | Comma-separated: `local,google,github,oidc,ldap`.                |
| `AUTH_LOCAL_ALLOW_REGISTRATION`    | Default `true`. Set `false` to disable `/register`.              |
| `AUTH_OVERWRITE_ROLE_ON_LOGIN`     | Default `false`. See SEV-741 — defence against silent role escalation by federated IdP. |
| `MFA_ENCRYPTION_KEY`               | Fernet key (url-safe base64, 32 bytes decoded). Empty disables MFA. |
| `MFA_CHALLENGE_TTL_SECONDS`        | Default `300` (5 min).                                           |
| `MFA_BACKUP_CODES_COUNT`           | Default `10`.                                                    |
| `GOOGLE_CLIENT_ID` / `_SECRET` / `_REDIRECT_URI` | Google OAuth2.                                      |
| `GITHUB_CLIENT_ID` / `_SECRET` / `_REDIRECT_URI` | GitHub OAuth2.                                      |
| `OIDC_DISCOVERY_URL` / `_CLIENT_ID` / `_CLIENT_SECRET` / `_REDIRECT_URI` / `_ROLE_CLAIM` | Generic OIDC. |
| `LDAP_SERVER_URL` / `_BIND_DN` / `_BIND_PASSWORD` / `_SEARCH_BASE` / `_ROLE_MAPPING` | LDAP. |
