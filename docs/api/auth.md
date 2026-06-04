# Auth, Sessions, MFA, API Keys

> **Base paths:** `/api/v1/auth`, `/api/v1/auth/mfa`, `/api/v1/auth/api-keys`
>
> **Source:** [`engine/api/routes/auth.py`](../../engine/api/routes/auth.py),
> [`engine/api/routes/mfa.py`](../../engine/api/routes/mfa.py),
> [`engine/api/routes/api_keys.py`](../../engine/api/routes/api_keys.py),
> [`engine/api/auth/`](../../engine/api/auth/)

## Mental model

Nexus supports a pluggable auth-provider registry. The engine startup
loops over `NEXUS_AUTH_PROVIDERS` (comma-separated) and instantiates
each provider in [`engine/app.py:_build_auth_registry`](../../engine/app.py).
Today the set is:

| Slug     | Class | What it does |
|----------|-------|--------------|
| `local`  | `LocalAuthProvider` | Email + bcrypt password, local registration. Always loaded. |
| `google` | `GoogleAuthProvider` | Google OAuth2 (Authorization Code). |
| `github` | `GitHubAuthProvider` | GitHub OAuth2. |
| `oidc`   | `OIDCAuthProvider` | Generic OpenID Connect (e.g. Okta, Keycloak). |
| `ldap`   | `LDAPAuthProvider` | Bind-then-search LDAP; requires `python-ldap` extra. |

Sessions are JWT access tokens (15–60 min, see
`NEXUS_JWT_ACCESS_TOKEN_EXPIRE_MINUTES`) plus rotating refresh tokens
(7-day expiry, hash-stored in `refresh_tokens` table — see
[`engine/db/models.py:RefreshToken`](../../engine/db/models.py)).

Refresh-token rotation is **atomic and replay-detecting**: reusing an
already-rotated token revokes every active session for that user. See
`POST /api/v1/auth/refresh` below.

MFA is TOTP-based with a per-user Fernet-encrypted secret
(`NEXUS_MFA_ENCRYPTION_KEY`). Backup codes are bcrypt-hashed; the
plaintext is shown once at enrollment.

API keys are an alternative credential for headless clients. The token
form is `prefix.secret`, where `prefix` is stored plaintext (for UI
display + DB lookup) and `secret` is bcrypt-hashed. Plaintext is shown
exactly once on creation.

## `POST /api/v1/auth/register`

Create a new local user.

- **Auth:** none (gated only by `NEXUS_AUTH_LOCAL_ALLOW_REGISTRATION=true`).
- **Body:** `RegisterRequest { email, password, display_name? }`.
  Passwords enforce `MIN_PASSWORD_LENGTH = 8` (in
  [`engine/api/routes/auth.py`](../../engine/api/routes/auth.py)).
- **Success (201):** `TokenResponse` — access + refresh, ready to use.
- **Conflict (409):** email already exists. We deliberately return 409
  rather than 200-with-fake-token to keep the registration UX honest.
- **Forbidden (403):** local registration disabled.

## `POST /api/v1/auth/login`

Local email/password login.

- **Body:** `LoginRequest { email, password }`.
- **No MFA enrolled (200):** `TokenResponse`.
- **MFA enrolled (200):** `MFARequiredResponse { mfa_required: true, challenge_token }`.
  The client must now `POST /api/v1/auth/mfa/verify` with the challenge
  token and a TOTP code; only then does it receive a real access token.
  Returning 200 (not 401) for both branches is deliberate — the frontend
  treats the presence of `mfa_required` as a routing signal, not an
  error.
- **Unauthorized (401):** bad credentials.

## `POST /api/v1/auth/refresh`

Rotate a refresh token.

- **Body:** `RefreshRequest { refresh_token }`.
- **Success (200):** new `TokenResponse`. The old refresh token is
  atomically marked `revoked_at = now()` via a `RETURNING` update. If
  the same token comes back, every unrevoked token for that user is
  revoked and the request returns 401 — this is the replay-detection
  guard.
- **Unauthorized (401):** invalid token, expired token, or replay
  detected (different `detail` strings for each case).

## `GET /api/v1/auth/me`

- **Auth:** Bearer or API key.
- **200:** `UserProfileResponse { id, email, display_name, role,
  auth_provider, is_active }`.

## `POST /api/v1/auth/logout`

- **Auth:** Bearer or API key.
- **Body:** optional `RefreshRequest`. If a refresh token is supplied,
  only that one is revoked; otherwise every active refresh token for the
  user is revoked.
- **200:** `{"status": "logged_out"}`.

## `GET /api/v1/auth/{provider}/authorize`

Begin a federated login. `{provider}` ∈ `google | github | oidc`.

- **Query:** none.
- **200:** `{ authorize_url, state }`. The client redirects the user
  agent to `authorize_url`. A `state` cookie is set on the
  `/api/v1/auth` path (HttpOnly, SameSite=Lax, Secure in production)
  for CSRF defense.

## `GET /api/v1/auth/{provider}/callback`

OAuth2 callback. Validates `state` against the cookie, exchanges the
code for an IdP token, looks up / provisions the local user, and issues
engine tokens.

- **Success (200):** `TokenResponse`.
- **401 / 400:** state mismatch, IdP error, or no `email` claim.

> **Role escalation note (SEV-741).** By default the engine does *not*
> overwrite a previously-granted local role from the IdP claim. This is
> controlled by `NEXUS_AUTH_OVERWRITE_ROLE_ON_LOGIN` (defaults to
> `false`). Operators who want strict IdP-authoritative role mapping
> opt in explicitly — see [`engine/config.py`](../../engine/config.py).

## MFA endpoints

All mounted at `/api/v1/auth/mfa`. See
[`engine/api/routes/mfa.py`](../../engine/api/routes/mfa.py).

| Method | Path | Body | Result |
|--------|------|------|--------|
| `POST` | `/enroll` | — | `{ secret, otpauth_uri }`. User adds the secret to their authenticator. |
| `POST` | `/enroll/confirm` | `{ secret, code }` | `{ backup_codes }` — TOTP secret is now persisted, MFA is on. |
| `POST` | `/verify` | `{ challenge_token, code }` | On success: `TokenResponse`. Backup codes are accepted here and rotated out of the stored set if used. |
| `POST` | `/disable` | `{ password, code }` | `{ status: "disabled" }`. Requires the user's *password* (not a federated IdP session) plus a current TOTP code. |
| `POST` | `/backup-codes/regen` | `{ code }` | `{ backup_codes }` — fresh set, old set invalidated. |

`/enroll` and `/enroll/confirm` are idempotent only across a single
enrollment — calling `/enroll` again after `confirm` returns 409.

## API keys

All mounted at `/api/v1/auth/api-keys`. See
[`engine/api/routes/api_keys.py`](../../engine/api/routes/api_keys.py).

| Method | Path | Auth | Body / Notes |
|--------|------|------|--------------|
| `POST` | `` | Bearer | `ApiKeyCreateRequest { name, scopes?, expires_at?, env? }`. **201** returns `ApiKeyCreatedResponse` — the plaintext `token` is in this response only. |
| `GET` | `` | Bearer | Lists caller's keys: `[{ id, name, prefix, scopes, last_used_at, expires_at, revoked_at, created_at }]`. Plaintext secret is never returned. |
| `DELETE` | `/{key_id}` | Bearer | **204** on success, **404** if not owned by caller. Soft-revokes (`revoked_at = now()`). |

### Using an API key

Two equivalent forms are accepted:

```http
GET /api/v1/portfolio/ HTTP/1.1
X-API-Key: nexus_abc123.xYz...
```

```http
GET /api/v1/portfolio/ HTTP/1.1
Authorization: Bearer nexus_abc123.xYz...
```

The dependency resolver recognises the `nexus_` prefix and routes the
request through `_user_from_api_key` instead of JWT decoding.

### Scope allow-list

| Scope | What it allows |
|-------|----------------|
| `read` | GET routes (default on creation). |
| `write` | Mutating routes that don't move money. |
| `trade` | Order / backtest submit + webhook CRUD. |
| `admin` | Anything an `admin` user can do. |

The canonical list lives in
[`engine/api/auth/api_keys.py:VALID_SCOPES`](../../engine/api/auth/api_keys.py).

## Failure modes & error shapes

| Scenario | Status | `detail` |
|----------|--------|----------|
| No Authorization header on a protected route | 401 | `Authentication required` |
| Expired JWT | 401 | `Invalid or expired token` |
| Revoked refresh token reused | 401 | `Token reuse detected — all sessions revoked` |
| Wrong MFA code | 401 | `Invalid MFA code` |
| Trying to issue an API key with a bogus scope | 400 | `Invalid scopes: [...]` |
| Hitting a role-gated route without sufficient role | 403 | `Insufficient role` |
| Hitting a scope-gated route without sufficient scope | 403 | `Insufficient scope` |

The MFA `MFAServiceError` is the transport-level exception for Fernet /
TOTP / backup-code hash failures; the route layer maps it to either 400,
500, or 503 depending on whether the failure is user-input, key-misconfig,
or general-unavailable.
