# ADR-0002: Authentication & Role-Based Access Control

**Status:** Accepted — implemented 2026-04 → 2026-07, **diverging from the proposal below** in naming, package layout, and scope (OIDC/LDAP/MFA/API-keys all shipped instead of being deferred). See [Evolution — how this actually landed](#evolution--how-this-actually-landed) at the end of this ADR for the as-built record; the original proposal is preserved verbatim above it as decision history.
**Date:** 2026-04-17
**Tracks:** SEV-233 (gh#86), SEV-273 (gh#9) — closed as duplicate of this ADR
**Owner:** TBD

## Context

The engine currently has zero authentication. Every API route is reachable by anyone who can hit the host. Before any non-localhost deploy — let alone exposing live broker integration (SEV-266) — we must gate the API behind authentication and a permission model.

Two existing issues describe this:
- **SEV-273 (gh#9)**: "Implement user authentication and RBAC" (smaller scope, JWT-only)
- **SEV-233 (gh#86)**: "Pluggable authentication system with RBAC (JWT, OAuth2, LDAP, OIDC)" (larger pluggable scope)

This ADR consolidates them. SEV-273 should be closed as superseded.

## Decision

Adopt a **two-layer authentication architecture**: a thin pluggable interface in front, with a sane default backend (JWT-on-Postgres) shipped first.

### Layer 1 — `AuthBackend` protocol (in `engine/auth/backend.py`)

```python
class AuthBackend(Protocol):
    async def authenticate(self, request: Request) -> Identity | None: ...
    async def issue_token(self, user_id: UUID, scopes: list[str]) -> Token: ...
    async def revoke_token(self, token_id: str) -> None: ...
```

Plugins ship as separate modules: `engine.auth.backends.jwt_local`, `engine.auth.backends.oauth2_proxy` (later), `engine.auth.backends.oidc` (later). Selected via `NEXUS_AUTH_BACKEND` env var.

### Layer 2 — `RBAC` enforcement (in `engine/auth/rbac.py`)

Resource × action grid stored in DB. Three default roles to start:

| Role | Read | Write | Live trading | Admin |
|---|---|---|---|---|
| `viewer` | ✓ | — | — | — |
| `trader` | ✓ | ✓ | ✓ (own portfolios) | — |
| `admin` | ✓ | ✓ | ✓ (any portfolio) | ✓ |

Enforced via FastAPI dependency: `Depends(require_role("trader"))` or `Depends(require_scope("portfolio:write"))`.

### v1 backend: JWT-on-Postgres

- HS256 signed (RS256 swap is a one-line config change later)
- Tokens stored hashed in `auth_tokens` table for revocation
- 24h TTL on access tokens, 30d on refresh tokens
- Secret rotation via `NEXUS_JWT_SECRET_ROTATE` env (dual-key window)
- Argon2id for password hashing

### Out of scope for v1
- OAuth2/OIDC backends (SSO providers)
- LDAP backend (enterprise on-prem)
- MFA (added in a follow-up)
- Session management UI
- Per-tenant multi-org

These each get their own ADR when they land.

## Implementation phases

1. **DB schema** (~½ day) — Alembic migration `004_auth_users_roles.py`: `users`, `roles`, `user_roles`, `auth_tokens` tables.
2. **AuthBackend protocol + JWT default impl** (~1 day) — pure logic, unit-tested, no FastAPI dependency.
3. **FastAPI integration** (~½ day) — `Depends(get_current_user)`, `require_role`, `require_scope` helpers.
4. **Route protection** (~1 day) — apply to every existing route in `engine/api/routes/`. Add `@public` marker for `/health`, `/ready`, `/metrics`.
5. **CLI bootstrapping** (~½ day) — `nexus user create`, `nexus user grant-role` so first-run works without UI.
6. **Frontend integration** (~1 day) — login page, token storage in `httpOnly` cookie, axios interceptor refresh.
7. **Migration guide for existing deployments** (~½ day) — there are none today, but document the upgrade path.

**Total**: ~5 days of focused work.

## Consequences

**Positive**
- Engine becomes deployable beyond localhost.
- Live trading routes (SEV-266 Alpaca etc.) can land safely behind `require_role("trader")`.
- Plugin architecture means OIDC/LDAP land later without touching the core.

**Negative**
- All current API consumers (the in-repo frontend, any local dev scripts) must obtain a token. We need a `nexus dev-token` CLI helper to keep DX tolerable.
- JWT secrets are now deployment-critical secrets. Add to runbook + secret rotation procedure.
- Revocation requires DB hits per request — accept the overhead for v1; cache lookups in Valkey if it becomes a problem (premature optimization otherwise).

## Alternatives considered

- **Session cookies only (no JWT)**: simpler, but doesn't compose well with the planned MCP server (SEV-223) where token-based auth is more natural.
- **External OAuth2 proxy (oauth2-proxy / Pomerium)**: punts auth to infra. Reasonable for self-hosted, but doesn't help embedded use cases (SDK clients, MCP, CI). Plugin design lets users opt into this later.
- **Auth0/Clerk/Stytch SaaS**: fastest path, but introduces vendor lock-in for an OSS-trending project. Plugin design lets users wire one in if they want.

## Open questions
- Do we need per-portfolio ACLs in v1, or is per-user RBAC sufficient until multi-tenant lands?
- Should the JWT include scope claims, or look them up per request? (Trade-off: token size vs. dynamic permission updates.)
- API key auth (long-lived, scoped) for SDK clients — v1 or follow-up?

---

<a id="evolution--how-this-actually-landed"></a>
## Evolution — how this actually landed

The decision above was **accepted in spirit** but the implementation,
landed across SEV-233 / SEV-273 and the subsequent auth PRs (most recently
Google OAuth #1281, WS token auth #1271), took a **different concrete
shape**. This section is the as-built record so the proposal and the code
agree. The original decision text is left intact above as history.

### What changed from the proposal

| Proposal (above) | As-built (code) |
|---|---|
| `engine/auth/backend.py` defines an `AuthBackend` `Protocol` | [`engine/api/auth/registry.py`](../../engine/api/auth/registry.py) defines `AuthProviderRegistry`; providers implement the **ABC** `IAuthProvider` in [`engine/api/auth/base.py`](../../engine/api/auth/base.py) (not a runtime `Protocol`). |
| Backends live under `engine.auth.backends.*` | Providers live under [`engine/api/auth/`](../../engine/api/auth/): `local.py`, `google.py`, `github_oauth.py`, `oidc.py`, `ldap.py`. (`engine/auth/providers/` also exists but holds a legacy/alternate Google provider — the active set is in `api/auth/`.) |
| Selected via single `NEXUS_AUTH_BACKEND` env var | Selected via **`NEXUS_AUTH_PROVIDERS`** — a comma-separated list, so multiple providers coexist. Built by `create_app()._build_auth_registry()` ([`engine/app.py`](../../engine/app.py)), which `match`-loads each provider lazily. |
| v1 ships **only** JWT-on-Postgres; OAuth2/OIDC/LDAP explicitly **out of scope** | **All five providers shipped**: `local`, `google`, `github`, `oidc`, `ldap` are all importable and wired by `_build_auth_registry`. Their config knobs (`google_client_id`, `github_client_*`, `oidc_discovery_url`, `ldap_server_url`, …) are all in [`engine/config.py`](../../engine/config.py). (Feature status is still *partial* — see [`known-limitations.md`](../known-limitations.md).) |
| MFA **out of scope for v1** ("added in a follow-up") | **Shipped** — [`engine/api/auth/mfa.py`](../../engine/api/auth/mfa.py) + [`mfa_service.py`](../../engine/api/auth/mfa_service.py): TOTP with Fernet-encrypted secrets at rest, challenge TTL, and bcrypt-hashed backup codes. At-rest model is ADR-0006. |
| API keys listed as an **open question** | **Shipped** — [`engine/api/auth/api_keys.py`](../../engine/api/auth/api_keys.py) + the `api_keys` table (migration 011). `nxs_*` tokens work on the REST `get_current_user` path. **Note:** they do **not** yet open WebSocket connections — see [`known-limitations.md`](../known-limitations.md) ("WebSocket does not accept API keys"). |
| 3 roles: `viewer` / `trader` / `admin` via a `user_roles` join table | **7-role hierarchy** in `IAuthProvider.map_roles` (`_ROLE_PRIORITY`): `viewer(0)` < `user(1)` < `retail_trader(2)` < `quant_dev(3)` < `developer(4)` < `portfolio_manager(5)` < `admin(6)`. Stored as a **single** `users.role` column (not a join); the REST layer enforces a separate `ROLE_HIERARCHY`. External IdP roles are sanitized via `_sanitize_role` (NFKC + control-char strip + allowlist) so a hostile IdP cannot inject a spoofed `admin`. |
| `auth_tokens` table (hashed, revocable) | Named **`refresh_tokens`** in the as-built schema ([`engine/db/models.py`](../../engine/db/models.py)). Revocation is via refresh-token rows; access tokens are stateless HS256 JWTs. |
| Password hashing: **Argon2id** | **bcrypt** (`hashed_password` on `users` — see [`data-model.md`](../data-model.md) and ADR-0006). |
| JWT: HS256 now, RS256 "a one-line config change later" | Still HS256; the RS256 swap remains a TODO, not yet a config knob. |

### What held

- The **two-layer shape** (pluggable provider in front, RBAC enforcement
  behind, default to a JWT-on-Postgres backend) is exactly what shipped —
  only the names moved (`AuthBackend`→`IAuthProvider`, `engine/auth/`→
  `engine/api/auth/`).
- **JWT-on-Postgres** remains the default (`local` provider, HS256,
  refresh tokens in the DB).
- **RBAC via FastAPI dependency** (`require_role` / `get_current_user`)
  is the enforcement model actually in use.
- The **alternatives considered** (session-only, external OAuth2 proxy,
  hosted Auth0/Clerk) were all rejected for the reasons recorded above.

### Why the divergence

The provider ABC + registry landed simpler to extend than a single-backend
`Protocol` + `NEXUS_AUTH_BACKEND` switch: each provider is a self-contained
module behind a uniform `IAuthProvider.authenticate(**kwargs)` surface,
and `NEXUS_AUTH_PROVIDERS` lets operators enable several at once (e.g.
`local,google`) without a code change. Deferring OIDC/LDAP proved
unnecessary once `IAuthProvider` made each one a ~one-file job, so they
were pulled forward rather than cut. MFA and API keys were pulled forward
for the same reason and because the MCP + WS surfaces (#1271) needed
token-bearing non-interactive clients.

### Open follow-ups still true

- Unify the **two Alpaca-style adapters** concern (here, the **two auth
  provider roots**): `engine/api/auth/providers/` vs `engine/auth/providers/`
  both exist. `create_app()` wires the `api/auth/` set; the `engine/auth/`
  tree is the legacy/SDK-facing one. Consolidate before more providers land.
- The per-portfolio ACL vs per-user RBAC open question is still open —
  RBAC alone is in force.
- **Two parallel provider roots — now three providers wide.** The split
  is no longer just the Google pair recorded when this section was first
  written: the `engine/auth/` tree (a standalone, protocol-step library
  whose modules decompose each OAuth2/OIDC flow into independently
  testable steps and whose docstrings say they "complement" `api/auth`)
  now hosts a **third** standalone provider beside Google. As-built:
  - `engine/auth/providers/google.py` (`GoogleOAuthProvider` +
    `IDTokenClaims`) — the original alternate; not wired.
  - `engine/auth/github.py` (`GitHubOAuthProvider`, `GitHubUserInfo`) —
    standalone; not wired into the runtime registry.
  - `engine/auth/oidc.py` (`OIDCProvider` + `IDTokenClaims`, PR #1633) —
    a generic, issuer-configurable OIDC client with JWKS caching,
    `alg=none` rejection, and an injectable `_JWKSClient`/transport for
    tests; **not** wired into the runtime registry.
  None of these are imported by the matching `api/auth/` adapter, and
  `create_app()._build_auth_registry()` resolves only the `api/auth/`
  set (`engine/auth/get_oauth_provider` can *build* the OIDC one, but
  that factory is not on the request path). The advice below therefore
  hardened: **do not land more standalone providers in `engine/auth/`
  until the split is reconciled** — formally designate `engine/auth/`
  as a reusable protocol library that the `api/auth` adapters consume,
  or fold the standalone implementations into the wired adapters.
