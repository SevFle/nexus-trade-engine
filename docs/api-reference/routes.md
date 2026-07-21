# REST routes

Every HTTP endpoint on the engine, grouped by the router module that
owns it. All routes are wired through
[`engine/api/router.py`](../../engine/api/router.py). Conventions
(auth, RBAC, legal gate, errors, validation, middleware) live in
[`api-reference.md`](../api-reference.md); this page is the
per-endpoint catalog and only repeats what is non-obvious.

Routes mount under `/api/v1/*` **except** the cross-cutting probes
(`/health`, `/ready`, `/health/providers`, `/metrics`, `/api/v1/health`)
which sit outside the auth model by design. The router file applies the
**legal-acceptance gate** (`Depends(require_legal_acceptance)`) to
`backtest`, `scoring`, and `market-data` at `include_router` time; the
other gated routers (`portfolio`, `strategies`, `marketplace`) declare
the dependency on the `APIRouter(...)` itself. `auth`, `mfa`,
`api-keys`, `tasks`, `system`, `reference`, `tax`, `webhooks`,
`privacy`, `legal`, `client`, `health`, and `metrics` are **not**
gated, because they must be reachable before the user has accepted
anything (e.g. the legal-docs UI calls `/reference/suggest`).

Path-parameter identifiers in user-controlled positions
(`{strategy_id}` on `/strategies/*`, `{strategy_name}` on `/scoring/*`)
are validated by the shared `SafeIdentifier` alias
([`engine/api/validators.py`](../../engine/api/validators.py)); a
malformed value returns `422` *before* the handler runs.

<a id="auth"></a>
## Auth — `engine/api/routes/auth.py`

Mounted at `/api/v1/auth`. Tokens minted here are accepted by every
protected route (see [Authentication](../api-reference.md#authentication)).

| Method & path | Body | Auth | Status | Notes |
|---|---|---|---|---|
| `POST /register` | `{email, password, display_name?}` | none | `201` | Local provider only. Password ≥ 8 chars. `409` if email taken. |
| `POST /login` | `{email, password}` | none | `200` / `200` (MFA) | Hard-coded to `"local"` provider. If MFA is enabled, returns `{mfa_required:true, challenge_token}` instead of tokens — finish via [`/auth/mfa/verify`](#mfa). |
| `POST /refresh` | `{refresh_token}` | none | `200` | **Atomic rotation**: a single `UPDATE … WHERE revoked_at IS NULL RETURNING …` either rotates the token or detects reuse. Reuse of a revoked token revokes **every** session for the user (`auth.token_replay_detected`). |
| `POST /logout` | `{refresh_token}?` | bearer | `200` | Revokes the supplied token, or every active session for the user when the body is omitted. |
| `GET  /me` | — | bearer | `200` | Returns the principal's `id`, `email`, `display_name`, `role`, `auth_provider`, `is_active`. |
| `GET  /{provider}/authorize` | — | none | `200` | Builds an OAuth authorize URL + opaque `state`, set as an httponly cookie scoped to `/api/v1/auth`. `404` if the provider is not in the registry. |
| `GET  /{provider}/callback` | `?code=&state=` | cookie | `200` | Validates the `oauth_state_{provider}` cookie against `state`, exchanges `code` for a principal, mints tokens. `ldap` is **registered but has no route** — see [known-limitations](../known-limitations.md#ldap-has-no-route). |

<a id="mfa"></a>
## MFA — `engine/api/routes/mfa.py`

Mounted at `/api/v1/auth/mfa`. TOTP enrollment + verification (gh#126).
Secrets are Fernet-encrypted at rest with `NEXUS_MFA_ENCRYPTION_KEY`.

| Method & path | Body | Status | Notes |
|---|---|---|---|
| `POST /enroll` | — | `200` / `409` | Returns `secret` (base32) + `otpauth_uri`. `409` if MFA already enabled. |
| `POST /enroll/confirm` | `{secret, code}` | `200` / `400` | Verifies the first TOTP, persists the encrypted secret + bcrypt-hashed backup codes, returns the **plaintext backup codes once**. |
| `POST /verify` | `{challenge_token, code}` | `200` / `401` | Completes an MFA-challenged login. Backup codes are consumed in place; `verify_login_code` rewrites the stored list when one is used. |
| `POST /disable` | `{password, code}` | `200` / `401` | Requires the local password **and** a live TOTP / backup code. `400` for OAuth-only users (`hashed_password` is null). |
| `POST /backup-codes/regen` | `{code}` | `200` / `401` | Rotates backup codes after verifying a live TOTP / backup code. Returns the new plaintext set once. |

## API keys — `engine/api/routes/api_keys.py`

Mounted at `/api/v1/auth/api-keys`. Long-lived bearer credentials
(gh#94); the plaintext token is returned **exactly once** on `POST`.

| Method & path | Body | Auth | Status | Notes |
|---|---|---|---|---|
| `POST   ` | `{name, scopes[], expires_at?, env?}` | bearer | `201` | `scopes` ⊆ `{read, trade, admin}`; `env` matches `^[A-Za-z0-9_]+$`. Response carries `token` (`nxs_<prefix>_<secret>`); never logged or stored in plaintext. |
| `GET    ` | — | bearer | `200` | Lists the caller's keys (no plaintext — only `prefix` + metadata). |
| `DELETE /{key_id}` | — | bearer | `204` / `404` | Sets `revoked_at` (a tombstone, not a row delete, so the audit trail survives). |

## System — `engine/api/routes/system.py`

Mounted at `/api/v1/system`. Operator / CI surface (gh#94).

| Method & path | Auth | Response shape |
|---|---|---|
| `GET /status` | bearer | `{engine_version, uptime_seconds, server_time, components:[{name, healthy, detail?}], counts:{users, portfolios, backtests, webhooks_active, api_keys_active}}` |

`components` currently probes the database only (`SELECT now()`).
`counts` are best-effort: a per-table count failure yields `-1` rather
than a 500.

<a id="tasks"></a>
## Tasks — `engine/api/routes/tasks.py`

Mounted at `/api/v1/tasks`. TaskIQ broker liveness (gh#1306).

| Method & path | Auth | Notes |
|---|---|---|
| `GET /status` | **none** | Always `200`. `broker` is `"running" \| "stopped"` derived from the broker's real state — prefers taskiq's `is_started` flag and falls back to a bounded `PING` against the broker's shared `connection_pool`. The probe **never closes** the throwaway Redis client (it cannot sever the pool the app depends on), and the `PING` is wrapped in `asyncio.wait_for(..., 1.0)` so a hung broker cannot wedge the probe. |

Deliberately unauthenticated: load balancers / orchestrators hit it
during deploys, so the broker field must reflect reality rather than a
hardcoded constant.

## Privacy / DSR — `engine/api/routes/privacy.py`

Mounted at `/api/v1/privacy`. GDPR + CCPA surface (gh#157).

| Method & path | Body | Status | Notes |
|---|---|---|---|
| `POST /export` | — | `200` | Synchronous export of the caller's data. Records a `dsr_requests` row, collects user data, marks the request `completed`. |
| `POST /delete` | `{note?}` | `202` / `409` | Schedules deletion with a 30-day grace window (`sla_due_at`). `409` if a deletion is already pending. |
| `POST /delete/cancel` | — | `200` / `404` | Cancels during the grace window. |
| `GET  /delete/status` | — | `200` | `{pending, sla_due_at, request:null}` (no row returned to keep the response small). |
| `GET  /requests` | — | `200` | The caller's full DSR history. |
| `GET  /kinds` | — | `200` | `{kinds:[...]}` — the allow-list of DSR kinds for OpenAPI clients. |

## Backtest — `engine/api/routes/backtest.py`

Mounted at `/api/v1/backtest` (legal-gated).

| Method & path | Body | Status | Notes |
|---|---|---|---|
| `POST /run` | `BacktestRequest` | `200` | Legacy synchronous-ish entry; enqueues a `BackgroundTasks` job and returns the `backtest_id` immediately. |
| `POST ` (root) | `BacktestSubmitRequest` | `202` | The async entry point the k6 baseline exercises. Accepts both canonical (`strategy_name`/`start_date`/`end_date`) and load-test (`strategy_id`/`start`/`end`) field aliases via Pydantic `AliasChoices` — keeps the load script stable across schema renames. |
| `GET  /results/{backtest_id}` | — | `200` / `202` / `404` / `403` | Polls a background run. `202` while running, `200` with `BacktestResultResponse` on completion, `403` if the caller is not the submitter, `404` if the id is unknown or evicted. |

`BacktestRequest`:
```json
{"strategy_name":"mean_reversion","symbol":"AAPL",
 "start_date":"2020-01-01","end_date":"2024-01-01",
 "initial_capital":100000.0,"config":{}}
```

`BacktestResultResponse.metrics` carries the full cost-aware summary:
`total_return_pct`, `sharpe_ratio`, `sortino_ratio`, `max_drawdown_*`,
`calmar_ratio`, `win_rate`, `profit_factor`, `total_costs`,
`total_taxes`, `cost_drag_pct`, `turnover_ratio`, `exposure_pct`, plus
a list of `rolling_metrics` snapshots.

> **Storage caveat.** Results live in an **in-process dict** keyed by
> `backtest_id` with a 1-hour TTL (`_RESULTS_TTL_SECONDS`). The
> `backtest_results` table exists but the REST route does not yet write
> to it — see [known-limitations](../known-limitations.md).

## Client errors — `engine/api/routes/client_errors.py`

Mounted at `/api/v1/client`. Browser-side error ingest (gh#153).

| Method & path | Body | Auth | Status |
|---|---|---|---|
| `POST /errors` | `ClientErrorReport` | **none** | `201` |

The frontend `ErrorBoundary` posts here. CRLF + ANSI escapes are
stripped from every text field (defence against log forging / terminal
escapes), the caller-supplied `error_id` must be a UUID, and the `url`
field is reduced to scheme+host+path so accidentally-captured query
tokens never reach the audit trail. Rate-limited to 30 req/min
separately from the global limiter.

## Legal — `engine/api/routes/legal.py`

Two surfaces coexist on one router. The legal-document-management
slice (`/api/v1/legal/*`) and the legal-gate acceptance slice
(`/api/legal/*` — no `v1`).

| Method & path | Auth | Notes |
|---|---|---|
| `GET  /api/v1/legal/documents` | optional | Lists documents; per-user acceptance status included when a valid bearer is supplied. |
| `GET  /api/v1/legal/documents/{slug}` | none | Renders a document. `slug` matches `^[a-z0-9-]+$`. Markdown body is template-substituted (`{{OPERATOR_NAME}}`, `{{EFFECTIVE_DATE}}`, …) and the front matter is stripped. |
| `POST /api/v1/legal/accept` | bearer | Records a *list* of document acceptances (`{acceptances:[…]}`) with the request IP + user agent. |
| `GET  /api/v1/legal/acceptances/me` | bearer | The caller's acceptance history (optional `?document_slug=` filter). |
| `GET  /api/v1/legal/attributions` | none | Data-provider attribution list (used by the reference UI). |
| `POST /api/legal/accept` | bearer | Records acceptance of the **current** terms version. Submitted `document_version` must equal `settings.legal_terms_version`, else `422 LEGAL_VERSION_MISMATCH`. The server-authoritative current version is what's persisted — never the client string. |
| `GET  /api/legal/status` | bearer | `{accepted, current_version, accepted_version, accepted_at, needs_acceptance}`. |

## Portfolio — `engine/api/routes/portfolio.py`

Mounted at `/api/v1/portfolio` (legal-gated at the router level).

| Method & path | Body | Status | Notes |
|---|---|---|---|
| `POST   /` | `{name, description?, initial_capital?}` | `200` | `initial_capital ≥ 0`, default `100_000`. |
| `GET    /` | — | `200` | The caller's portfolios only. |
| `GET    /{portfolio_id}` | — | `200` / `400` / `404` / `403` | `400` on a non-UUID id; `403` if the portfolio belongs to another user. |
| `DELETE /{portfolio_id}` | — | `200` / `400` / `404` / `403` | Hard-deletes the row. (No soft-delete yet — see [data-model](../data-model.md).) |

<a id="strategies"></a>
## Strategies — `engine/api/routes/strategies.py`

Mounted at `/api/v1/strategies` (legal-gated at the router level).
`{strategy_id}` is a `SafeIdentifier` (validated up front).

| Method & path | Body | Status | Notes |
|---|---|---|---|
| `GET    /` | — | `200` | Lists installed strategies + status from `app.state.plugin_registry`. |
| `GET    /{strategy_id}` | — | `200` / `404` | Manifest details + computed `requires_network` / `requires_gpu`. |
| `POST   /{strategy_id}/activate` | `{params}` | `200` / `404` / `500` | Instantiates the strategy with the supplied params. |
| `POST   /{strategy_id}/deactivate` | — | `200` | Unloads the strategy. |
| `POST   /{strategy_id}/reload` | — | `200` / `500` | Hot-reload from disk. |
| `GET    /{strategy_id}/health` | — | `200` / `404` | Runtime health (currently a stub — sandbox metrics are not surfaced here yet). |

> **Wiring caveat.** `app.state.plugin_registry` is attached by the
> legacy `engine/main.py` entrypoint, not by `create_app()`. The
> canonical uvicorn entrypoint does not yet populate it — see
> [known-limitations](../known-limitations.md).

## Webhooks — `engine/api/routes/webhooks.py`

Mounted at `/api/v1/webhooks`. HMAC-signed outbound delivery (gh#80).

| Method & path | Body | Auth | Status | Notes |
|---|---|---|---|---|
| `POST   ` | `WebhookCreateRequest` | `trade` scope | `201` | `template ∈ {generic, discord, slack, telegram}` (validated server-side). Response echoes the `signing_secret` exactly once. |
| `GET    ` | — | bearer | `200` | Lists the caller's webhooks (no secret). |
| `PUT    /{webhook_id}` | `WebhookUpdateRequest` | bearer | `200` / `404` | Partial update. Template validated when supplied. |
| `DELETE /{webhook_id}` | — | bearer | `204` / `404` | Hard-deletes the config. |
| `POST   /{webhook_id}/test` | — | bearer | `200` | Fires a synthetic `test.event` through a real `WebhookDispatcher` and returns the resulting `DeliveryResponse`. |
| `GET    /{webhook_id}/deliveries` | `?limit=` | bearer | `200` | Delivery history (`limit` clamped to `[1, 200]`, default 50). |

<a id="marketplace"></a>
## Marketplace — `engine/api/routes/marketplace.py`

Mounted at `/api/v1/marketplace` (legal-gated at the router level).

| Method & path | Body / query | Auth | Status | Notes |
|---|---|---|---|---|
| `GET    /browse` | `?category=&search=&sort_by=&page=&per_page=` | bearer | `200` | **Stub** — returns an empty list. |
| `GET    /search` | `?q=&category=&tag=&sort=&page=&limit=` | bearer | `200` / `400` | Real catalog search. `sort ∈ ALLOWED_SORTS`; `relevance` falls back to `downloads` when `q` is empty (relevance is undefined without a query). |
| `GET    /categories` | — | bearer | `200` | Static category list. |
| `POST   /install` | `{strategy_id, version}` | `developer` role | `200` | **Stub** — returns `status:"not_implemented"`. |
| `DELETE /uninstall/{strategy_id}` | — | `developer` role | `200` | **Stub**. |
| `POST   /{strategy_id}/rate` | `?rating=&review=` | bearer | `200` / `400` | **Legacy stub** — returns `status:"not_implemented"`. |
| `POST   /strategies/{strategy_id}/ratings` | `{stars, review?}` | bearer | `201` / `400` | **Real** upsert (one rating per user × strategy). `stars ∈ [1,5]`. |
| `GET    /strategies/{strategy_id}/ratings` | `?limit=&offset=` | bearer | `200` / `400` | Aggregate stats + paged reviews. |

> **Persistence caveat.** Ratings are backed by `RatingsStore` which
> is **in-memory only** today — data is lost on restart. See
> [known-limitations](../known-limitations.md).

## Reference — `engine/api/routes/reference.py`

Mounted at `/api/v1/reference`. **Not legal-gated** (called before
acceptance).

| Method & path | Query | Auth | Notes |
|---|---|---|---|
| `GET /exchanges` | — | none | Static list of supported venues (fully cached, no DB / network). |
| `GET /suggest` | `?q=&limit=&asset_class=` | none | Typeahead. Hits the local `SearchIndex` first; falls back to Yahoo Finance search on miss. Empty `q` → `400`; `len(q) > MAX_QUERY_LEN` → `400`. |

<a id="tax"></a>
## Tax — `engine/api/routes/tax.py`

Mounted at `/api/v1/tax`. Jurisdiction-aware tax reports (gh#155).

| Method & path | Body | Auth | Status | Notes |
|---|---|---|---|---|
| `POST /report/{code}` | `{disposals:[{description, acquired, disposed, proceeds, cost}]}` | bearer | `200` / `400` | `code ∈ {US, GB, DE, FR}` (case-insensitive). Money is sent as **strings** to preserve `Decimal` precision. Returns `{jurisdiction, summary}`. |
| `POST /report/{code}/csv` | same | bearer | `200` / `400` | Same dispatch, response is a 2-row CSV attachment (`Content-Type: text/csv`). |

## Scoring — `engine/api/routes/scoring.py`

Mounted at `/api/v1/scoring` (legal-gated at include time). `{strategy_name}`
is a `SafeIdentifier`.

| Method & path | Body / query | Status | Notes |
|---|---|---|---|
| `POST /{strategy_name}/run` | `{universe:[...], raw_data:{symbol:{factor:value}}}` | `200` / `404` / `400` | Loads a scoring strategy from the registry, computes scores, persists a `scoring_snapshots` row. `404` if the strategy is missing; `400` if it isn't a scoring strategy. |
| `GET  /{strategy_name}/results` | `?limit=&offset=&sort_by=&sort_order=` | `200` | Paged scoring history. |

<a id="market-data"></a>
## Market data — `engine/api/routes/market_data.py`

Mounted at `/api/v1/market-data` (legal-gated at include time).

| Method & path | Query | Status | Notes |
|---|---|---|---|
| `GET /{symbol}/bars` | `?interval=&period=&provider?=&asset_class?=` | `200` / `400` / `401` / `451` / `500` | OHLCV bars. Symbol validated by `fullmatch` (trailing bytes cannot satisfy the pattern). Asset class inferred from symbol shape unless pinned. |
| `GET /{symbol}/quote` | `?provider?=&asset_class?=` | `200` / `400` / `404` / `502` / `503` | Latest price. |

Error mapping for provider failures (see
[`engine/data/providers/__init__.py`](../../engine/data/providers/__init__.py)):

| Exception | HTTP | Meaning |
|---|---|---|
| `CapabilityNotSupportedError` | `501` | No registered adapter serves this capability. |
| `NoProviderAvailableError` | `503` | Every candidate adapter failed or none registered. |
| `TransientProviderError` / `TimeoutError` | `503` | Upstream blip — retry. |
| `FatalProviderError` | `400` | Caller-side problem (bad symbol, etc.). |
| `ProviderError` (quote path) | `502` | Generic upstream error. |

NaN / non-finite floats in bar fields are silently dropped (a row
with a NaN open would otherwise produce invalid JSON).

<a id="observability"></a>
<a id="health"></a>
## Health & metrics

Mounted at the app root (not under `/api/v1`).

| Method & path | Auth | Notes |
|---|---|---|
| `GET /health` | none | Liveness. `{status:"ok"}`. |
| `GET /api/v1/health` | none | Alias so the k6 baseline (`GET /api/v1/health`) resolves. |
| `GET /health/providers` | none | Per-provider health from the data registry. `overall ∈ {ok, degraded, down}`. |
| `GET /ready` | none | Readiness. Real DB + Valkey probes (`SELECT 1`, `PING`). `degraded` if any check fails. |
| `GET /metrics` | none | Prometheus exposition. Empty placeholder body when the active backend is `NullBackend`; otherwise the rendered histogram/counter set. Deliberately scraped by Prometheus including `/metrics` itself so scrape latency is observable. |

## WebSocket

`/api/v1/ws` and `/api/v1/ws/events` are documented separately in
[`websocket.md`](websocket.md) — they have their own auth model, wire
protocol, channel taxonomy, and close-code space.
