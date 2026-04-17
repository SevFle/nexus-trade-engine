# ADR-0002: Authentication & Role-Based Access Control

**Status:** Proposed
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
