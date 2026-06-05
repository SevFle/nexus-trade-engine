# Technical decisions

Two layers of decision documentation:

- **ADR (Architecture Decision Records)** — immutable, append-only.
  Used for cross-cutting decisions that future contributors need to
  understand. Lives in [`adr/`](adr/). Format: MADR.
- **Decision log (this file)** — short summary of decisions that
  don't justify a full ADR. Updated in place when a decision is
  reversed.

If a log entry turns out to be load-bearing, promote it to a numbered
ADR in the same PR.

## Decision log

| Date       | Decision                                                  | Source                                   |
|------------|-----------------------------------------------------------|------------------------------------------|
| 2026-04-15 | **Python 3.12 + uv + FastAPI + asyncpg + TimescaleDB**.    | [ADR-0001](adr/0001-scaffold-tech-choices.md) |
| 2026-04-17 | **Auth = pluggable providers, default local bcrypt + JWT.**| [ADR-0002](adr/0002-auth-rbac.md)        |
| 2026-04-20 | **Mobile = PWA on top of the React frontend, no native.**  | [ADR-0003](adr/0003-mobile-app-strategy.md) |
| 2026-04-22 | **TaskIQ over Celery** for async work. Same Redis protocol, native async, smaller surface. FastAPI integration via `taskiq-fastapi`. | `pyproject.toml` lock |
| 2026-04-22 | **Valkey over Redis** for the cache/broker. Drop-in protocol, governance transitioned away from Redis's SSPL. | `pyproject.toml` lock |
| 2026-04-25 | **Polars over pandas** for backtest internals. Arrow-columnar, lazy execution, no GIL releases for vectorised ops. (Pandas still accepted as input shape from data providers — most adapters return pandas DataFrames today; the conversion is one call.) | `engine/core/backtest_runner.py` |
| 2026-04-28 | **Cost model is a first-class input to `IStrategy.evaluate`**, not an after-the-fact deduction. Strategies can (and should) factor commissions, spread, slippage, taxes, and FX into their signals. | `sdk/nexus_sdk/strategy.py:IStrategy.evaluate` |
| 2026-05-02 | **Single-replica deployment is the supported topology** for v0.x. Multi-replica needs Redis pub/sub fan-out for WebSocket broadcasts and sticky sessions for the in-process backtest result dict. | `engine/api/websocket/manager.py` docstring; [`limitations.md`](limitations.md) |
| 2026-05-04 | **API key format `nxs_<env>_<random>`**. The `nxs_` prefix dispatches at the auth boundary without a separate header; the env label is operator-chosen for human sorting. | `engine/api/auth/api_keys.py` |
| 2026-05-05 | **Legal-document acceptance is immutable**. Triggers installed in migration `006` reject UPDATE/DELETE. Audit trail must be replayable years later. | `engine/db/migrations/versions/006_legal_acceptance_immutable.py` |
| 2026-05-09 | **DSR SLA default = 30 days** to match GDPR Art. 12. Operators in non-GDPR jurisdictions can shorten by writing to `dsr_requests.sla_due_at` directly. | `engine/privacy/__init__.py` |
| 2026-05-10 | **Webhook dispatcher retries 5xx and network errors only**. 4xx is terminal — the receiver is telling us our payload is wrong, retrying won't fix it. | `engine/events/webhook_dispatcher.py` |
| 2026-05-12 | **No multi-tenancy.** One deployment = one operator = one database. Multi-tenant SaaS would require row-level security and per-tenant secrets management, both out of scope. | `architecture/overview.md` non-goals |
| 2026-05-14 | **Federated role mapping never implicitly promotes.** Unrecognized IdP roles are dropped and logged, not promoted to a sane default. Defense-in-depth against a misconfigured / compromised provider. | `engine/api/auth/base.py:map_roles`; SEV-741 |
| 2026-05-16 | **MFA TOTP secrets encrypted at rest with Fernet.** Key rotation is operator-managed via `NEXUS_MFA_ENCRYPTION_KEY`; there is no in-band rotation flow. | `engine/api/auth/mfa_service.py` |
| 2026-05-20 | **Symbol validation uses `re.fullmatch`, not `re.match`.** Prevents trailing newlines from satisfying a pattern with a `$` anchor and slipping an unsanitized value into logs. | `engine/api/routes/market_data.py:_validate_symbol` |
| 2026-05-22 | **WebSocket auth message required within 10s of accept.** `AUTH_TIMEOUT_SECONDS`. JWT-in-query is intentionally not supported (proxy-log leak vector). | `engine/api/routes/websocket.py` |
| 2026-05-23 | **Operator-configurable platform fee and operator identity** are surfaced via Markdown substitution variables in legal docs, not in the API contract. Single source of truth = `engine/config.py`. | `engine/api/routes/legal.py:_apply_substitutions` |
| 2026-05-30 | **Backtest results live in an in-process dict with 1h TTL today**. Persistent path via TaskIQ worker exists but is not the primary path yet. | `engine/api/routes/backtest.py:_backtest_results` |

## Decision log entries that should become ADRs

These are decisions that have stuck for >1 release but lack the formal
*why* record. Promote when you have an hour.

1. **TaskIQ over Celery.** Reasonable but undocumented.
2. **Cost model as evaluate() input.** This shapes the strategy SDK;
   a future SDK redesign should reference the decision.
3. **Single-replica v0.x.** Constraints upgrade path to multi-replica
   pub/sub.
4. **No multi-tenancy.** Fundamental scoping decision.

## When this file changes

- Add a row when a decision is made that's not big enough for an ADR
  but big enough that "why did we do this?" will come up in code
  review later.
- Update a row in place when a decision is reversed. Note the
  reversal in the same row (e.g. "Reversed 2026-06-01 — see
  ADR-0007").
- Promote a row to an ADR when it accumulates enough context. Delete
  the row and add a one-line pointer to the new ADR.
