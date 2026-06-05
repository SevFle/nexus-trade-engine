# Legal API

Mounted at the root (no `/api/v1` prefix on list / accept / acceptance
endpoints; the operator surface is intentionally stable across
versions). Implementation: `engine/api/routes/legal.py`. Service:
`engine/legal/service.py`. Sync: `engine/legal/sync.py`.

Legal document registry: ToS, Privacy, Risk Disclaimer, EULA, etc.
The engine loads markdown files from `legal/` at startup
(`sync_legal_documents` in `engine/app.py:lifespan`), upserts them
into the `legal_documents` table, and gates mutation endpoints on
documented user acceptance.

Substitution variables are applied server-side so a single source of
truth works across operators (`{{OPERATOR_NAME}}`,
`{{OPERATOR_EMAIL}}`, `{{OPERATOR_URL}}`, `{{JURISDICTION}}`,
`{{PLATFORM_FEE_PERCENT}}`, `{{EFFECTIVE_DATE}}`).

## GET /api/v1/legal/documents

List documents. Auth is *optional* — anonymous clients see the same
list, but acceptance status is only populated when authenticated.

**Query params**

| Param      | Type   | Default | Notes                       |
|------------|--------|---------|-----------------------------|
| `category` | string | null    | Filter (e.g. `terms`, `privacy`) |

**Response** `DocumentListResponse`:
```json
{
  "documents": [
    {
      "slug": "terms-of-service",
      "title": "Terms of Service",
      "current_version": "2.1.0",
      "effective_date": "2026-01-01",
      "requires_acceptance": true,
      "category": "terms",
      "accepted": true,
      "accepted_version": "2.1.0",
      "accepted_at": "2026-01-04T12:00:00Z"
    }
  ]
}
```

`accepted` / `accepted_version` / `accepted_at` are populated only
when the caller is authenticated.

## GET /api/v1/legal/documents/{slug}

Read one document. Substitution variables are expanded; front-matter
is stripped.

**Path** — `slug` matching `^[a-z0-9-]+$`.

**Query params** — `version` (optional; defaults to `current_version`).

**Response** `DocumentDetailResponse`:
```json
{
  "slug": "terms-of-service",
  "title": "Terms of Service",
  "version": "2.1.0",
  "effective_date": "2026-01-01",
  "content_markdown": "# Terms of Service\n\n...",
  "requires_acceptance": true
}
```

**Errors** — `404 Not Found` if the slug doesn't resolve.

## POST /api/v1/legal/accept

Record acceptance for one or more documents. Atomic per call: either
all succeed or all fail.

**Auth** — required.

**Request body** `AcceptRequest`:
```json
{
  "acceptances": [
    { "document_slug": "terms-of-service", "document_version": "2.1.0" },
    { "document_slug": "privacy-policy",   "document_version": "1.0.0" }
  ]
}
```

**Response** `AcceptResponse`:
```json
{
  "accepted": [
    { "document_slug": "terms-of-service", "document_version": "2.1.0",
      "accepted_at": "..." }
  ]
}
```

Each row captures the caller's IP and user agent for audit. Rows are
**immutable** after creation — see migration `006_legal_acceptance_immutable`.

## GET /api/v1/legal/acceptances/me

List the caller's acceptance history.

**Query params** — `document_slug` (optional filter).

**Response** `AcceptanceListResponse`.

## GET /api/v1/legal/attributions

List data-provider attributions shown in the marketplace / settings UI.

**Query params** — `context` (optional; e.g. `marketplace`,
`settings`).

**Response** `AttributionListResponse`:
```json
{
  "attributions": [
    {
      "provider_slug": "yahoo",
      "provider_name": "Yahoo Finance",
      "attribution_text": "Data provided by Yahoo Finance",
      "attribution_url": "https://...",
      "logo_path": null,
      "display_contexts": ["marketplace", "settings"]
    }
  ]
}
```

## How legal gating works

`engine/legal/dependencies.py:require_legal_acceptance` is a FastAPI
dependency applied at the router level
(`engine/api/router.py`). For protected routers it runs **before**
the route handler:

1. Look up the user's acceptances.
2. For every document with `requires_acceptance=True`, check that
   the user has accepted **at least** `current_version`.
3. If any are missing → `403 Forbidden` with
   `{"detail": "Legal acceptance required", "missing": [...]}`.

The list of routers that enforce acceptance today:

- `backtest_router`
- `portfolio_router`
- `strategies_router`
- `market_data_router`
- `scoring_router`
- `marketplace_router`

Add the dependency to any new router that touches money decisions.

## Operator substitutions

`engine/config.py` settings drive the substitutions:

| Setting                       | Replaces                |
|-------------------------------|-------------------------|
| `NEXUS_OPERATOR_NAME`         | `{{OPERATOR_NAME}}`     |
| `NEXUS_OPERATOR_EMAIL`        | `{{OPERATOR_EMAIL}}`    |
| `NEXUS_OPERATOR_URL`          | `{{OPERATOR_URL}}`      |
| `NEXUS_JURISDICTION`          | `{{JURISDICTION}}`      |
| `NEXUS_PLATFORM_FEE_PERCENT`  | `{{PLATFORM_FEE_PERCENT}}` |
| (per-document effective_date) | `{{EFFECTIVE_DATE}}`    |

Substitution strings are escaped against Markdown special chars to
prevent injection through operator-supplied values.
