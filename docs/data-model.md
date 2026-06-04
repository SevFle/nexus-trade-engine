# Data model

This page documents every persistent entity in the engine, the
relationships between them, and the constraints that matter at the
application layer. The schema source of truth is
[`engine/db/models.py`](../engine/db/models.py); the migration chain
is in [`engine/db/migrations/versions/`](../engine/db/migrations/versions/).
See also [architecture/database.md](architecture/database.md) for
migration policy and async-session conventions.

## Entity-relationship diagram

```mermaid
erDiagram
    User ||--o{ Portfolio          : owns
    User ||--o{ RefreshToken       : has
    User ||--o{ WebhookConfig      : owns
    User ||--o{ ApiKey             : owns
    User ||--o{ LegalAcceptance    : signs
    User ||--o{ DSRequest          : requests

    Portfolio ||--o{ Position            : has
    Portfolio ||--o{ Order               : has
    Portfolio ||--o{ TaxLotRecord        : has
    Portfolio ||--o{ InstalledStrategy   : installs
    Portfolio ||--o{ BacktestResult      : runs
    Portfolio ||--o{ WebhookConfig       : targets

    WebhookConfig ||--o{ WebhookDelivery : dispatches
    LegalDocument ||--o{ LegalAcceptance : requires

    User {
        uuid    id              PK
        string  email           UK
        string  hashed_password "nullable for OAuth-only users"
        string  display_name
        bool    is_active
        string  role            "user | developer | admin"
        string  auth_provider   "local | google | github | oidc | ldap"
        string  external_id     "nullable; provider's user id"
        bool    mfa_enabled
        text    mfa_secret_encrypted "Fernet; nullable"
        jsonb   mfa_backup_codes "nullable; hashed"
    }

    Portfolio {
        uuid      id              PK
        uuid      user_id         FK
        string    name
        text      description
        decimal   initial_capital "Numeric(18,4)"
    }

    Position {
        uuid    id              PK
        uuid    portfolio_id    FK
        string  symbol
        decimal quantity        "Numeric(18,8)"
        decimal avg_entry_price "Numeric(18,8)"
        decimal current_price   "Numeric(18,8)"
    }

    Order {
        uuid      id           PK
        uuid      portfolio_id FK
        string    symbol
        string    side         "buy | sell"
        string    order_type   "market | limit | …"
        decimal   quantity
        decimal   price        "nullable for market orders"
        string    status       "pending | filled | rejected | failed"
        datetime  filled_at    "nullable"
    }

    TaxLotRecord {
        uuid     id                    PK
        string   lot_id                UK
        uuid     portfolio_id          FK
        string   symbol
        decimal  quantity
        decimal  remaining_quantity
        decimal  purchase_price
        datetime purchase_date
        decimal  cost_basis_adjustment "wash-sale adjustments"
        string   status                "open | partially_consumed | closed"
    }

    InstalledStrategy {
        uuid      id            PK
        uuid      portfolio_id  FK
        string    strategy_name
        jsonb     config
        bool      is_active
    }

    BacktestResult {
        uuid      id              PK
        uuid      portfolio_id    FK "nullable: ad-hoc backtests"
        string    strategy_name
        datetime  start_date
        datetime  end_date
        jsonb     metrics
        float     composite_score "nullable"
        jsonb     score_breakdown "nullable"
    }

    ScoringSnapshot {
        uuid    id           PK
        string  strategy_id  IX
        int     universe_size
        jsonb   excluded_factors
        jsonb   results
    }

    OHLCVBar {
        uuid     id          PK
        string   symbol
        datetime timestamp
        decimal  open high low close
        decimal  volume      "Numeric(24,4)"
    }

    LegalDocument {
        uuid    id                  PK
        string  slug                UK
        string  title
        string  current_version
        date    effective_date
        bool    requires_acceptance
        string  category
        int     display_order
        string  file_path
    }

    LegalAcceptance {
        uuid      id              PK
        uuid      user_id         FK "RESTRICT + DEFERRABLE"
        string    document_slug
        string    document_version
        datetime  accepted_at
        string    ip_address
        string    user_agent
        string    context         "onboarding | explicit"
        datetime  revoked_at      "nullable"
    }

    WebhookConfig {
        uuid    id            PK
        uuid    user_id       FK
        uuid    portfolio_id  FK "nullable: user-wide"
        string  url
        jsonb   event_types
        string  signing_secret
        jsonb   custom_headers
        string  template      "generic | discord | slack | telegram"
        int     max_retries
        bool    is_active
    }

    WebhookDelivery {
        uuid     id              PK
        uuid     webhook_id      FK
        string   event_type
        jsonb    payload
        string   status          "pending | delivered | failed | retrying"
        int      response_status "nullable"
        int      response_ms     "nullable"
        int      attempts
        text     error           "nullable"
        datetime delivered_at    "nullable"
    }

    ApiKey {
        uuid      id          PK
        uuid      user_id     FK
        string    name
        string    prefix      UK "e.g. nxs_abcd"
        string    key_hash    "bcrypt"
        jsonb     scopes      "['read','trade','admin']"
        datetime  last_used_at
        datetime  expires_at
        datetime  revoked_at
    }

    RefreshToken {
        uuid      id          PK
        uuid      user_id     FK "CASCADE"
        string    token_hash  UK "sha256"
        datetime  expires_at
        datetime  revoked_at  "set on rotation/logout"
        string    user_agent
        string    ip_address
    }

    DSRequest {
        uuid     id           PK
        uuid     user_id      FK "CASCADE"
        string   kind         "export | delete | rectify | restrict | object"
        string   status       "pending | completed | cancelled"
        text     note
        jsonb    details
        datetime sla_due_at    "GDPR Art. 12: 1 month"
        datetime completed_at
        datetime cancelled_at
    }

    DataProviderAttribution {
        uuid    id                PK
        string  provider_slug     UK
        string  provider_name
        text    attribution_text
        string  attribution_url
        string  logo_path
        jsonb   display_contexts
        bool    is_active
    }
```

## Conventions

- **Primary keys.** UUIDs everywhere. Legacy migrations are
  bigserial-free; new tables must use UUIDs.
- **Timestamps.** `created_at` is non-null with a Python-side default
  of `datetime.now(tz=UTC)`. `updated_at` is set both as a default
  and via SQLAlchemy's `onupdate` so server-side writes also bump it.
  All timestamp columns are `TIMESTAMPTZ`.
- **Money.** Stored as `Numeric(18,8)` — 10 digits before the
  decimal, 8 after. Volume goes wider: `Numeric(24,4)`. Never use
  `REAL` / `DOUBLE PRECISION` for money.
- **JSON.** Always `JSONB`, never `JSON` — `JSONB` deduplicates keys,
  supports indexing, and has consistent equality semantics. Add a
  `GIN` index if you query by key.
- **Foreign keys.** Default `ON DELETE CASCADE` for owned data
  (positions, orders, webhooks). Audit rows use `RESTRICT` so a
  misconfigured delete can't lose history. `legal_acceptances`
  additionally marks the FK `DEFERRABLE INITIALLY DEFERRED` so the
  acceptance row can be inserted in the same transaction that
  creates the user.

## Index strategy

The hot paths and the indexes that serve them:

| Hot path                                  | Index                                              |
|-------------------------------------------|----------------------------------------------------|
| Login by email                            | `users.email` UNIQUE                               |
| Per-provider identity lookup              | `uq_user_provider_external (auth_provider, external_id)` |
| List user's portfolios                    | `portfolios.user_id`                               |
| Position lookup by portfolio + symbol     | `uq_position_portfolio_symbol` UNIQUE              |
| Tax-lot open-lots query                   | `ix_tax_lot_portfolio_symbol (portfolio_id, symbol)` |
| OHLCV latest-bar query                    | `ix_ohlcv_symbol_timestamp (symbol, timestamp)` + UNIQUE |
| Refresh-token rotation                    | `refresh_tokens.token_hash` UNIQUE                 |
| API-key auth lookup by prefix             | `api_keys.prefix` UNIQUE                           |
| User's active API keys                    | `ix_api_keys_user_active (user_id, revoked_at)`    |
| DSR queue (per-user, per-kind, status)    | `ix_dsr_requests_user_kind_status`                 |
| Scoring snapshot time-series              | `ix_scoring_snapshot_strategy_time`                |
| Webhook delivery retry scan               | `webhook_deliveries.status` + `created_at`         |
| Legal acceptance history per user         | `ix_acceptance_user_doc_ver`                       |

When adding a hot path that doesn't fit one of these, add an index
in the same migration that introduces the query. Coverage of the
explain plan is part of the migration PR checklist.

## Enum representations

Postgres enums are stored as `TEXT` plus a Python `Enum`:

| Concept        | Column                | Values                                              |
|----------------|-----------------------|------------------------------------------------------|
| User role      | `users.role`          | `user`, `developer`, `admin`                        |
| Auth provider  | `users.auth_provider` | `local`, `google`, `github`, `oidc`, `ldap`         |
| Order side     | `orders.side`         | `buy`, `sell`                                       |
| Order status   | `orders.status`       | `pending`, `filled`, `rejected`, `failed`           |
| Tax lot status | `tax_lot_records.status` | `open`, `partially_consumed`, `closed`           |
| Webhook template | `webhook_configs.template` | `generic`, `discord`, `slack`, `telegram`     |
| DSR kind       | `dsr_requests.kind`   | `export`, `delete`, `rectify`, `restrict`, `object` |

## TimescaleDB hypertables

Two tables are converted to hypertables (see migration chain):

- `ohlcv_bars` — chunk interval `1 day`. Retention policy lives in
  the operator's playbook, not in the schema.
- Account equity history (planned — see
  [limitations.md](limitations.md)).

Operators can run on vanilla Postgres if they accept the storage
cost. The `enable_extension timescaledb` is wrapped in a
`CREATE EXTENSION IF NOT EXISTS`, so it's safe on plain PG.

## Soft delete vs hard delete

| Table            | Strategy        | Notes |
|------------------|-----------------|-------|
| `users`          | Soft (`is_active=false`) | FK references in audit tables block hard delete. |
| `portfolios`     | Soft (in current impl) | `DELETE` route marks deleted; the row remains. |
| `webhook_configs`| Hard            | `WebhookDelivery` rows are retained for audit. |
| `api_keys`       | Soft (`revoked_at`) | Allows audit of "which key was used". |
| `refresh_tokens` | Hard on logout, soft on revoke | The `revoked_at` column lets us distinguish. |
| `dsr_requests`   | Soft delete via `cancelled_at` | GDPR Art. 12 audit trail. |

## Privacy-relevant columns

Under GDPR / CCPA these columns are personal data and must be
included in any data export / purged on deletion. The DSR pipeline
in [`engine/privacy/`](../engine/privacy/) is the canonical
implementation; this table exists so reviewers can spot drift.

| Column | Table |
|---|---|
| `email`, `display_name`, `external_id`, `hashed_password`, `mfa_secret_encrypted`, `mfa_backup_codes` | `users` |
| `user_agent`, `ip_address` | `legal_acceptances`, `refresh_tokens` |
| `note`, `details` | `dsr_requests` |
| `payload` | `webhook_deliveries` — *may* contain PII depending on event type; treat as personal |

The deletion job hashes / nulls PII but keeps FK-referenced audit
rows so financial reporting stays consistent. See
[`engine/privacy/deletion.py`](../engine/privacy/deletion.py).
