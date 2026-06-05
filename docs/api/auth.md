# Auth API

Mounted at `/api/v1/auth`. Implementation: `engine/api/routes/auth.py`.

The engine supports five identity providers via a pluggable registry
(`engine/api/auth/registry.py`). Wire-up happens at app start in
`engine/app.py:_build_auth_registry`, driven by the comma-separated
`NEXUS_AUTH_PROVIDERS` env var (default `local`).

| Provider | Module                                | Notes                                                          |
|----------|---------------------------------------|----------------------------------------------------------------|
| `local`  | `engine/api/auth/local.py`            | Email/password. Bcrypt hash. Enabled by default.               |
| `google` | `engine/api/auth/google.py`           | OAuth2. Requires `NEXUS_GOOGLE_CLIENT_*`.                      |
| `github` | `engine/api/auth/github_oauth.py`     | OAuth2. Requires `NEXUS_GITHUB_CLIENT_*`.                      |
| `oidc`   | `engine/api/auth/oidc.py`             | Generic OIDC. Requires `NEXUS_OIDC_*`.                          |
| `ldap`   | `engine/api/auth/ldap.py`             | Bind DN + search. Optional `python-ldap` extra.                |

Federated providers do **not** silently promote unrecognized roles —
see `engine/api/auth/base.py:map_roles` and the SEV-741 changelog.

## POST /register

Local-provider signup. Returns a fresh token pair.

**Request body** `RegisterRequest`:
```json
{
  "email": "alice@example.com",
  "password": "eight-or-more",
  "display_name": "Alice"
}
```

`password` must be ≥ 8 chars (constant defined in the route module).
`display_name` defaults to the local part of the email.

**Responses**
- `201 Created` — `TokenResponse` (access + refresh).
- `403 Forbidden` — local provider disabled.
- `409 Conflict` — email already registered.

Registration can be disabled by setting
`NEXUS_AUTH_LOCAL_ALLOW_REGISTRATION=false`.

## POST /login

Verifies credentials. If MFA is enabled, returns a short-lived
challenge token instead of minting session tokens.

**Request body** `LoginRequest`:
```json
{ "email": "alice@example.com", "password": "eight-or-more" }
```

**Responses**
- `200 OK` — `TokenResponse`.
- `200 OK` — `MFARequiredResponse` (`{"mfa_required": true,
  "challenge_token": "..."}`). Client must complete
  [`POST /api/v1/auth/mfa/verify`](mfa.md#post-verify).
- `401 Unauthorized` — bad credentials.

## POST /refresh

Rotates a refresh token. **Single-use**: the refresh token is revoked
on first successful exchange. If a previously-used token is presented
again, the engine treats it as replay and revokes **every** session
for that user.

**Request body** `RefreshRequest`:
```json
{ "refresh_token": "<opaque>" }
```

**Responses**
- `200 OK` — fresh `TokenResponse`.
- `401 Unauthorized` — invalid / expired / replayed token (the latter
  revokes all the user's sessions).

## POST /logout

Revokes the caller's refresh token (if supplied) or all of the user's
outstanding refresh tokens.

**Headers** — requires authentication.
**Body** (optional) — `RefreshRequest`.

**Response** — `200 OK` `{"status": "logged_out"}`.

## GET /me

Returns the authenticated user's profile.

**Response** `UserProfileResponse`:
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

## OAuth2 flow: GET /{provider}/authorize

Begins an OAuth2/OIDC flow for the given provider. Returns a URL the
client should redirect to plus a state parameter that is also stored
in an HttpOnly cookie scoped to `/api/v1/auth` (max-age 600s).

**Path** — `google` | `github` | `oidc`.

**Response**:
```json
{ "authorize_url": "https://...", "state": "..." }
```

**Errors** — `404` if the provider is not enabled, `500` if it cannot
build an authorize URL.

## OAuth2 callback: GET /{provider}/callback

Completes an OAuth2/OIDC flow. Validates `state` against the cookie,
exchanges `code` for user info via the provider, and mints session
tokens.

**Query params** — `code`, `state`.

**Responses**
- `200 OK` — `TokenResponse`.
- `401 Unauthorized` — missing/mismatched state, bad code.

The cookie is deleted after a successful callback.

## Token response shape

```json
{
  "access_token": "<JWT>",
  "refresh_token": "<opaque>",
  "token_type": "bearer",
  "expires_in": 3600
}
```

`expires_in` is derived from `NEXUS_JWT_ACCESS_TOKEN_EXPIRE_MINUTES`
(default 60). Refresh tokens live for
`NEXUS_JWT_REFRESH_TOKEN_EXPIRE_DAYS` (default 7).

## JWT claim shape

The JWT is HS256-signed with `NEXUS_SECRET_KEY`. Claims:

| Claim     | Source                                |
|-----------|---------------------------------------|
| `sub`     | `str(user.id)` (UUID)                 |
| `email`   | `user.email`                          |
| `role`    | `user.role`                           |
| `provider`| `user.auth_provider`                  |
| `exp`     | now + access TTL                      |
| `iat`     | now                                   |

Rotation: set `NEXUS_SECRET_KEY_PREVIOUS` to the old key during a
rotation; both keys verify during the window.

## Rate-limit interaction

Auth routes share the global 600/min limit. There is no per-route
tightening today — operators who observe credential stuffing should
front the engine with a reverse proxy that adds per-IP captcha or
lockout (the engine does not implement either).
