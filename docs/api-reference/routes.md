# REST API — per-route reference

Every HTTP endpoint exposed by the engine, grouped by the router module
that owns it. This page is the *catalog*; cross-cutting conventions (auth
model, legal gate, error semantics, identifier validation, middleware)
live in [`api-reference.md`](../api-reference.md) — read that first.

All routes are mounted by [`engine/api/router.py`](../../engine/api/router.py).
Path parameters in `{braces}` are dynamic; everything else is literal.
Where a route deviates from the default auth/legal model, the deviation
is called out explicitly. **Auth shorthand** used below:

- **user** — `Depends(get_current_user)`; any authenticated principal
  (JWT *or* API key) is accepted.
- **role:X** — `Depends(require_role("X"))`; JWT or API key, role ≥ X
  (see [RBAC hierarchy](../api-reference.md#roles-rbac-hierarchy)).
- **scope:X** — `Depends(require_api_scope("X"))`; API keys must declare
  scope ≥ X. JWTs bypass this (they're gated by role instead).
- **public** — no auth.
- **legal** — router mounted with `Depends(require_legal_acceptance)`;
  unauthenticated → `401`, authenticated but stale → `451`.

Source-link comments next to each route point at the handler so the
prose and code stay in lockstep. Status codes listed are the *non-obvious*
ones — `200`/`201`/`204` are implied unless something else is interesting.

---

<a id="observability"></a>
## Health & infrastructure

These probes are deliberately **public** so load balancers, orchestrators
and CI can hit them during deploys without credentials. None of them
mutate state. `/metrics` is the Prometheus exposition surface; the
health routes give the per-subsystem liveness/readiness signals that
on-call uses to scope an incident.

| Method | Path | Auth | Source | Notes |
|---|---|---|---|---|
| `GET` | `/health` | public | [`health.py`](../../engine/api/routes/health.py) | Liveness — always `200 {"status":"ok"}`. |
| `GET` | `/api/v1/health` | public | same | Aliased so the k6 baseline (`GET /api/v1/health`) resolves without a `404`. |
| `GET` | `/ready` | public | same | Readiness — real DB + Valkey probes. Returns `{"status":"ok"\|"degraded","db":"ok"\|"error","valkey":"ok"\|"error"}`. **Always 200**; callers branch on the per-subsystem fields. |
| `GET` | `/health/providers` | public | same | Aggregates `registry.health()` across market-data adapters into `{status, providers:{name:{status,latency_ms,detail}}}`. Overall is `down` iff every adapter is down, else `degraded` if any isn't `up`. |
| `GET` | `/metrics` | public | [`metrics.py`](../../engine/api/routes/metrics.py) | Prometheus exposition. `NullBackend` → placeholder body (still 200); `RecordingBackend` → real scrape. Unauthenticated by design — protect with a network ACL. |

### Tasks

| Method | Path | Auth | Source | Notes |
|---|---|---|---|---|
| `GET` | `/api/v1/tasks/status` | **public** | [`tasks.py`](../../engine/api/routes/tasks.py) | TaskIQ broker liveness. **Always 200** with body `{status:"ok", broker:"running"\|"stopped", broker_online:bool}`. The HTTP code never reflects broker health (a probe must not trip orchestrator restarts); callers branch on `broker_online`. Derived from the broker's real state — `is_started` flag if present, else a bounded `PING` against the broker's connection pool. |

---

## Auth & MFA

Router prefix `/api/v1/auth` (MFA under `/api/v1/auth/mfa`). None of
these are legal-gated — a user must be able to authenticate *before*
accepting terms.

### Sessions

| Method | Path | Auth | Source | Body / Response |
|---|---|---|---|---|
| `POST` | `/api/v1/auth/register` | public | [`auth.py`](../../engine/api/routes/auth.py) | `{email, password, display_name?}` → `201 {access_token, refresh_token, token_type:"bearer", expires_in}`. Password ≥ 8 chars enforced by the local provider. `409` if email is already registered. |
| `POST` | `/api/v1/auth/login` | public | same | `{email, password}`. If MFA is enabled, returns `200 {mfa_required:true, challenge_token}` instead of tokens — the client then completes `/auth/mfa/verify`. `401` on bad credentials. |
| `POST` | `/api/v1/auth/refresh` | public | same | `{refresh_token}`. **Rotation + replay detection:** the matching row is atomically revoked (single `UPDATE … WHERE revoked_at IS NULL RETURNING …`); a second presentation of the same token is detected and triggers revocation of *every* outstanding token for that user → `401 "Token reuse detected — all sessions revoked"`. |
| `GET` | `/api/v1/auth/me` | user | same | Returns `{id, email, display_name, role, auth_provider, is_active}`. |
| `POST` | `/api/v1/auth/logout` | user | same | Optional `{refresh_token}` revokes just that session; absent body revokes **all** of the user's outstanding sessions. Always `200 {status:"logged_out"}`. |
| `GET` | `/api/v1/auth/{provider}/authorize` | public | same | Builds an OAuth authorize URL + opaque `state`, set as a 10-minute httponly cookie scoped to `/api/v1/auth`. `404` if `provider` is not in the registry. `local` is not OAuth-shaped and has no authorize URL. |
| `GET` | `/api/v1/auth/{provider}/callback` | public | same | Query params `code` + `state`. **CSRF:** `state` must match the cookie set by `/authorize` (constant-time compare), after which the cookie is deleted. `401` on mismatch/missing. On success, mints JWT + refresh and returns a `TokenResponse`. |

> **LDAP caveat:** `ldap` is registered as a provider (PR #1368, see
> [`engine/auth/providers/ldap.py`](../../engine/auth/providers/ldap.py))
> but has **no route** — `/auth/login` is hard-coded to `"local"` and
> `/auth/{provider}/callback` expects an OAuth `code`+`state` flow that
> doesn't fit LDAP's username/password shape. See
> [known-limitations.md → LDAP has no route](../known-limitations.md#ldap-has-no-route).

### MFA (TOTP)

Router prefix `/api/v1/auth/mfa`. All routes require `user` except
`/verify` (which consumes the `challenge_token` issued by `/login`).

| Method | Path | Auth | Body → Response |
|---|---|---|---|
| `POST` | `/api/v1/auth/mfa/enroll` | user | → `{secret, otpauth_uri}`. `409` if MFA already enabled. The secret is *not* persisted until `/confirm`. |
| `POST` | `/api/v1/auth/mfa/enroll/confirm` | user | `{secret, code}` → `{backup_codes[]}`. Verifies the TOTP code against the proposed secret, then encrypts (Fernet) and stores it + hashed backup codes. |
| `POST` | `/api/v1/auth/mfa/verify` | challenge_token | `{challenge_token, code}` → `TokenResponse`. Completes the login flow that `/auth/login` deferred. Backup codes are single-use (rotated out on success). `401` on bad code. |
| `POST` | `/api/v1/auth/mfa/disable` | user | `{password, code}`. Requires both factors — defence against a stolen JWT being used to weaken the account. `401` on either factor wrong. |
| `POST` | `/api/v1/auth/mfa/backup-codes/regen` | user | `{code}` → `{backup_codes[]}`. Issues a fresh set; old codes are invalidated. |

Source: [`mfa.py`](../../engine/api/routes/mfa.py). At-rest crypto
decisions (bcrypt password hash, Fernet-encrypted TOTP secret) are in
[ADR 0006](../adr/0006-bcrypt-fernet.md).

### API keys

Router prefix `/api/v1/auth/api-keys` (mounted under the `/api/v1`
include in [`router.py`](../../engine/api/router.py)). All routes
require `user`.

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/v1/auth/api-keys` | Body `{name, scopes:["read"\|"trade"\|"admin"], expires_at?, env?}` → `201` with `ApiKeyCreatedResponse` — **includes `token` (plaintext, returned exactly once)** plus summary fields (`prefix`, `scopes`, …). Invalid scopes → `400`. |
| `GET` | `/api/v1/auth/api-keys` | List the caller's keys (newest first). Tokens are never re-surfaced — only `prefix` + metadata. |
| `DELETE` | `/api/v1/auth/api-keys/{key_id}` | Soft-revoke (`revoked_at = now`). `404` if not owned by the caller. `204 No Content`. |

Token format is `nxs_<prefix>_<secret>`; `is_engine_token` recognizes
the prefix so the auth dependency can disambiguate JWT vs API key from
a single `Authorization` header. Source:
[`api_keys.py`](../../engine/api/routes/api_keys.py).

---

## System & client telemetry

| Method | Path | Auth | Notes |
|---|---|---|---|
| `GET` | `/api/v1/system/status` | user | [`system.py`](../../engine/api/routes/system.py). Engine version, uptime, server time, per-component health (`database`), and best-effort table counts (`users`, `portfolios`, `backtests`, `webhooks_active`, `api_keys_active`). A failed count is `-1`, never an error. |
| `POST` | `/api/v1/client/errors` | **public** | [`client_errors.py`](../../engine/api/routes/client_errors.py). Frontend `ErrorBoundary` ingest. Body `{message, stack?, component_stack?, url?, user_agent?, error_id?}`. **Sanitised server-side:** CRLF/ANSI escapes stripped, URL reduced to scheme+host+path (query strings often carry tokens), caller-supplied `error_id` must be a UUID. Returns `201 {error_id}`. Capped at 30 req/min/IP — see [api-reference.md](../api-reference.md#errors). |

---

## Backtest

Router prefix `/api/v1/backtest`, **legal-gated** at the include level.
All routes require `user`. Results are stored in a process-local dict
with a 1-hour TTL — **not persisted**; see
[known-limitations.md → backtest results not persisted](../known-limitations.md#backtest-results-not-persisted).

| Method | Path | Body → Response |
|---|---|---|
| `POST` | `/api/v1/backtest/run` | `{strategy_name, symbol, start_date, end_date, initial_capital?=100000, config?}` → `200 {status:"accepted", backtest_id}`. Kicks off a background task; poll `/results/{id}`. |
| `POST` | `/api/v1/backtest` | `202 Accepted`. Accepts the canonical fields *and* the k6 load-test payload (`strategy_id`/`start`/`end` aliases via `AliasChoices`). Otherwise identical to `/run`. |
| `GET` | `/api/v1/backtest/results/{backtest_id}` | `200` with `BacktestResultResponse` (status `completed`/`failed`), `202` while `running`, `404` if unknown, **`403` if the caller isn't the original submitter** (owner check on the cached tuple). The `metrics` block carries ~26 KPIs incl. rolling Sharpe/Sortino/vol/DD per window; see `MetricsSummary` in the source. |

Source: [`backtest.py`](../../engine/api/routes/backtest.py).

---

## Strategies & scoring

### Strategies

Router prefix `/api/v1/strategies`, **legal-gated** at the router level.
All routes require `user`. `{strategy_id}` is validated by the shared
[`SafeIdentifier`](../../engine/api/validators.py) alias — bad shape →
`422` before the handler runs.

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/v1/strategies/` | Lists installed strategies from `app.state.plugin_registry`. |
| `GET` | `/api/v1/strategies/{strategy_id}` | Manifest detail. `404` if unknown. |
| `POST` | `/api/v1/strategies/{strategy_id}/activate` | Body `{params:dict}`. Instantiates via the sandbox; `500` on activation failure (error string reflected). |
| `POST` | `/api/v1/strategies/{strategy_id}/deactivate` | Unloads. Always `200`. |
| `POST` | `/api/v1/strategies/{strategy_id}/reload` | Hot-reload from disk. `500` on failure. |
| `GET` | `/api/v1/strategies/{strategy_id}/health` | Runtime health of an active strategy. `404` if not loaded. |

Source: [`strategies.py`](../../engine/api/routes/strategies.py).

### Scoring

Router prefix `/api/v1/scoring`, **legal-gated** at the include level.
All routes require `user`. `{strategy_name}` uses `SafeIdentifier`.

| Method | Path | Body / Query → Response |
|---|---|---|
| `POST` | `/api/v1/scoring/{strategy_name}/run` | `{universe:string[], raw_data?:{symbol:{factor:value}}}` → `ScoringRunResponse {strategy_id, scores[], excluded_factors[], universe_size}`. `404` if the strategy isn't found; `400` if it isn't a scoring strategy (`is_scoring_strategy` check). Result is persisted to `scoring_snapshots`. |
| `GET` | `/api/v1/scoring/{strategy_name}/results` | Query `limit` (1-100), `offset`, `sort_by`, `sort_order=desc\|asc`. Returns persisted snapshots with their scores. |

Source: [`scoring.py`](../../engine/api/routes/scoring.py).

---

## Portfolio

Router prefix `/api/v1/portfolio`, **legal-gated** at the router level.
All routes require `user`.

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/v1/portfolio/` | Body `{name, description?, initial_capital?=100000}` → `201 PortfolioResponse`. |
| `GET` | `/api/v1/portfolio/` | Lists the caller's portfolios. |
| `GET` | `/api/v1/portfolio/{portfolio_id}` | `400` on non-UUID, `404` if not found, **`403` if not owned by the caller**. |
| `DELETE` | `/api/v1/portfolio/{portfolio_id}` | Hard delete. Same ownership/error semantics as GET. |

Source: [`portfolio.py`](../../engine/api/routes/portfolio.py).

---

## Webhooks

Router prefix `/api/v1/webhooks`. All routes require `user`; create
requires **scope:trade** for API-key callers.

| Method | Path | Auth | Notes |
|---|---|---|---|
| `POST` | `/api/v1/webhooks` | scope:trade | Body `{url, event_types?:[], custom_headers?:{}, template?="generic", max_retries?=3, portfolio_id?}`. Template must be one of `generic\|discord\|slack\|telegram` (`400` otherwise). `signing_secret` is generated server-side and echoed **only** in this create response. |
| `GET` | `/api/v1/webhooks` | user | Lists the caller's webhooks. `signing_secret` is never re-surfaced. |
| `PUT` | `/api/v1/webhooks/{webhook_id}` | user | Partial update. `404` if not owned. |
| `DELETE` | `/api/v1/webhooks/{webhook_id}` | user | `204`. |
| `POST` | `/api/v1/webhooks/{webhook_id}/test` | user | Fires a synthetic `test.event` delivery through `WebhookDispatcher` (real HTTP, 10s timeout) and returns the resulting `DeliveryResponse`. |
| `GET` | `/api/v1/webhooks/{webhook_id}/deliveries` | user | Query `limit` (clamped 1-200, default 50). Delivery history newest-first. |

Source: [`webhooks.py`](../../engine/api/routes/webhooks.py).

---

## Marketplace

Router prefix `/api/v1/marketplace`, **legal-gated** at the router
level. Browse/search/categories require `user`; install/uninstall
require **role:developer**.

| Method | Path | Auth | Status |
|---|---|---|---|
| `GET` | `/api/v1/marketplace/browse` | user | **Stub** — always returns `{strategies:[], total:0, ...}`. |
| `GET` | `/api/v1/marketplace/search` | user | **Real.** Query `q?`, `category?`, `tag?`, `sort` (`relevance\|downloads\|rating\|recent\|name`), `page`, `limit` (1-100). Empty `q` + `sort=relevance` transparently falls back to `downloads` (relevance is undefined without a query). Catalog-sourced numeric fields are `Optional` and serialize to JSON `null` when missing rather than a misleading `0`. |
| `GET` | `/api/v1/marketplace/categories` | user | **Real** — static taxonomy (`algorithmic`, `ml`, `llm`, `hybrid`, `income`, `macro`). |
| `POST` | `/api/v1/marketplace/install` | role:developer | **Stub** — returns `{status:"not_implemented", ...}`. |
| `DELETE` | `/api/v1/marketplace/uninstall/{strategy_id}` | role:developer | **Stub.** |
| `POST` | `/api/v1/marketplace/{strategy_id}/rate` | user | **Legacy stub** — validates `rating` is 1-5 and returns `{status:"not_implemented"}`. Use the ratings endpoints below for real behaviour. |
| `POST` | `/api/v1/marketplace/strategies/{strategy_id}/ratings` | user | **Real but in-memory.** Body `{stars:1-5, review?}` → `201 RatingResponse`. Upsert — one rating per `(strategy_id, user_id)`; resubmit updates in place. |
| `GET` | `/api/v1/marketplace/strategies/{strategy_id}/ratings` | user | **Real but in-memory.** Query `limit` (0-100), `offset`. Returns aggregate stats + a page of reviews (text-only, newest-updated first). |

> **Persistence caveat:** the `ratings` endpoints are backed by an
> in-memory `RatingsStore` — ratings vanish on process restart. See
> [known-limitations.md](../known-limitations.md#marketplace-ratings-not-persisted).
> Search eager-fallback and detail leakage were hardened in PR #1527.

Source: [`marketplace.py`](../../engine/api/routes/marketplace.py).

---

## Market data

Router prefix `/api/v1/market-data`, **legal-gated** at the include
level. Both routes require `user`.

| Method | Path | Query → Response |
|---|---|---|
| `GET` | `/api/v1/market-data/{symbol}/bars` | `interval` (default `1d`), `period` (default `1y`), `provider?` (pin a specific adapter), `asset_class?` (override inference). Returns `BarsResponse {symbol, interval, period, asset_class, provider, bars:[{timestamp,open,high,low,close,volume}]}`. |
| `GET` | `/api/v1/market-data/{symbol}/quote` | `provider?`, `asset_class?` → `QuoteResponse {symbol, asset_class, provider, price}`. |

**Symbol validation** (`_validate_symbol`): `fullmatch` against
`SYMBOL_PATTERN`, with explicit `..` rejection so path traversal can't
slip past the trailing `$` anchor. Bad shape → `400`.

**Asset-class inference** (`detect_asset_class`): conservative — equities
are the default long tail. Yahoo forex (`EURUSD=X`) is checked first;
crypto is checked before forex because the fiat/crypto quote sets
overlap (`BTC/USD` would otherwise misclassify). Override with the
`asset_class` query param.

**Error mapping** (registry path, no pinned provider):

| Cause | Status |
|---|---|
| `CapabilityNotSupportedError` — no adapter supports the op | `501` |
| `NoProviderAvailableError` — every candidate failed / none registered | `503` |
| `FatalProviderError` — bad symbol, permanent upstream error | `400` |
| `TransientProviderError` / `TimeoutError` (pinned provider only) | `503` |
| `ProviderError` (quote, pinned provider only) | `502` |
| Quote returns `None` for a valid symbol | `404` |

Bars where any OHLCV field is non-finite (`NaN`/`null`) are silently
dropped rather than serialised into invalid JSON.

Source: [`market_data.py`](../../engine/api/routes/market_data.py).

---

## Reference

Router prefix `/api/v1/reference`. **Not legal-gated** — the legal-docs
UI itself calls `/suggest` to render attributions, so it must work
pre-acceptance. Both routes are **public**.

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/v1/reference/exchanges` | Static ISO 10383 MIC list (Nasdaq, NYSE, Xetra, LSE, …, plus a synthetic Crypto entry). Fully cached — no DB, no network. Exercised by the k6 baseline. |
| `GET` | `/api/v1/reference/suggest` | Query `q` (required, ≤ `MAX_QUERY_LEN`), `limit` (1-50, default 10), `asset_class?`. Hits the local `SearchIndex` first; on no match, falls back to the Yahoo Finance search API (`query2.finance.yahoo.com/v1/finance/search`, 5s timeout). Yahoo results are filtered by `asset_class` if supplied. Empty `q` → `400`. |

Source: [`reference.py`](../../engine/api/routes/reference.py).

---

## Tax

Router prefix `/api/v1/tax`. **Not legal-gated** (operators may want
tax reports before accepting terms). Both routes require `user`.

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/v1/tax/report/{code}` | `{disposals:[{description, acquired, disposed, proceeds:str, cost:str}]}` → `{jurisdiction:"US"\|..., summary:{...}}`. `code` is a 2-letter jurisdiction slug, case-insensitive; supported set: `US`, `GB`, `DE`, `FR`. Unknown → `400`. Money is passed as **string** to preserve `Decimal` precision through JSON. |
| `POST` | `/api/v1/tax/report/{code}/csv` | Same dispatch, returns the summary as a 2-row CSV (`text/csv`, `attachment; filename="tax-report-<CODE>.csv"`) for spreadsheet/CPA workflows. |

Source: [`tax.py`](../../engine/api/routes/tax.py). The aggregation
itself lives in [`engine.core.tax.reports`](../../engine/core/tax/reports/).

---

## Privacy / DSR (GDPR & CCPA)

Router prefix `/api/v1/privacy`. **Not legal-gated.** All routes
require `user`.

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/v1/privacy/export` | Synchronous export of the caller's data. Returns `{request:DSRRequestSummary, data:{...}}`. Marks the DSR `completed`. |
| `POST` | `/api/v1/privacy/delete` | Body `{note?}` (≤ 4000 chars). Initiates a 30-day-grace deletion. `202`. `409` if a deletion is already pending. |
| `POST` | `/api/v1/privacy/delete/cancel` | Cancels a pending deletion. `404` if none pending. |
| `GET` | `/api/v1/privacy/delete/status` | `{pending:bool, sla_due_at?, request:null}` (the full request is intentionally not echoed here). |
| `GET` | `/api/v1/privacy/requests` | DSR history for the caller. |
| `GET` | `/api/v1/privacy/kinds` | **Public-ish allow-list** of DSR kinds for OpenAPI clients to validate against. |

Source: [`privacy.py`](../../engine/api/routes/privacy.py).

---

## Legal

Router has no prefix — routes carry their full paths. The
document-management surface is **public** for reads (so unauthenticated
clients can render docs); acceptance recording requires `user`.

### Document management (the "long" surface)

| Method | Path | Auth | Notes |
|---|---|---|---|
| `GET` | `/api/v1/legal/documents` | optional | Query `category?`. Returns summaries. Auth is *optional* — if a valid Bearer is present, the response is personalised with acceptance state. |
| `GET` | `/api/v1/legal/documents/{slug}` | public | Query `version?`. `{slug}` matches `^[a-z0-9-]+$`. Markdown content has front-matter stripped and `{{OPERATOR_NAME}}`/`{{EFFECTIVE_DATE}}`/… placeholders substituted from settings. `404` if unknown. |
| `POST` | `/api/v1/legal/accept` | user | Records acceptance of one or more documents (body shape: `AcceptRequest`). Persists IP + user-agent for audit. |
| `GET` | `/api/v1/legal/acceptances/me` | user | Query `document_slug?`. The caller's acceptance history. |
| `GET` | `/api/v1/legal/attributions` | public | Query `context?`. Open-source / data attributions. |

### Legal gate (the "short" surface)

A lean, append-only acceptance log keyed by user — distinct from the
document-management surface above. Persistence:
[`engine.legal.models.LegalAcceptance`](../../engine/legal/models.py)
(table `legal_gate_acceptances`).

| Method | Path | Auth | Notes |
|---|---|---|---|
| `POST` | `/api/legal/accept` | user | Body `{document_version}` (must equal `settings.legal_terms_version`, else `422` with `code:"LEGAL_VERSION_MISMATCH"`). The server-authoritative version is what's persisted — never the client string — so the audit trail can't be polluted by a crafted payload. Client IP resolved via CIDR-aware `resolve_client_ip` (honors `trusted_proxies`). |
| `GET` | `/api/legal/status` | user | `{accepted, current_version, accepted_version?, accepted_at?, needs_acceptance}`. `accepted` is `True` only when the user's most recent acceptance matches the current version. |

> **Note the path prefix difference:** document management lives under
> `/api/v1/legal/*`, the gate lives under `/api/legal/*` (no `v1`).
> Both are mounted by the same router instance; the discrepancy is
> intentional but worth flagging to clients.

Source: [`legal.py`](../../engine/api/routes/legal.py). See also
[ADR 0002](../adr/0002-auth-rbac.md) for the legal-gate design.
