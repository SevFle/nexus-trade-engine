"""Webhook dispatcher (gh#80).

Subscribes to event-bus events, fans out matching events to user-
configured webhooks. Each delivery is signed with HMAC-SHA256 over
the canonical JSON payload (header ``X-Nexus-Signature: sha256=<hex>``)
and retried with exponential backoff on transient (5xx / network)
failures. Every attempt persists to ``webhook_deliveries`` for audit
+ replay.

Templates ('generic', 'discord', 'slack', 'telegram') reshape the
payload to match each platform's incoming-webhook schema. The signing
header is computed against the *outgoing* body (post-template) so
signature verification matches what the receiver sees on the wire.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from engine.db.models import WebhookConfig, WebhookDelivery
from engine.events.bus import EventBus, EventType
from engine.observability.metrics import MetricsBackend, get_metrics

logger = structlog.get_logger()


_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
_DEFAULT_TIMEOUT = 10.0


def canonical_payload(event_type: str, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "event": event_type,
        "timestamp": datetime.now(UTC).isoformat(),
        "data": data,
    }


def sign_payload(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def render_template(template: str, payload: dict[str, Any]) -> dict[str, Any]:
    if template == "generic":
        return payload
    if template == "discord":
        return {
            "content": f"**{payload['event']}**",
            "embeds": [
                {
                    "title": payload["event"],
                    "description": json.dumps(payload["data"], indent=2),
                    "timestamp": payload["timestamp"],
                }
            ],
        }
    if template == "slack":
        return {
            "blocks": [
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": payload["event"]},
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"```{json.dumps(payload['data'], indent=2)}```",
                    },
                },
            ]
        }
    if template == "telegram":
        return {
            "text": (
                f"*{payload['event']}*\n"
                f"```\n{json.dumps(payload['data'], indent=2)}\n```"
            ),
            "parse_mode": "Markdown",
        }
    return payload


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff: 1s, 2s, 4s, 8s, ... capped at 60s."""
    return min(2 ** (attempt - 1), 60.0)


class WebhookDispatcher:
    """Subscribes to the event bus and dispatches webhooks."""

    def __init__(
        self,
        bus: EventBus,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]]
        | Callable[[], Awaitable[AsyncSession]],
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
        metrics: MetricsBackend | None = None,
    ) -> None:
        self._bus = bus
        self._session_factory = session_factory
        self._http = http_client or httpx.AsyncClient(timeout=timeout)
        self._owns_http = http_client is None
        self._sleep = sleep_fn or asyncio.sleep
        self._subscribed: list[EventType] = []
        self._metrics = metrics

    @property
    def metrics(self) -> MetricsBackend:
        """Resolve the metrics backend lazily so tests can swap the
        process-singleton via :func:`set_metrics` after construction."""
        return self._metrics if self._metrics is not None else get_metrics()

    def subscribe_to(self, event_types: list[EventType]) -> None:
        for et in event_types:
            self._bus.subscribe(et, self._on_event)
            self._subscribed.append(et)

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _on_event(self, event: dict) -> None:
        event_type = event.get("type", "")
        async with self._session_factory() as session:
            configs = await self._matching_configs(session, event_type)
            for cfg in configs:
                await self.dispatch_one(session, cfg, event_type, event.get("data", {}))
            await session.commit()

    async def _matching_configs(
        self, session: AsyncSession, event_type: str
    ) -> list[WebhookConfig]:
        result = await session.execute(
            select(WebhookConfig).where(WebhookConfig.is_active.is_(True))
        )
        configs = list(result.scalars().all())
        return [c for c in configs if not c.event_types or event_type in c.event_types]

    async def dispatch_one(
        self,
        session: AsyncSession,
        cfg: WebhookConfig,
        event_type: str,
        data: dict[str, Any],
    ) -> WebhookDelivery:
        canonical = canonical_payload(event_type, data)
        outgoing = render_template(cfg.template, canonical)
        body = json.dumps(outgoing, separators=(",", ":")).encode("utf-8")
        signature = sign_payload(cfg.signing_secret, body)
        headers = {
            "Content-Type": "application/json",
            "X-Nexus-Signature": signature,
            "X-Nexus-Event": event_type,
            **(cfg.custom_headers or {}),
        }

        delivery = WebhookDelivery(
            id=uuid.uuid4(),
            webhook_id=cfg.id,
            event_type=event_type,
            payload=canonical,
        )
        session.add(delivery)
        await session.flush()

        metrics = self.metrics
        base_tags = {"event_type": event_type, "template": cfg.template}

        max_attempts = max(1, cfg.max_retries)
        for attempt in range(1, max_attempts + 1):
            delivery.attempts = attempt
            metrics.counter("webhook.attempts", tags=base_tags)
            t0 = time.monotonic()
            try:
                resp = await self._http.post(cfg.url, content=body, headers=headers)
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                delivery.response_status = resp.status_code
                delivery.response_ms = elapsed_ms
                metrics.histogram(
                    "webhook.duration_ms",
                    float(elapsed_ms),
                    tags={**base_tags, "status": str(resp.status_code)},
                )
                if 200 <= resp.status_code < 300:
                    delivery.status = "delivered"
                    delivery.delivered_at = datetime.now(UTC)
                    delivery.error = None
                    await session.flush()
                    metrics.counter("webhook.delivered", tags=base_tags)
                    logger.info(
                        "webhook.delivered",
                        webhook_id=str(cfg.id),
                        event_type=event_type,
                        ms=elapsed_ms,
                    )
                    return delivery
                if resp.status_code not in _RETRYABLE_STATUS:
                    delivery.status = "failed"
                    delivery.error = f"HTTP {resp.status_code} (non-retryable)"
                    await session.flush()
                    metrics.counter(
                        "webhook.failed",
                        tags={**base_tags, "reason": "non_retryable"},
                    )
                    logger.warning(
                        "webhook.failed_non_retryable",
                        webhook_id=str(cfg.id),
                        status=resp.status_code,
                    )
                    return delivery
                delivery.error = f"HTTP {resp.status_code}"
            except httpx.HTTPError as exc:
                delivery.error = f"{type(exc).__name__}: {exc}"
                delivery.response_status = None
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                delivery.response_ms = elapsed_ms
                metrics.histogram(
                    "webhook.duration_ms",
                    float(elapsed_ms),
                    tags={**base_tags, "status": "network_error"},
                )

            if attempt < max_attempts:
                await self._sleep(_backoff_delay(attempt))

        delivery.status = "failed"
        await session.flush()
        metrics.counter(
            "webhook.failed",
            tags={**base_tags, "reason": "exhausted"},
        )
        logger.warning(
            "webhook.exhausted",
            webhook_id=str(cfg.id),
            event_type=event_type,
            attempts=max_attempts,
        )
        return delivery


__all__ = [
    "WebhookDispatcher",
    "canonical_payload",
    "render_template",
    "sign_payload",
]
