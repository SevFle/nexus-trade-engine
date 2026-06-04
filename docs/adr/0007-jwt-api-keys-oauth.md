# ADR-0007: JWT + API keys + OAuth2-family; no sessions

- **Status**: Accepted
- **Date**: 2026-05-04
- **Deciders**: lead maintainer + security reviewer
- **Tags**: auth, security, api

## Context and Problem Statement

The engine has two distinct caller populations:

- **Interactive users** — humans clicking around the React
  dashboard, or using the SDK from a notebook. Their sessions are
  short-lived, MFA-protected, and brokered by a browser.
- **Headless clients** — CI pipelines, scheduled jobs, MCP-style
  agents. Their sessions are long-lived, must survive process
  restarts, and never have a human in the loop to re-enter a
  password.

A single auth mechanism serves both populations badly: server-side
sessions force headless clients to fake a login flow; long-lived
JWTs are unsafe for browser sessions because they cannot be
revoked without a server-side blocklist.

## Decision Drivers

- **Operational simplicity.** The auth model must be a single
  dependency stack the operator has to understand. Two parallel
  stacks is acceptable; three is not.
- **Revocation.** Interactive tokens must be revocable from the UI
  without rotating a server-wide secret.
- **Auditability.** Every authenticated request must resolve to a
  user id and, for API keys, a specific key id. "Anonymous but
  valid" is not a thing.
- **OAuth2-family support.** Enterprises want to bring their own
  identity (Google, GitHub, OIDC, LDAP). The framework should
  accept these without re-architecting.

## Considered Options

1. **JWT for interactive + API keys for headless + OAuth2-family
   for federated login** (chosen).
2. **Server-side sessions for everyone** — Flask/Django-style,
   cookie-based.
3. **Long-lived JWTs for everyone** — issue one token per
   principal, never expire it, rotate via re-login.
4. **OAuth2-only** — delegate to an external IdP, accept whatever
   token they hand back.

## Decision Outcome

Chosen option: **Option 1 — JWT for interactive, API keys for
headless, OAuth2-family for federated login**, all behind a single
`AuthProviderRegistry` (`engine/api/auth/registry.py`).

The FastAPI auth dependency (`engine/api/auth/dependency.py`)
inspects the credential on every request and routes it to the
right verifier:

- Bearer JWT → `decode_token` → user lookup by `sub` claim.
- Bearer engine API key (`nxs_*`) or `X-API-Key` header → bcrypt
  hash lookup against `api_keys.key_hash`.
- OAuth2/OIDC/LDAP → these are login-time providers; on success
  they mint a Nexus JWT (and optionally an API key) and the rest
  of the request flow uses that.

### Consequences

- **Positive** — interactive sessions get MFA, refresh-token
  rotation, and revocation via the `refresh_tokens` table.
- **Positive** — headless clients get scoped API keys (`read` <
  `trade` < `admin`) with per-key last-used tracking and revocation.
- **Positive** — federated login is a plugin: `local`, `google`,
  `github`, `oidc`, `ldap` are all configured by
  `NEXUS_AUTH_PROVIDERS` and live behind the same `AuthProvider`
  interface. Operators can add a new IdP without touching core
  auth.
- **Negative** — the auth dependency does two distinct lookups
  (JWT path vs API-key path) on every request. We accept the
  overhead; both paths are O(1) DB hits and the user is cached on
  `request.state` for the rest of the request.
- **Negative** — token replay detection for refresh tokens
  requires a write on every refresh. We accept this — it's the
  price of correct rotation.

## Pros and Cons of the Options

### Option 1 — JWT + API keys + OAuth2-family (chosen)

- **Pros:** each population gets the right token type; revocation
  works for both; federated login is a plugin.
- **Cons:** two token formats in the codebase; API keys are
  one-shot-displayed and lost forever if the operator doesn't save
  them.

### Option 2 — server-side sessions for everyone

- **Pros:** simpler mental model; revocation is trivial (delete
  the session row).
- **Cons:** headless clients must fake a browser; cross-origin
  cookie handling is painful for SDK users; doesn't compose with
  the planned MCP server.

### Option 3 — long-lived JWTs for everyone

- **Pros:** stateless server; minimal DB load.
- **Cons:** no safe revocation without a server-side blocklist
  (which is just sessions reinvented); browser storage of
  long-lived JWTs is an XSS exfiltration risk.

### Option 4 — OAuth2-only

- **Pros:** zero in-house auth code.
- **Cons:** forces every operator to pick an IdP; blocks local dev
  without an IdP running; loses the API-key flow that the SDK
  relies on.

## Implementation notes

- RBAC roles: `viewer < user < retail_trader < quant_dev <
  developer < portfolio_manager < admin` (see
  `ROLE_HIERARCHY` in
  [`engine/api/auth/dependency.py`](../../engine/api/auth/dependency.py)).
  Routes use `require_role("developer")` to gate by role.
- API-key scopes: `read < trade < admin` (see
  `_SCOPE_HIERARCHY` in the same file). Routes use
  `require_api_scope("trade")` to gate by scope. JWT requests
  bypass scope checks (they're gated by role instead).
- MFA: TOTP with Fernet-encrypted secrets at rest
  (`NEXUS_MFA_ENCRYPTION_KEY`). Backup codes are bcrypt-hashed.
  See [`engine/api/auth/mfa_service.py`](../../engine/api/auth/mfa_service.py).
- Refresh-token replay detection: every refresh atomically
  flips `revoked_at` from NULL to now; if the row was already
  revoked, every other live refresh token for that user is also
  revoked (token-replay signal). See
  [`engine/api/routes/auth.py:refresh_token`](../../engine/api/routes/auth.py).
- OAuth2 state cookies are HttpOnly + SameSite=Lax + Secure in
  production; stored under the path-scoped key
  `oauth_state_<provider>`.

## Links

- Implementation: [`engine/api/auth/`](../../engine/api/auth/),
  [`engine/api/routes/auth.py`](../../engine/api/routes/auth.py),
  [`engine/api/routes/api_keys.py`](../../engine/api/routes/api_keys.py),
  [`engine/api/routes/mfa.py`](../../engine/api/routes/mfa.py).
- Models: [`engine/db/models.py`](../../engine/db/models.py)
  (`User`, `RefreshToken`, `ApiKey`).
- Supersedes: ADR-0002 (auth & RBAC) — this ADR captures the
  concrete shape ADR-0002 sketched.
