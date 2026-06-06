# Auth API

Routes for registering, logging in, multi-factor auth, OAuth2 / OIDC /
LDAP federation, and API key management. All routes are mounted under
`/api/v1/auth`. The MFA sub-tree is at `/api/v1/auth/mfa` and API keys
at `/api/v1/auth/api-keys`.

Implementation: [`engine/api/routes/auth.py`](../../engine/api/routes/auth.py),
[`engine/api/routes/mfa.py`](../../engine/api/routes/mfa.py),
[`engine/api/routes/api_keys.py`](../../engine/api/routes/api_keys.py).

## Endpoint summary

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/api/v1/auth/register` | none (if `auth_local_allow_registration=true`) | Create a local user; returns token pair |
| `POST` | `/api/v1/auth/login` | none | Local login; returns token pair or `mfa_required` |
| `POST` | `/api/v1/auth/refresh` | refresh token | Rotate refresh token; new access token |
| `POST` | `/api/v1/auth/logout` | JWT | Revoke one or all refresh tokens for caller |
| `GET`  | `/api/v1/auth/me` | JWT | Current user profile |
| `GET`  | `/api/v1/auth/{provider}/authorize` | none | Build an OAuth2/OIDC/LDAP authorize URL |
| `GET`  | `/api/v1/auth/{provider}/callback` | state cookie | Complete federated login |
| `POST` | `/api/v1/auth/mfa/enroll` | JWT | Begin TOTP enrollment (returns secret + otpauth URI) |
| `POST` | `/api/v1/auth/mfa/enroll/confirm` | JWT | Verify first TOTP; persist encrypted secret + backup codes |
| `POST` | `/api/v1/auth/mfa/verify` | challenge token | Verify TOTP at login; receive tokens |
| `POST` | `/api/v1/auth/mfa/disable` | JWT + password + TOTP | Disable MFA |
| `POST` | `/api/v1/auth/mfa/backup-codes/regen` | JWT + TOTP | Generate fresh backup codes |
| `POST` | `/api/v1/auth/api-keys` | JWT | Issue an API key (returned once) |
| `GET`  | `/api/v1/auth/api-keys` | JWT | List caller's API keys |
| `DELETE` | `/api/v1/auth/api-keys/{key_id}` | JWT | Revoke an API key |

## Token model

### Access token (JWT)

- **Algorithm:** HS256. Switching to RS256 is a one-line change once a
  key source is wired.
- **Claims:** `sub` (user UUID), `email`, `role`, `provider` (`local` /
  `google` / `github` / `oidc` / `ldap`), `exp`, `iat`.
- **TTL:** `NEXUS_JWT_ACCESS_TOKEN_EXPIRE_MINUTES` (default 60 m).
- **Signing key:** `NEXUS_SECRET_KEY`. `NEXUS_SECRET_KEY_PREVIOUS` is
  accepted in parallel during rotation.

### Refresh token

- Opaque random string, hashed (SHA-256) at rest in `refresh_tokens`.
- **TTL:** `NEXUS_JWT_REFRESH_TOKEN_EXPIRE_DAYS` (default 7 d).
- **Rotation:** every call to `/refresh` atomically revokes the
  presented token and issues a new one. If a revoked token is ever
  re-presented, *every* outstanding refresh token for that user is
  revoked immediately (token-replay detection).

### API key

- **Format:** `nxs_<env>_<32 hex chars>` (e.g. `nxs_live_a1b2c3...`).
- Stored as bcrypt hash; first 12 chars (`nxs_<env>_<3 hex>`) are
  stored in plaintext for display.
- **Shown once** on creation; cannot be recovered.
- Optional `expires_at` and per-key `scopes` (`read` / `trade` /
  `admin`).

## Schemas

```python
class RegisterRequest(BaseModel):
    email: EmailStr
    password: str            # ≥ 8 chars
    display_name: str | None = None

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int          # seconds, matches access-token TTL

class MFARequiredResponse(BaseModel):
    mfa_required: bool = True
    challenge_token: str     # 5-minute TTL, used in /mfa/verify

class RefreshRequest(BaseModel):
    refresh_token: str

class UserProfileResponse(BaseModel):
    id: str
    email: str
    display_name: str
    role: str                # viewer|user|retail_trader|quant_dev|developer|portfolio_manager|admin
    auth_provider: str       # local|google|github|oidc|ldap
    is_active: bool
```

### API keys

```python
class ApiKeyCreateRequest(BaseModel):
    name: str                # 1-255 chars
    scopes: list[str] = ["read"]
    expires_at: datetime | None = None
    env: str = "live"        # alphanumeric + underscore

class ApiKeyCreatedResponse(ApiKeySummary):
    token: str               # full token; shown once

class ApiKeySummary(BaseModel):
    id: UUID
    name: str
    prefix: str              # first 12 chars
    scopes: list[str]
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
```

### MFA

```python
class EnrollResponse(BaseModel):
    secret: str              # base32 TOTP secret
    otpauth_uri: str         # drop into Authenticator / 1Password / Authy

class ConfirmRequest(BaseModel):
    secret: str              # same value returned by /enroll
    code: str                # 6-digit TOTP

class ConfirmResponse(BaseModel):
    backup_codes: list[str]  # 10 single-use codes; store securely

class VerifyRequest(BaseModel):
    challenge_token: str     # from /login
    code: str                # TOTP or backup code

class DisableRequest(BaseModel):
    password: str
    code: str
```

## Examples

### Local registration + login

```bash
# Register (if NEXUS_AUTH_LOCAL_ALLOW_REGISTRATION=true)
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H 'content-type: application/json' \
  -d '{"email":"alice@example.com","password":"hunter2hunter2"}'
# => {"access_token": "...", "refresh_token": "...", "expires_in": 3600}

# Login
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"alice@example.com","password":"hunter2hunter2"}'
# => {"access_token": "...", "refresh_token": "...", "expires_in": 3600}
# or, if MFA enabled:
# => {"mfa_required": true, "challenge_token": "..."}

# Refresh
curl -X POST http://localhost:8000/api/v1/auth/refresh \
  -H 'content-type: application/json' \
  -d '{"refresh_token": "<refresh>"}'

# Whoami
curl http://localhost:8000/api/v1/auth/me \
  -H 'authorization: Bearer <access>'

# Logout (revoke all my sessions)
curl -X POST http://localhost:8000/api/v1/auth/logout \
  -H 'authorization: Bearer <access>'
```

### MFA enrollment

```bash
# 1) Begin
curl -X POST http://localhost:8000/api/v1/auth/mfa/enroll \
  -H 'authorization: Bearer <access>'
# => {"secret": "JBSWY3DPEHPK3PXP", "otpauth_uri": "otpauth://..."}

# 2) Confirm with a 6-digit code from Authenticator
curl -X POST http://localhost:8000/api/v1/auth/mfa/enroll/confirm \
  -H 'authorization: Bearer <access>' \
  -H 'content-type: application/json' \
  -d '{"secret": "JBSWY3DPEHPK3PXP", "code": "123456"}'
# => {"backup_codes": ["abcd1234", ...]}  ← store these

# Next /login returns {"mfa_required": true, "challenge_token": "..."}

# 3) Verify at login
curl -X POST http://localhost:8000/api/v1/auth/mfa/verify \
  -H 'content-type: application/json' \
  -d '{"challenge_token": "<challenge>", "code": "123456"}'
# => {"access_token": "...", "refresh_token": "..."}
```

### API key for automation

```bash
# Issue (token returned exactly once)
curl -X POST http://localhost:8000/api/v1/auth/api-keys \
  -H 'authorization: Bearer <access>' \
  -H 'content-type: application/json' \
  -d '{"name":"nightly-rebalancer","scopes":["trade"],"env":"live"}'
# => {"id": "...", "name": "...", "prefix": "nxs_live_aB3",
#     "token": "nxs_live_aB3cD...<rest>", "scopes": ["trade"], ...}

# Use it
curl http://localhost:8000/api/v1/portfolio/ \
  -H 'x-api-key: nxs_live_aB3cD...'

# List + revoke
curl http://localhost:8000/api/v1/auth/api-keys \
  -H 'authorization: Bearer <access>'

curl -X DELETE http://localhost:8000/api/v1/auth/api-keys/<id> \
  -H 'authorization: Bearer <access>'
```

## Errors

| Status | When |
|---|---|
| `400` | Bad OAuth state cookie, missing `code`/`state`, malformed request |
| `401` | Bad credentials, expired/revoked token, MFA challenge expired or wrong code |
| `403` | `auth_local_allow_registration=false` and `/register` was hit |
| `404` | Unknown `{provider}` in `/auth/{provider}/authorize` |
| `409` | Email collision on `/register`; MFA already enabled on `/enroll` |

## Federated providers

Enabled by `NEXUS_AUTH_PROVIDERS` (comma-separated). Order matters only
for log output; each is independent.

| Provider | Env vars required | Notes |
|---|---|---|
| `local` | none | Email/password. Always-on if listed. |
| `google` | `NEXUS_GOOGLE_CLIENT_ID`, `NEXUS_GOOGLE_CLIENT_SECRET`, `NEXUS_GOOGLE_REDIRECT_URI` | OAuth2 |
| `github` | `NEXUS_GITHUB_CLIENT_ID`, `NEXUS_GITHUB_CLIENT_SECRET`, `NEXUS_GITHUB_REDIRECT_URI` | OAuth2 |
| `oidc` | `NEXUS_OIDC_DISCOVERY_URL`, `NEXUS_OIDC_CLIENT_ID`, `NEXUS_OIDC_CLIENT_SECRET`, `NEXUS_OIDC_REDIRECT_URI` | Generic OIDC; role claim configurable |
| `ldap` | `NEXUS_LDAP_SERVER_URL`, `NEXUS_LDAP_BIND_DN`, `NEXUS_LDAP_BIND_PASSWORD`, `NEXUS_LDAP_SEARCH_BASE` | Bind + search |

Role-overwrite behaviour is gated by `NEXUS_AUTH_OVERWRITE_ROLE_ON_LOGIN`
(default `false`). When `false`, a federated login never overwrites a
role that was already granted locally — defense-in-depth against a
misconfigured or compromised IdP. See SEV-741 and
[`docs/adr/0002-auth-rbac.md`](../adr/0002-auth-rbac.md).

## Further reading

- [ADR-0002 — Auth & RBAC](../adr/0002-auth-rbac.md)
- [auth-mfa runbook](../operations/runbooks/auth-mfa.md)
- [Backup & recovery of MFA-encrypted columns](../operations/backup-and-recovery.md#recovering-mfa-encrypted-columns)
