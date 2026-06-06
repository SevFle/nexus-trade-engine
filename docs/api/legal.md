# Legal API

Read legal documents, accept them, and list acceptances + attributions.
Implementation: [`engine/api/routes/legal.py`](../../engine/api/routes/legal.py),
service layer: [`engine/legal/service.py`](../../engine/legal/service.py).

Documents live as Markdown files under `NEXUS_LEGAL_DOCUMENTS_DIR`
(default `legal/`) and are synced into the `legal_documents` table at
startup by [`engine/legal/sync.py`](../../engine/legal/sync.py).

## Endpoint summary

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET`  | `/api/v1/legal/documents`              | optional | List documents (with caller's accept state if authed) |
| `GET`  | `/api/v1/legal/documents/{slug}`       | none     | Get rendered document body |
| `POST` | `/api/v1/legal/accept`                 | JWT      | Record acceptance of one or more documents |
| `GET`  | `/api/v1/legal/acceptances/me`         | JWT      | List caller's acceptances |
| `GET`  | `/api/v1/legal/attributions`           | none     | List data-provider attribution text |

`GET /documents` and `/documents/{slug}` accept an optional
`Authorization: Bearer ...`; when present, the response is enriched
with the caller's acceptance status. Without it, the route returns the
public view. This is why these routes don't go through the
`get_current_user` dependency directly.

## Schemas

```python
class DocumentSummary(BaseModel):
    slug: str
    title: str
    current_version: str
    effective_date: date
    requires_acceptance: bool
    category: str
    accepted: bool | None            # only when authed

class DocumentDetailResponse(BaseModel):
    slug: str
    title: str
    version: str
    effective_date: date
    content_markdown: str            # front-matter stripped
    requires_acceptance: bool

class AcceptRequest(BaseModel):
    acceptances: list[AcceptItem]    # slug + version

class AcceptResponse(BaseModel):
    accepted: list[AcceptRecord]

class AttributionListResponse(BaseModel):
    attributions: list[Attribution]
```

## Document substitution

Markdown bodies support these placeholders, replaced server-side before
rendering:

| Placeholder              | Replaced with |
|--------------------------|---------------|
| `{{OPERATOR_NAME}}`      | `NEXUS_OPERATOR_NAME` |
| `{{OPERATOR_EMAIL}}`     | `NEXUS_OPERATOR_EMAIL` |
| `{{OPERATOR_URL}}`       | `NEXUS_OPERATOR_URL` |
| `{{JURISDICTION}}`       | `NEXUS_JURISDICTION` |
| `{{PLATFORM_FEE_PERCENT}}` | `NEXUS_PLATFORM_FEE_PERCENT` |
| `{{EFFECTIVE_DATE}}`     | The document's `effective_date` column |

Markdown meta-characters in the substituted values are escaped (so an
operator email containing `_` doesn't accidentally render as italics).

## Acceptance immutability

Rows in `legal_acceptances` are immutable after insert ( enforced by a
trigger added in migration `006_legal_acceptance_immutable.py`). A
user can *re-accept* (creating a new row) but cannot edit or delete an
existing acceptance. This is the audit trail — every acceptance event
is permanent.

## Examples

```bash
# List (public)
curl http://localhost:8000/api/v1/legal/documents

# List with my acceptances
curl http://localhost:8000/api/v1/legal/documents \
  -H 'authorization: Bearer <access>'

# Read the Terms
curl http://localhost:8000/api/v1/legal/documents/terms-of-service

# Accept
curl -X POST http://localhost:8000/api/v1/legal/accept \
  -H 'authorization: Bearer <access>' \
  -H 'content-type: application/json' \
  -d '{"acceptances":[{"slug":"terms-of-service","version":"1.2.0"}]}'

# My history
curl http://localhost:8000/api/v1/legal/acceptances/me \
  -H 'authorization: Bearer <access>'
```

## Errors

| Status | When |
|---|---|
| `400` | `slug` fails the `^[a-z0-9-]+$` pattern. |
| `401` | `POST /accept` or `/acceptances/me` without a valid token. |
| `404` | Unknown `slug` or `version`. |
| `409` | (Service-layer) attempt to re-accept the same version of the same slug. |

## Legal gate

Routes mounted with `Depends(require_legal_acceptance)` will return
`403 legal_acceptance_required` until the caller has accepted the
current version of every `requires_acceptance=true` document. Gated
areas today: `/api/v1/backtest`, `/api/v1/scoring`,
`/api/v1/market-data`, `/api/v1/portfolio`, `/api/v1/strategies`,
`/api/v1/marketplace`. See [`README.md`](README.md) for the full list.

## Related

- [`docs/legal/processors.md`](../legal/processors.md) — how document
  bodies are processed.
- [ADR-0002 — Auth & RBAC](../adr/0002-auth-rbac.md) — context for the
  legal-acceptance gate.
