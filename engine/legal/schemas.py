from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    from datetime import date, datetime


class LegalDocumentSummary(BaseModel):
    slug: str
    title: str
    current_version: str
    effective_date: date
    requires_acceptance: bool
    category: str
    accepted: bool = False
    accepted_version: str | None = None
    needs_re_acceptance: bool = False


class LegalDocumentListResponse(BaseModel):
    documents: list[LegalDocumentSummary]


class LegalDocumentDetail(BaseModel):
    slug: str
    title: str
    version: str
    effective_date: date
    content_markdown: str
    requires_acceptance: bool


class AcceptanceItem(BaseModel):
    document_slug: str
    document_version: str


class AcceptRequest(BaseModel):
    acceptances: list[AcceptanceItem]


class AcceptedItem(BaseModel):
    document_slug: str
    document_version: str
    accepted_at: datetime


class AcceptResponse(BaseModel):
    accepted: list[AcceptedItem]


class AcceptanceRecord(BaseModel):
    id: str
    document_slug: str
    document_version: str
    accepted_at: datetime
    context: str
    revoked_at: datetime | None = None

    model_config: dict[str, Any] = {"from_attributes": True}


class AcceptanceListResponse(BaseModel):
    acceptances: list[AcceptanceRecord]


class AttributionItem(BaseModel):
    provider_slug: str
    provider_name: str
    attribution_text: str
    attribution_url: str | None = None
    logo_path: str | None = None


class AttributionListResponse(BaseModel):
    attributions: list[AttributionItem]


class PendingAcceptanceDetail(BaseModel):
    code: str = "legal_re_acceptance_required"
    documents: list[str]
