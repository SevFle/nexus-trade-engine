# Legal documents

Read, accept, and audit the legal documents the operator requires
before users can use the trading surface. Source:
[`engine/api/routes/legal.py`](../../engine/api/routes/legal.py),
[`engine/legal/`](../../engine/legal/).

Documents are Markdown files in
[`legal/`](../../legal/) at the repo root. On startup the engine
calls
[`sync_legal_documents`](../../engine/legal/sync.py) which
upserts a `legal_documents` row for every file detected, bumping
`current_version` and `effective_date` when the content changes.
The legal-acceptance dependency then gates routes on whether the
calling user has accepted the **current** version of every
document marked `requires_acceptance=True`.

## Endpoints

### `GET /api/v1/legal/documents`

List documents, optionally filtered by category. If the caller
presents a valid JWT, the response includes per-document
`accepted` (boolean) and `accepted_version` for that user.

**Query:** `category` (optional) — `general`, `terms`,
`privacy`, `risk_disclaimer`, `marketplace`, etc.

**Response** `200 OK`:

```json
{
  "documents": [
    {
      "slug": "terms-of-service",
      "title": "Terms of Service",
      "category": "terms",
      "current_version": "2026-04-15",
      "effective_date": "2026-04-15",
      "requires_acceptance": true,
      "accepted": true,
      "accepted_version": "2026-04-15"
    }
  ]
}
```

### `GET /api/v1/legal/documents/{slug}`

Fetch one document's content, with operator substitutions
applied. The following placeholders are templated server-side:

| Placeholder              | Source                                       |
|--------------------------|----------------------------------------------|
| `{{OPERATOR_NAME}}`      | `NEXUS_OPERATOR_NAME`                        |
| `{{OPERATOR_EMAIL}}`     | `NEXUS_OPERATOR_EMAIL`                       |
| `{{OPERATOR_URL}}`       | `NEXUS_OPERATOR_URL`                         |
| `{{JURISDICTION}}`       | `NEXUS_JURISDICTION`                         |
| `{{PLATFORM_FEE_PERCENT}}` | `NEXUS_PLATFORM_FEE_PERCENT`               |
| `{{EFFECTIVE_DATE}}`     | Document's `effective_date`                  |

Markdown special characters in the substituted values are escaped
(`\` `*` `_` `{` `}` `[` `]` `(` `)` `#` `+` `-` `.` `!` `|` `~`
`>`). YAML front matter (a `---\n...\n---\n` block at the top) is
stripped.

**Query:** `version` (optional) — fetch a specific historical
version. If omitted, the current version is returned.

**Response** `200 OK`:

```json
{
  "slug": "terms-of-service",
  "title": "Terms of Service",
  "version": "2026-04-15",
  "effective_date": "2026-04-15",
  "content_markdown": "# Terms of Service\n\nBy using ...",
  "requires_acceptance": true
}
```

`404 Not Found` if the slug does not exist.

### `POST /api/v1/legal/accept`

Record acceptance of one or more document versions. The IP and
user-agent are stamped for audit; the row is append-only (see
[`legal_acceptances` constraint](../architecture/database.md)).

**Auth:** JWT.

**Request body** — `AcceptRequest`:

```json
{
  "acceptances": [
    { "document_slug": "terms-of-service", "document_version": "2026-04-15" },
    { "document_slug": "privacy-policy",   "document_version": "2026-04-15" }
  ]
}
```

**Response** `200 OK` — `AcceptResponse`:

```json
{ "accepted": [{ "document_slug": "terms-of-service", "document_version": "2026-04-15" }] }
```

`400` if any document is unknown or no longer current (the caller
must accept the **current** version, not an old one).

### `GET /api/v1/legal/acceptances/me`

List the caller's acceptance audit. Newest first.

**Query:** `document_slug` (optional).

### `GET /api/v1/legal/attributions`

List the data-provider attributions required for display in the
UI footer / settings page. Public.

**Query:** `context` (optional) — filter by display context
(`footer`, `about`, `settings`).

## Operator workflow

1. Drop a Markdown file in `legal/`, e.g. `legal/refund-policy.md`.
2. Add front matter (or rely on the auto-generated slug from the
   filename).
3. On the next deploy, the engine calls `sync_legal_documents`,
   which inserts a `legal_documents` row.
4. Existing users will now fail the legal-acceptance gate until
   they sign; new users sign during onboarding.

## Notes

- The acceptance row is immutable (migration 006). If you need to
  "revoke" an acceptance, write a new row with `revoked_at` set;
  the dependency does not honour revoked rows for the gate, but
  the audit trail remains.
- Operators **must** keep their own
  [`docs/legal/processors.md`](../legal/processors.md) current —
  the upstream repo's copy is a template, not a real inventory.
