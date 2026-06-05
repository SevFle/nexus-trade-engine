# Data model

The full relational schema lives in
[`engine/db/models.py`](../../engine/db/models.py). Migrations live
in [`engine/db/migrations/versions/`](../../engine/db/migrations/versions/)
(Alembic, async engine). This document is the engineer's view —
entities, relationships, the constraints that matter for operations,
and the why behind the design.

## Conventions

- **Primary keys.** All PKs are `UUID` (`uuid.uuid4` default), stored
  as native Postgres `uuid` type.
- **Timestamps.** `DateTime(timezone=true)` everywhere; default
  `_utcnow` (`engine/db/models.py:13`). Application code is
  tz-aware; we never store naive datetimes.
- **Money.** `Numeric(18, 8)` for prices and quantities (crypto needs
  the 8 decimals). Money values cross the API as JSON **strings** to
  preserve `Decimal` precision; Pydantic refuses numeric coercion in
  `DisposalRequest` (`engine/api/routes/tax.py:67`).
- **Soft deletes.** Generally not used. `Portfolio` and `Order` rows
  are cascade-deleted. The two exceptions are `LegalAcceptance`
  (`ondelete=RESTRICT`, deferred — audit rows outlive the user) and
  `ApiKey.revoked_at` (tombstone column for revocation without
  losing audit history).
- **Foreign keys.** `ON DELETE CASCADE` everywhere unless noted.

## Entity relationship diagram

```mermaid
erDiagram
  users ||--o{ portfolios : owns
  users ||--o{ refresh_tokens : has
  users ||--o{ api_keys : owns
  users ||--o{ webhook_configs : owns
  users ||--o{ legal_acceptances : recorded
  users ||--o{ dsr_requests : files
  portfolios ||--o{ positions : has
  portfolios ||--o{ orders : places
  portfolios ||--o{ tax_lot_records : tracks
  portfolios ||--o{ installed_strategies : runs
  portfolios ||--o{ backtest_results : produces
  portfolios ||--o{ webhook_configs : scoped_to
  webhook_configs ||--o{ webhook_deliveries : produces
  legal_documents ||--o{ legal_acceptances : accepted_in
}
```

## Entities

### `users`

The identity record. One row per human or service principal.

| Column                  | Type                       | Notes |
|-------------------------|----------------------------|-------|
| `id`                    | `UUID` PK                  | |
| `email`                 | `VARCHAR(255)`, unique     | Case-insensitive lookup is on the caller. |
| `hashed_password`       | `VARCHAR(255)`, nullable   | `NULL` for OAuth-only users. bcrypt cost 12. |
| `display_name`          | `VARCHAR(100)`             | |
| `is_active`             | `BOOL`                     | Soft disable; false → 401 on every auth. |
| `role`                  | `VARCHAR(20)`              | One of: `viewer`, `user`, `retail_trader`, `quant_dev`, `developer`, `portfolio_manager`, `admin`. Hierarchy enforced in code, not DB. |
| `auth_provider`         | `VARCHAR(20)`              | `local`, `google`, `github`, `oidc`, `ldap`. |
| `external_id`           | `VARCHAR(255)`, nullable   | IdP-side identifier. |
| `mfa_enabled`           | `BOOL`                     | |
| `mfa_secret_encrypted`  | `TEXT`, nullable           | Fernet-encrypted TOTP secret. |
| `mfa_backup_codes`      | `JSONB`, nullable          | `{hash: count_remaining}`. |
| `created_at`, `updated_at` | `TIMESTAMPTZ`           | |

**Constraints.** `UNIQUE (auth_provider, external_id)` — partial index
that allows OAuth users to share an email across providers but not
collide within one.

### `portfolios`

A user's trading account. All child entities (positions, orders, tax
lots, backtests) cascade from here.

| Column             | Type              | Notes |
|--------------------|-------------------|-------|
| `id`               | `UUID` PK         | |
| `user_id`          | `UUID` FK → users | `ON DELETE CASCADE`. |
| `name`             | `VARCHAR(200)`    | |
| `description`      | `TEXT`            | |
| `initial_capital`  | `NUMERIC(18, 4)`  | Default `100000.0`. |
| `created_at`       | `TIMESTAMPTZ`     | |

### `positions`

Current holdings. One row per `(portfolio_id, symbol)` pair.

| Column           | Type              | Notes |
|------------------|-------------------|-------|
| `id`             | `UUID` PK         | |
| `portfolio_id`   | `UUID` FK → portfolios | Cascade. |
| `symbol`         | `VARCHAR(20)`     | |
| `quantity`       | `NUMERIC(18, 8)`  | Negative = short. |
| `avg_entry_price`| `NUMERIC(18, 8)`  | |
| `current_price`  | `NUMERIC(18, 8)`  | |
| `updated_at`     | `TIMESTAMPTZ`     | |

**Constraints.** `UNIQUE (portfolio_id, symbol)` — enforced at the DB
because the order manager upserts into this table.

### `orders`

Order ledger. Status transitions: `pending → validated → submitted →
filled | rejected | failed`.

| Column         | Type                  | Notes |
|----------------|-----------------------|-------|
| `id`           | `UUID` PK             | |
| `portfolio_id` | `UUID` FK → portfolios | Cascade. |
| `symbol`       | `VARCHAR(20)`         | |
| `side`         | `VARCHAR(10)`         | `buy` / `sell`. |
| `order_type`   | `VARCHAR(20)`         | `market` / `limit` / `stop`. |
| `quantity`     | `NUMERIC`             | |
| `price`        | `NUMERIC`, nullable   | Required for limit / stop. |
| `status`       | `VARCHAR(20)`         | Default `pending`. |
| `filled_at`    | `TIMESTAMPTZ`, nullable | |
| `created_at`   | `TIMESTAMPTZ`         | |

### `installed_strategies`

Many-to-many between portfolios and strategies.

| Column           | Type              | Notes |
|------------------|-------------------|-------|
| `id`             | `UUID` PK         | |
| `portfolio_id`   | `UUID` FK → portfolios | Cascade. |
| `strategy_name`  | `VARCHAR(100)`    | Matches `strategy.manifest.yaml` id. |
| `config`         | `JSONB`           | Strategy-specific config blob. |
| `is_active`      | `BOOL`            | |
| `installed_at`   | `TIMESTAMPTZ`     | |

### `tax_lot_records`

Per-lot cost basis. The tax engine writes here on every fill; the
report generator reads from here at year-end.

| Column                   | Type                  | Notes |
|--------------------------|-----------------------|-------|
| `id`                     | `UUID` PK             | |
| `lot_id`                 | `VARCHAR(36)`, unique | Stable external id. |
| `portfolio_id`           | `UUID` FK → portfolios | Cascade. |
| `symbol`                 | `VARCHAR(20)`         | |
| `quantity`               | `NUMERIC(18, 8)`      | Original quantity. |
| `remaining_quantity`     | `NUMERIC(18, 8)`      | Decremented on each disposal. |
| `purchase_price`         | `NUMERIC(18, 8)`      | |
| `purchase_date`          | `TIMESTAMPTZ`         | |
| `cost_basis_adjustment`  | `NUMERIC(18, 8)`      | Wash sale adjustments applied here. |
| `status`                 | `VARCHAR(30)`         | `open`, `partially_consumed`, `closed`. |
| `created_at`, `updated_at` | `TIMESTAMPTZ`       | |

**Indexes.** `(portfolio_id, symbol)` — common query pattern for
"show me lots for AAPL in this portfolio."

### `backtest_results`

Outcomes of historical runs.

| Column             | Type                          | Notes |
|--------------------|-------------------------------|-------|
| `id`               | `UUID` PK                     | |
| `portfolio_id`     | `UUID` FK → portfolios, nullable | Nullable so GDPR export of an orphaned user can keep backtests. |
| `strategy_name`    | `VARCHAR(100)`                | |
| `start_date`       | `TIMESTAMPTZ`                 | |
| `end_date`         | `TIMESTAMPTZ`                 | |
| `metrics`          | `JSONB`                       | Full metrics blob — sharpe, drawdown, etc. |
| `composite_score`  | `FLOAT`, nullable             | Strategy evaluator output. |
| `score_breakdown`  | `JSONB`, nullable             | Per-factor score detail. |
| `created_at`       | `TIMESTAMPTZ`                 | |

### `users` continued — auth tokens

#### `refresh_tokens`

Opaque refresh tokens. Hashed at rest.

| Column         | Type              | Notes |
|----------------|-------------------|-------|
| `id`           | `UUID` PK         | |
| `user_id`      | `UUID` FK → users | Cascade. |
| `token_hash`   | `VARCHAR(64)`, unique | SHA-256 hex of the raw token. |
| `expires_at`   | `TIMESTAMPTZ`     | |
| `revoked_at`   | `TIMESTAMPTZ`, nullable | Non-null = revoked. |
| `created_at`   | `TIMESTAMPTZ`     | |
| `user_agent`   | `VARCHAR(512)`, nullable | Audit trail. |
| `ip_address`   | `VARCHAR(45)`, nullable | Audit trail. |

**Replay detection.** The refresh handler does an atomic
`UPDATE … WHERE revoked_at IS NULL RETURNING …`. If the row was
already revoked, the handler treats it as replay and revokes every
active session for the user (`engine/api/routes/auth.py:202`).

#### `api_keys`

| Column             | Type                       | Notes |
|--------------------|----------------------------|-------|
| `id`               | `UUID` PK                  | |
| `user_id`          | `UUID` FK → users          | Cascade. |
| `name`             | `VARCHAR(255)`             | |
| `prefix`           | `VARCHAR(32)`, unique      | DB lookup key. Visible in UI. |
| `key_hash`         | `VARCHAR(255)`             | bcrypt hash of the secret. |
| `scopes`           | `JSONB`                    | `[\"read\",\"trade\"]`. |
| `last_used_at`     | `TIMESTAMPTZ`, nullable    | Updated on every authenticated call. |
| `expires_at`       | `TIMESTAMPTZ`, nullable    | |
| `revoked_at`       | `TIMESTAMPTZ`, nullable    | |
| `created_at`, `updated_at` | `TIMESTAMPTZ`       | |

**Index.** `(user_id, revoked_at)` — list "active keys for user" query.

The token format is `nxs_<prefix>_<secret>`; `is_engine_token` is the
single point of recognition in `engine/api/auth/api_keys.py`.

### Legal / compliance

#### `legal_documents`

Synced from `legal/*.md` on app startup.

| Column              | Type                | Notes |
|---------------------|---------------------|-------|
| `id`                | `UUID` PK           | |
| `slug`              | `VARCHAR(50)`, unique | e.g. `terms-of-service`. |
| `title`             | `VARCHAR(200)`      | |
| `current_version`   | `VARCHAR(20)`       | Semantic versioning. |
| `effective_date`    | `DATE`              | |
| `requires_acceptance` | `BOOL`            | |
| `category`          | `VARCHAR(30)`       | e.g. `terms`, `privacy`, `eula`. |
| `display_order`     | `INT`               | UI ordering. |
| `file_path`         | `VARCHAR(255)`      | Path within `legal/`. |
| `created_at`, `updated_at` | `TIMESTAMPTZ` | |

#### `legal_acceptances`

Audit row. Immutable after insert (no `UPDATE` path).

| Column             | Type              | Notes |
|--------------------|-------------------|-------|
| `id`               | `UUID` PK         | |
| `user_id`          | `UUID` FK → users | **`ON DELETE RESTRICT`, `DEFERRABLE INITIALLY DEFERRED`** — the row must survive the user. |
| `document_slug`    | `VARCHAR(50)`     | |
| `document_version` | `VARCHAR(20)`     | |
| `accepted_at`      | `TIMESTAMPTZ`     | |
| `ip_address`       | `VARCHAR(45)`     | |
| `user_agent`       | `VARCHAR(500)`    | |
| `context`          | `VARCHAR(50)`     | `onboarding`, `upgrade`, `login`. |
| `revoked_at`       | `TIMESTAMPTZ`, nullable | Only set if the document itself is retracted. |

**Indexes.** `(user_id, document_slug)`, `(user_id, document_slug,
document_version)`, `(accepted_at)` — supports "list mine" and
"activity in window" queries.

### Privacy / DSR

#### `dsr_requests`

GDPR / CCPA request audit.

| Column         | Type              | Notes |
|----------------|-------------------|-------|
| `id`           | `UUID` PK         | |
| `user_id`      | `UUID` FK → users | Cascade. |
| `kind`         | `VARCHAR(32)`     | `export`, `delete`, `rectify`, `restrict`, `object`. |
| `status`       | `VARCHAR(32)`     | `pending`, `completed`, `cancelled`. |
| `note`         | `TEXT`, nullable  | User-supplied context. |
| `details`      | `JSONB`           | Operator notes, export URLs, etc. |
| `sla_due_at`   | `TIMESTAMPTZ`     | GDPR Art. 12 — one month. |
| `completed_at` | `TIMESTAMPTZ`, nullable | |
| `cancelled_at` | `TIMESTAMPTZ`, nullable | |
| `created_at`, `updated_at` | `TIMESTAMPTZ` | |

**Index.** `(user_id, kind, status)` — for the operator dashboard's
"pending requests" view.

### Webhooks

#### `webhook_configs`

User-owned outbound integrations.

| Column             | Type              | Notes |
|--------------------|-------------------|-------|
| `id`               | `UUID` PK         | |
| `user_id`          | `UUID` FK → users | Cascade. |
| `portfolio_id`     | `UUID` FK → portfolios, nullable | Optional scope. |
| `url`              | `VARCHAR(2048)`   | |
| `event_types`      | `JSONB`           | `["order.filled", "portfolio.updated", ...]`. |
| `signing_secret`   | `VARCHAR(128)`    | Random 32-byte URL-safe; HMAC-SHA256 signing. |
| `custom_headers`   | `JSONB`           | |
| `template`         | `VARCHAR(20)`     | `generic`, `discord`, `slack`, `telegram`. |
| `max_retries`      | `INT`             | Default 3. |
| `is_active`        | `BOOL`            | |
| `created_at`, `updated_at` | `TIMESTAMPTZ` | |

#### `webhook_deliveries`

Per-attempt delivery ledger. The dispatcher writes one row per
attempt; retry attempts update the same row until terminal.

| Column           | Type                | Notes |
|------------------|---------------------|-------|
| `id`             | `UUID` PK           | |
| `webhook_id`     | `UUID` FK → webhook_configs | Cascade. |
| `event_type`     | `VARCHAR(64)`       | |
| `payload`        | `JSONB`             | |
| `status`         | `VARCHAR(20)`       | `pending`, `delivered`, `failed`. |
| `response_status`| `INT`, nullable     | |
| `response_ms`    | `INT`, nullable     | |
| `attempts`       | `INT`               | |
| `error`          | `TEXT`, nullable    | |
| `created_at`     | `TIMESTAMPTZ`       | Indexed. |
| `delivered_at`   | `TIMESTAMPTZ`, nullable | |

### Reference data

#### `data_provider_attributions`

Per-provider legal attribution. Surfaced at `/api/v1/legal/attributions`.

| Column              | Type                | Notes |
|---------------------|---------------------|-------|
| `id`                | `UUID` PK           | |
| `provider_slug`     | `VARCHAR(50)`, unique | |
| `provider_name`     | `VARCHAR(100)`      | |
| `attribution_text`  | `TEXT`              | Required attribution copy. |
| `attribution_url`   | `VARCHAR(500)`, nullable | |
| `logo_path`         | `VARCHAR(255)`, nullable | |
| `display_contexts`  | `JSONB`             | e.g. `["chart","attribution_page"]`. |
| `is_active`         | `BOOL`              | |
| `created_at`, `updated_at` | `TIMESTAMPTZ` | |

### Market data

#### `ohlcv_bars`

Time-series. **TimescaleDB hypertable candidate** (in production with
the extension enabled).

| Column      | Type            | Notes |
|-------------|-----------------|-------|
| `id`        | `UUID` PK       | |
| `symbol`    | `VARCHAR(20)`   | |
| `timestamp` | `TIMESTAMPTZ`   | Bar start. |
| `open`      | `NUMERIC(18, 8)`| |
| `high`      | `NUMERIC(18, 8)`| |
| `low`       | `NUMERIC(18, 8)`| |
| `close`     | `NUMERIC(18, 8)`| |
| `volume`    | `NUMERIC(24, 4)`| |

**Constraints.** `UNIQUE (symbol, timestamp)` — upserts rely on this.
**Index.** `(symbol, timestamp)` for the dominant `WHERE symbol = ?
AND timestamp BETWEEN ? AND ?` pattern.

### Scoring

#### `scoring_snapshots`

Per-run scoring output. JSONB blob lets the schema evolve without
migrations.

| Column             | Type                | Notes |
|--------------------|---------------------|-------|
| `id`               | `UUID` PK           | |
| `strategy_id`      | `VARCHAR(100)`      | |
| `universe_size`    | `INT`               | |
| `excluded_factors` | `JSONB`             | |
| `results`          | `JSONB`             | Strategy-specific output. |
| `created_at`       | `TIMESTAMPTZ`       | |

**Index.** `(strategy_id, created_at)` — recent-runs query.

## Migrations

Migrations are sequential, prefixed `001_…` through `012_…`. Each
file is a one-way upgrade; Alembic's `downgrade()` is implemented but
**not tested** in CI — operators should treat downgrades as
destructive and prefer forward-fixing.

Notable migrations:

- `001_initial_schema.py` — users, portfolios, positions, orders,
  installed_strategies, backtest_results, tax_lot_records, ohlcv_bars.
- `005_auth_rbac.py` — refresh_tokens, role column, indexes.
- `006_legal_acceptance_immutable.py` — RESTRICT FK on
  legal_acceptances.user_id.
- `010_webhooks.py` — webhook_configs, webhook_deliveries.
- `011_api_keys.py` — api_keys.
- `012_dsr_requests.py` — dsr_requests.

Adding a new table or column:

```bash
make migrate-new msg="add foo column to bar"
# edit engine/db/migrations/versions/013_*.py
make migrate
```

## Operational notes

- **Vacuum strategy.** `positions` and `tax_lot_records` are
  write-heavy during backtests; autovacuum is sufficient at the
  default scale. Operators running 10k+ backtests per day should
  lower `autovacuum_vacuum_scale_factor` on those two tables.
- **Index bloat.** `webhook_deliveries.created_at` and
  `scoring_snapshots.(strategy_id, created_at)` are insertion-hot.
  Monitor `pg_stat_user_indexes` for low `idx_scan` / high
  `idx_blks_read` ratios.
- **Time-series hypertables.** `ohlcv_bars` is the right candidate
  for `create_hypertable()` once TimescaleDB is enabled. Retention
  policy: keep at least 7 years of daily bars (regulatory floor for
  most jurisdictions).

## Constraints the ORM enforces (and Postgres doesn't)

A few rules live in code rather than the schema:

- **Role hierarchy.** The seven roles are a code-level enum in
  `engine/api/auth/dependency.py:27`. The DB has `VARCHAR(20)`.
  Adding a role requires both a code change and (optionally) a
  migration to widen the column.
- **Scope hierarchy.** Same — code-only (`read < trade < admin`).
- **Order state machine.** Enforced in `engine/core/order_manager.py`,
  not via a CHECK constraint. Auditable via the order audit log.
- **Tax lot status.** `engine/db/models.py:183` defines the enum, but
  the column is `VARCHAR(30)` so historical rows are not broken by
  enum changes.

If you find yourself adding a constraint to the model, prefer a CHECK
constraint in a new migration; it survives ORMs.
