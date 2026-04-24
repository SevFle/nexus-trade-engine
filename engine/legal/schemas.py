from __future__ import annotations

from datetime import date, datetime  # noqa: TC003

from pydantic import BaseModel, Field


class DocumentSummary(BaseModel):
    slug: str
    title: str
    current_version: str
    effective_date: date
    requires_acceptance: bool
    category: str
    accepted: bool = False
    accepted_version: str | None = None
    needs_re_acceptance: bool = False


class DocumentListResponse(BaseModel):
    documents: list[DocumentSummary]


class DocumentDetailResponse(BaseModel):
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
    acceptances: list[AcceptanceItem] = Field(min_length=1)


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
