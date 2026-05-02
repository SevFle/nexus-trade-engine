"""Webhook CRUD + test + deliveries routes (gh#80)."""

from __future__ import annotations

import secrets
import uuid
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import desc, select

from engine.api.auth.dependency import get_current_user, require_api_scope
from engine.db.models import User, WebhookConfig, WebhookDelivery
from engine.deps import get_db
from engine.events.bus import EventBus
from engine.events.webhook_dispatcher import WebhookDispatcher

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()

router = APIRouter()


_VALID_TEMPLATES = {"generic", "discord", "slack", "telegram"}


class WebhookCreateRequest(BaseModel):
    url: HttpUrl
    event_types: list[str] = Field(default_factory=list)
    custom_headers: dict[str, str] = Field(default_factory=dict)
    template: str = "generic"
    max_retries: int = Field(default=3, ge=1, le=10)
    portfolio_id: uuid.UUID | None = None


class WebhookUpdateRequest(BaseModel):
    url: HttpUrl | None = None
    event_types: list[str] | None = None
    custom_headers: dict[str, str] | None = None
    template: str | None = None
    max_retries: int | None = Field(default=None, ge=1, le=10)
    is_active: bool | None = None


class WebhookResponse(BaseModel):
    id: uuid.UUID
    url: str
    event_types: list[str]
    template: str
    max_retries: int
    is_active: bool
    portfolio_id: uuid.UUID | None
    signing_secret: str | None = None  # only echoed on create


class DeliveryResponse(BaseModel):
    id: uuid.UUID
    event_type: str
    status: str
    response_status: int | None
    response_ms: int | None
    attempts: int
    error: str | None
    created_at: str
    delivered_at: str | None


def _to_response(cfg: WebhookConfig, *, include_secret: bool = False) -> WebhookResponse:
    return WebhookResponse(
        id=cfg.id,
        url=cfg.url,
        event_types=list(cfg.event_types or []),
        template=cfg.template,
        max_retries=cfg.max_retries,
        is_active=cfg.is_active,
        portfolio_id=cfg.portfolio_id,
        signing_secret=cfg.signing_secret if include_secret else None,
    )


def _validate_template(template: str) -> None:
    if template not in _VALID_TEMPLATES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"template must be one of {sorted(_VALID_TEMPLATES)}",
        )


@router.post("", response_model=WebhookResponse, status_code=status.HTTP_201_CREATED)
async def create_webhook(
    body: WebhookCreateRequest,
    user: User = Depends(require_api_scope("trade")),
    db: AsyncSession = Depends(get_db),
) -> WebhookResponse:
    _validate_template(body.template)
    cfg = WebhookConfig(
        id=uuid.uuid4(),
        user_id=user.id,
        portfolio_id=body.portfolio_id,
        url=str(body.url),
        event_types=body.event_types,
        signing_secret=secrets.token_urlsafe(32),
        custom_headers=body.custom_headers,
        template=body.template,
        max_retries=body.max_retries,
        is_active=True,
    )
    db.add(cfg)
    await db.flush()
    logger.info("webhook.created", webhook_id=str(cfg.id), user_id=str(user.id))
    return _to_response(cfg, include_secret=True)


@router.get("", response_model=list[WebhookResponse])
async def list_webhooks(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[WebhookResponse]:
    result = await db.execute(
        select(WebhookConfig)
        .where(WebhookConfig.user_id == user.id)
        .order_by(WebhookConfig.created_at.desc())
    )
    return [_to_response(cfg) for cfg in result.scalars().all()]


async def _get_owned(
    webhook_id: uuid.UUID, user: User, db: AsyncSession
) -> WebhookConfig:
    cfg = await db.get(WebhookConfig, webhook_id)
    if cfg is None or cfg.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Webhook not found"
        )
    return cfg


@router.put("/{webhook_id}", response_model=WebhookResponse)
async def update_webhook(
    webhook_id: uuid.UUID,
    body: WebhookUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WebhookResponse:
    cfg = await _get_owned(webhook_id, user, db)
    if body.template is not None:
        _validate_template(body.template)
    if body.url is not None:
        cfg.url = str(body.url)
    if body.event_types is not None:
        cfg.event_types = body.event_types
    if body.custom_headers is not None:
        cfg.custom_headers = body.custom_headers
    if body.template is not None:
        cfg.template = body.template
    if body.max_retries is not None:
        cfg.max_retries = body.max_retries
    if body.is_active is not None:
        cfg.is_active = body.is_active
    db.add(cfg)
    await db.flush()
    return _to_response(cfg)


@router.delete("/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(
    webhook_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    cfg = await _get_owned(webhook_id, user, db)
    await db.delete(cfg)
    await db.flush()


@router.post("/{webhook_id}/test", response_model=DeliveryResponse)
async def test_webhook(
    webhook_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DeliveryResponse:
    cfg = await _get_owned(webhook_id, user, db)

    @asynccontextmanager
    async def _bound_session():
        yield db

    async with httpx.AsyncClient(timeout=10.0) as http:
        dispatcher = WebhookDispatcher(
            bus=EventBus(),
            session_factory=_bound_session,
            http_client=http,
        )
        delivery = await dispatcher.dispatch_one(
            db,
            cfg,
            "test.event",
            {"message": "Test webhook delivery from Nexus Trade Engine"},
        )
    return DeliveryResponse(
        id=delivery.id,
        event_type=delivery.event_type,
        status=delivery.status,
        response_status=delivery.response_status,
        response_ms=delivery.response_ms,
        attempts=delivery.attempts,
        error=delivery.error,
        created_at=delivery.created_at.isoformat() if delivery.created_at else "",
        delivered_at=delivery.delivered_at.isoformat() if delivery.delivered_at else None,
    )


@router.get("/{webhook_id}/deliveries", response_model=list[DeliveryResponse])
async def list_deliveries(
    webhook_id: uuid.UUID,
    limit: int = 50,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[DeliveryResponse]:
    await _get_owned(webhook_id, user, db)
    result = await db.execute(
        select(WebhookDelivery)
        .where(WebhookDelivery.webhook_id == webhook_id)
        .order_by(desc(WebhookDelivery.created_at))
        .limit(min(max(1, limit), 200))
    )
    rows = result.scalars().all()
    return [
        DeliveryResponse(
            id=d.id,
            event_type=d.event_type,
            status=d.status,
            response_status=d.response_status,
            response_ms=d.response_ms,
            attempts=d.attempts,
            error=d.error,
            created_at=d.created_at.isoformat() if d.created_at else "",
            delivered_at=d.delivered_at.isoformat() if d.delivered_at else None,
        )
        for d in rows
    ]


__all__ = ["router"]
