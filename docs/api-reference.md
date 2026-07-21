# API Reference

The engine exposes one FastAPI app under `/api/v1/*` (plus `/health`,
`/ready`, `/metrics` outside the v1 prefix). OpenAPI is auto-generated
at `/docs` and `/redoc` when the engine is running; this section is
the curated, narrative version that explains *why* each surface
exists, what it requires, and how it behaves on error.

This page covers **conventions that apply to every route**: the auth
model, the legal-acceptance gate, error semantics, identifier
validation, and the middleware stack. The per-route catalogs live in
two sibling pages that grew too large to keep inline:

- [REST routes](api-reference/routes.md) — every HTTP endpoint,
  grouped by router module, with body shapes, status codes, and the
  rationale for the non-obvious ones.
- [WebSocket API](api-reference/websocket.md) — both `/api/v1/ws` and
  `/api/v1/ws/events`, the wire protocol, channel/room model, and
  cross-replica event delivery.

All routes are mounted by [`engine/api/router.py`](../engine/api/router.py).
Two cross-cutting probes worth calling out at the top because they sit
outside the auth model:

- **`GET /api/v1/tasks/status`** — TaskIQ-broker liveness. Deliberately
  **unauthenticated** because load balancers / CI hit it during deploys;
  always returns 200 with a real `broker: "running" | "stopped"` field
  derived from the broker's own state so a probe actually catches a dead
  queue without tripping orchestrator restarts. See
  [routes.md](api-reference/routes.md#tasks).
- **`GET /ready`** — orchestrator readiness hook. Real DB + Valkey probes.

## Authentication

Every protected route accepts either:

- `Authorization: Bearer <jwt>` — short-lived JWT issued by
  `POST /api/v1/auth/login` or `/register`/`/refresh`/`/auth/{provider}/callback`.
- `X-API-Key: nxs_<prefix>_<secret>` — long-lived, bcrypt-hashed API
  key (gh#94). The plaintext secret is returned **exactly once** on
  creation. Issued by `POST /api/v1/auth/api-keys`.

JWT and API-key paths share [`get_current_user`](../engine/api/auth/dependency.py);
the dependency stashes the active `ApiKey` on `request.state` when
present, so scope checks downstream don't re-authenticate.

<a id="roles-rbac-hierarchy"></a>
### Roles (RBAC hierarchy)

Enforced via `Depends(require_role("developer"))` etc. Numeric levels
in [`engine/api/auth/dependency.py`](../engine/api/auth/dependency.py#L27):

| Role | Level |
|---|---|
| `viewer` | 0 |
| `user` | 1 |
| `retail_trader` | 2 |
| `quant_dev` | 3 |
| `developer` | 4 |
| `portfolio_manager` | 5 |
| `admin` | 6 |

A request with role `R` satisfies `require_role(X)` iff
`level(R) >= level(X)`.

<a id="roles-rbac-exact-set"></a>
#### Exact-set enforcement: `require_roles()`

`require_roles(*roles: str)` (added gh#1597, hardened gh#1601) admits
**only** users whose `role` is exactly one of the supplied names —
no hierarchy. It is the right dependency when a route must be locked to
a specific role set without granting implicit access to more privileged
roles (e.g. an audit-log viewer that only `admin` and `developer` may
access, even though a hypothetical `super_admin` would normally
outrank both).

Key behaviours:

- Empty `roles` → `ValueError` at registration time (an empty
  allow-list would lock every principal, including admins — almost
  always a misconfiguration).
- The allowed set is frozen into a `set` once at registration time
  for O(1) lookup. A sorted copy is materialized once for stable
  audit-log messages.
- Denied requests emit an `rbac.deny` structlog warning with the
  caller's `role`, the allowed set, `request.url.path`, and
  `request.method` — logging is guarded by `contextlib.suppress` so a
  log failure never alters the 403 decision.
- Injected as `Depends(require_roles("admin", "developer"))` —
  composable with `get_current_user` (the RBAC check chains after
  authentication).

Audit note: `require_roles()` is currently exported from
`engine/api/auth/__init__.py` but not yet applied to any route. It
exists for routes that need exact-set gating once the role vocabulary
stabilises.

### API-key scopes

Hierarchy (gh#86): `admin > trade > read`. JWT-authenticated requests
bypass scope enforcement — JWTs are gated by role instead. API keys
that lack the required scope get `403`.

- `read` — GET / HEAD only.
- `trade` — POST / PUT / PATCH for backtest, portfolio, webhooks, etc.
- `admin` — equivalent to the `admin` role.

### Legal acceptance gate

`backtest`, `scoring`, `market-data`, `marketplace`, `portfolio`, and
`strategies` routers are mounted with
`Depends(require_legal_acceptance)`. Callers without an
`acceptances` row for the *current version* of every
`requires_acceptance` document get **`451 Unavailable For Legal
Reasons`** with body `{code:"legal_re_acceptance_required",
documents:[<slug>, …]}` (not `403` — the dedicated code lets clients
and the frontend distinguish a consent gate from an RBAC denial; see
[`engine/legal/dependencies.py`](../engine/legal/dependencies.py)).
An unauthenticated request hits `401` first: the dependency's
principal guard treats both an unresolved `Depends` marker and an
explicit `None` as "no user" so the gate can't be silently bypassed
when it is invoked outside FastAPI DI. Acceptance is recorded via
`POST /api/v1/legal/accept`. See [`data-model.md`](data-model.md) for
the immutable acceptance table.

Legal acceptance is wired in two places: most routers declare it at
the `APIRouter(dependencies=…)` level
([`portfolio.py`](../engine/api/routes/portfolio.py),
[`strategies.py`](../engine/api/routes/strategies.py),
[`marketplace.py`](../engine/api/routes/marketplace.py),
[`scoring.py`](../engine/api/routes/scoring.py)); `backtest` and
`market-data` get it from the top-level include in
[`router.py`](../engine/api/router.py). `reference`, `tax`, `webhooks`,
`privacy`, and `auth` are **not** gated — they need to be reachable
before the user has accepted anything (e.g. the legal docs UI itself
calls `/reference/suggest` to render attributions).

### Providers in the registry vs. routes on the wire

`NEXUS_AUTH_PROVIDERS` is a CSV of providers that
[`create_app()._build_auth_registry()`](../engine/app.py) loads lazily
and registers: `local`, `google`, `github`, `oidc`, `ldap`. **Being in
the registry is not the same as being reachable from an HTTP route.**
The only routes that exercise `registry.authenticate` are
`POST /api/v1/auth/login` (hard-coded to `"local"`) and
`GET /api/v1/auth/{provider}/callback` (OAuth-shaped — it expects a
`code` and validates an OAuth state cookie, which does not fit LDAP's
username/password flow). Consequence:

- `local`, `google`, `github`, `oidc` are reachable end-to-end.
- **`ldap` is registered but has no route.** The provider is callable as
  a library (`registry.authenticate("ldap", username=…, password=…,
  db=…)`) but no `/auth/ldap/login` (or similar) endpoint exists. A
  second, more robust `ldap3`-backed LDAP provider also landed in
  [`engine/auth/providers/ldap.py`](../engine/auth/providers/ldap.py)
  (PR #1368) and is *also* not wired. See
  [known-limitations.md](known-limitations.md#ldap-has-no-route).

---

## Errors

- **Auth**: `401` for missing/invalid/expired credentials; `403` for
  insufficient role/scope; **`451`** for missing legal acceptance
  (body `{code:"legal_re_acceptance_required", documents:[…]}`).
- **Validation**: `422` from FastAPI; `400` for hand-rolled checks
  (e.g. invalid scope in API keys, unknown tax jurisdiction).
  Identifiers in user-controlled path params (`{strategy_id}` on
  `/strategies/*`, `{strategy_name}` on `/scoring/*`) are validated
  up front by the shared `SafeIdentifier` pattern in
  [`engine/api/validators.py`](../engine/api/validators.py) — see
  [Validation](#validation).
- **Rate limit**: `429` with `Retry-After` from
  [`RateLimitMiddleware`](../engine/api/rate_limit.py). Default 600
  req/min/IP, burst 60. `/health` and `/metrics` are exempt;
  `/api/v1/client/errors` is capped at 30/min to prevent log DoS.
- **Body size**: hard 1 MiB cap on every request
  ([`BodySizeLimitMiddleware`](../engine/api/body_size_limit.py)).
- **Provider errors**: see [Market data](api-reference/routes.md#market-data).

<a id="validation"></a>
## Validation

User-controlled identifier path params (`{strategy_id}` on
`/strategies/*`, `{strategy_name}` on `/scoring/*`) are validated by
the shared [`SafeIdentifier`](../engine/api/validators.py) alias —
an `Annotated[str, Path(...)]` that bundles a regex pattern and a
length cap so every route module enforces the *same* contract without
re-deriving it.

- Pattern: `^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*$` — one or more
  dot-free tokens separated by single dots.
- Max length: 64 characters.
- Grammar: alphanumerics, underscore, hyphen, and `.` **only as a
  separator** between non-empty tokens. Leading/trailing dots,
  consecutive dots (`..`), and any other character class are
  rejected.
- Effect: non-conforming values return `422 Unprocessable Entity`
  from FastAPI *before* the handler runs, so a hostile identifier
  can never reach a registry lookup, a DB query, or a reflected
  error `detail`.

The pattern is deliberately written without look-around assertions
because Pydantic v2 compiles it with the Rust `regex` crate, which
does not support lookahead/look-behind; the "token (`.` token)*"
formulation achieves the dot discipline constructively. Dotted
namespacing (`mypackage.v2`) is accepted so versioned strategy
packages keep working — a regression test for that exact shape lives
in [`tests/test_validators.py`](../tests/test_validators.py).

<a id="cross-cutting-middleware"></a>
## Cross-cutting middleware

Applied in reverse order in [`create_app`](../engine/app.py#L154) so the
last-added wraps everything:

1. `SecurityHeadersMiddleware` — CSP, HSTS, X-Content-Type-Options, …
2. `CORSMiddleware` — `NEXUS_CORS_ORIGINS` (defaults to `http://localhost:3000`).
3. `RateLimitMiddleware`
4. `BodySizeLimitMiddleware` (1 MiB)
5. `CorrelationIdMiddleware` — stamps `X-Request-ID`.
6. `HttpMetricsMiddleware` — Prometheus histogram + counter for every
   route (including `/metrics` itself, deliberately, so scrape latency
   is observable).
