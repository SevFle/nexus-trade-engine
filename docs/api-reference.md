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
