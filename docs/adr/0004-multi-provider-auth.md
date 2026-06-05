# ADR-0004: Multi-provider auth (Local + OAuth + OIDC + LDAP)

**Status:** Accepted
**Date:** 2026-05-20
**Supersedes:** parts of [ADR-0002](0002-auth-rbac.md) (which
proposed a single `AuthBackend` protocol — what shipped is the more
specific `AuthProvider` interface plus a registry).
**Tracks:** SEV-741 (silent role escalation fix), gh#86 (pluggable auth)

## Context

ADR-0002 sketched a single-protocol auth layer. By the time we
went to wire federated providers, the actual requirement was
broader:

- Local email/password for self-hosted operators with no IdP.
- Google + GitHub OAuth2 for developer SSO.
- Generic OIDC for enterprise IdPs (Okta, Auth0, Keycloak).
- LDAP for on-prem deployments that already run Active Directory
  or OpenLDAP.

Forcing all of these through one shape would have produced either
(a) a leaky abstraction (LDAP queries leaking into the OAuth
handler) or (b) a generic bag-of-dicts API that the type-checker
could not reason about.

## Decision

Adopt a **registry of `AuthProvider` implementations**, each
owning its own protocol. The engine mounts a provider when its
slug appears in `NEXUS_AUTH_PROVIDERS`; the registry routes
`authenticate(provider=..., **creds)` to the right implementation.

```
AuthProviderRegistry
  ├── LocalAuthProvider      # email + password (bcrypt)
  ├── GoogleAuthProvider     # OAuth2 (Google)
  ├── GitHubAuthProvider     # OAuth2 (GitHub)
  ├── OIDCAuthProvider       # generic OIDC
  └── LDAPAuthProvider       # direct bind
```

Each provider implements:

```python
async def authenticate(self, *, db, **creds) -> AuthResult: ...
async def create_user(self, *, user_info, password, db) -> AuthResult: ...
def get_authorize_url(self, state: str) -> str: ...   # OAuth/OIDC only
```

The HTTP layer
([`engine/api/routes/auth.py`](../../engine/api/routes/auth.py))
is provider-agnostic — `POST /login` calls
`registry.authenticate("local", ...)`, the OAuth callback calls
`registry.authenticate(provider, code=..., ...)`, and the response
shape is always `TokenResponse` or `MFARequiredResponse`.

## Role overwrite policy (SEV-741)

By default the engine **does not** overwrite a local user's role
based on what a federated IdP asserts. The setting
`NEXUS_AUTH_OVERWRITE_ROLE_ON_LOGIN=false` is the safe default;
operators opt in explicitly by setting it to `true`. The earlier
behaviour silently called `dict.update()` on every login — a
misconfigured or compromised IdP could escalate or downgrade
roles invisibly.

## Consequences

**Positive**
- Adding a new provider is a single file + one entry in
  `_build_auth_registry()`. No changes to routes or models.
- The same user can authenticate via different providers on
  different sessions; `(auth_provider, external_id)` is the
  unique key, not `email`.
- The registry pattern extends naturally to MFA (already
  implemented as a separate concern layered on top of any
  provider).

**Negative**
- Federated login flow is three round-trips (authorize → callback
  → token mint) versus one for local. Frontend has to know this.
- LDAP requires a Python C extension (`python-ldap`); we made it
  an optional dependency (`pip install nexus-trade-engine[ldap]`)
  so the base image stays slim.
- Same-email cross-provider linking is not automatic. If
  `alice@example.com` exists locally and signs in via Google for
  the first time, two accounts are created. Resolution requires
  an admin script (not yet built).

## Alternatives considered

- **Single `AuthBackend` protocol** (the original ADR-0002 design).
  Rejected for the leaky-abstraction reason above.
- **Pass off to an OAuth2 proxy (Pomerium / oauth2-proxy)**.
  Reasonable for self-hosted but breaks the embedded-SDK case
  (long-lived API keys, MCP server).
- **Auth0 / Clerk / Stytch**. Vendor lock-in for an OSS-trending
  project. The registry lets operators wire one in themselves.

## Open questions

- Account-linking flow: do we want a first-class `/auth/link`
  endpoint that merges two user rows? Not yet; requires careful
  thought around which provider owns the role.
- WebAuthn / passkeys. Not in scope for this ADR; will get its own
  when prioritised.
- Per-provider MFA enforcement. Today MFA is user-level; an
  enterprise IdP may require MFA only for federated logins.
