# Legal API

Source: [`engine/api/routes/legal.py`](../../engine/api/routes/legal.py),
[`engine/legal/`](../../engine/legal/).

The engine requires every user to accept the current version of
each `requires_acceptance=true` legal document before they can hit
gated routes (`/api/v1/portfolio`, `/api/v1/strategies`,
`/api/v1/backtest`, etc.). The list of documents is seeded from
`legal/` on startup; operators can extend the directory and the
engine will pick them up.

Documents are written in Markdown with optional YAML front-matter.
Template variables in the body are substituted at read time:

| Placeholder              | Replaced with                                 |
|--------------------------|-----------------------------------------------|
| `{{OPERATOR_NAME}}`      | `settings.operator_name`                      |
| `{{OPERATOR_EMAIL}}`     | `settings.operator_email`                     |
| `{{OPERATOR_URL}}`       | `settings.operator_url`                       |
| `{{JURISDICTION}}`       | `settings.jurisdiction`                       |
| `{{PLATFORM_FEE_PERCENT}}` | `settings.platform_fee_percent`             |
| `{{EFFECTIVE_DATE}}`     | The document's `effective_date`.              |

## Endpoints

### `GET /api/v1/legal/documents`

List every active legal document, optionally filtered by category.
If the caller is authenticated, the response includes per-document
acceptance status for that user.

**Auth**: optional (Bearer JWT). When present, the response includes
"has the current user accepted the current version of this document".

**Query params**:

| Name       | Type   | Default | Notes                          |
|------------|--------|---------|--------------------------------|
| `category` | string | null    | Filter by `legal_documents.category`. |

**Response**: `200 OK`:

```json
{
  "documents": [
    {
      "slug": "terms-of-service",
      "title": "Terms of Service",
      "current_version": "1.2.0",
      "effective_date": "2026-01-01",
      "category": "terms",
      "requires_acceptance": true,
      "accepted": true,
      "accepted_version": "1.2.0",
      "accepted_at": "2026-01-15T10:30:00Z"
    }
  ]
}
```

### `GET /api/v1/legal/documents/{slug}`

Fetch the rendered markdown body of one document. The version can be
pinned via query param; default is the current version.

**Path params**: `slug` — `^[a-z0-9-]+$`.

**Query params**:

| Name      | Type   | Default | Notes                            |
|-----------|--------|---------|----------------------------------|
| `version` | string | current | Specific semver to fetch.        |

**Response**: `200 OK`:

```json
{
  "slug": "terms-of-service",
  "title": "Terms of Service",
  "version": "1.2.0",
  "effective_date": "2026-01-01",
  "content_markdown": "# Terms of Service\n\n... rendered markdown ...",
  "requires_acceptance": true
}
```

`404` if the slug does not exist or the requested version is not
found.

### `POST /api/v1/legal/accept`

Record acceptance for one or more documents. Acceptance rows are
**immutable** after insert (enforced by a trigger — see migration
`006`); each `(user, document, version)` tuple produces a fresh row.

**Auth**: Bearer JWT or API key.

**Request body**:

```json
{
  "acceptances": [
    { "document_slug": "terms-of-service", "document_version": "1.2.0" },
    { "document_slug": "privacy-policy",   "document_version": "2.0.0" }
  ]
}
```

**Response**: `200 OK`:

```json
{
  "accepted": [
    { "document_slug": "terms-of-service", "document_version": "1.2.0", "accepted_at": "..." },
    { "document_slug": "privacy-policy",   "document_version": "2.0.0", "accepted_at": "..." }
  ]
}
```

The server records `ip_address` and `user_agent` from the request for
audit purposes.

### `GET /api/v1/legal/acceptances/me`

List the caller's acceptance history.

**Auth**: Bearer JWT or API key.

**Query params**:

| Name            | Type   | Default | Notes                              |
|-----------------|--------|---------|------------------------------------|
| `document_slug` | string | null    | Filter to one document.            |

**Response**: `200 OK`:

```json
{
  "acceptances": [
    {
      "document_slug": "terms-of-service",
      "document_version": "1.2.0",
      "accepted_at": "2026-01-15T10:30:00Z",
      "ip_address": "192.0.2.10",
      "user_agent": "Mozilla/5.0 ...",
      "context": "onboarding"
    }
  ]
}
```

### `GET /api/v1/legal/attributions`

List data-provider attributions. Surfaced in the UI footer per
provider TOS.

**Auth**: none.

**Query params**:

| Name      | Type   | Default | Notes                                       |
|-----------|--------|---------|---------------------------------------------|
| `context` | string | null    | Filter by `display_contexts` JSONB array.   |

**Response**: `200 OK`:

```json
{
  "attributions": [
    {
      "provider_slug": "yahoo",
      "provider_name": "Yahoo Finance",
      "attribution_text": "Data provided by Yahoo Finance.",
      "attribution_url": "https://...",
      "logo_path": "/logos/yahoo.png",
      "display_contexts": ["ui_footer", "export_header"]
    }
  ]
}
```

## Operator setup

Operators edit the legal corpus by dropping markdown files into
`legal/`. Each file should declare its metadata in the engine's
sync format (see `engine/legal/sync.py`); on app start the engine
upserts a `legal_documents` row per file. Bumping the version in the
file forces every user to re-accept on next gated request.
