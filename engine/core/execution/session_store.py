"""
Redis-backed store for paper trading session state.

Provides cross-process access to session metadata so the API server
and taskiq workers can share session state. Falls back to in-memory
when Redis is unavailable.
"""

from __future__ import annotations

import json
import time
from typing import Any

import structlog

from engine.config import settings

logger = structlog.get_logger()

_KEY_PREFIX = "paper:session:"
_SESSIONS_TTL_SECONDS = 86400 * 7


def _key(session_id: str) -> str:
    return f"{_KEY_PREFIX}{session_id}"


class PaperSessionStore:
    """Shared paper session store backed by Valkey/Redis."""

    def __init__(self, client: Any = None) -> None:
        self._client = client
        self._local_fallback: dict[str, dict[str, Any]] = {}

    async def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        from valkey.asyncio import Valkey  # noqa: PLC0415

        self._client = Valkey.from_url(settings.valkey_url)
        return self._client

    async def save(self, session_id: str, data: dict[str, Any]) -> None:
        payload = {**data, "_updated_at": time.monotonic()}
        try:
            client = await self._get_client()
            serialized = json.dumps(payload, default=str)
            await client.set(
                _key(session_id), serialized, ex=_SESSIONS_TTL_SECONDS
            )
        except Exception:
            logger.exception("paper_store.save_fallback", session_id=session_id)
            self._local_fallback[session_id] = payload

    async def get(self, session_id: str) -> dict[str, Any] | None:
        try:
            client = await self._get_client()
            raw = await client.get(_key(session_id))
            if raw is not None:
                return json.loads(raw)
        except Exception:
            logger.exception("paper_store.get_redis_failed", session_id=session_id)

        return self._local_fallback.get(session_id)

    async def delete(self, session_id: str) -> None:
        try:
            client = await self._get_client()
            await client.delete(_key(session_id))
        except Exception:
            logger.exception("paper_store.delete_failed", session_id=session_id)
        self._local_fallback.pop(session_id, None)

    async def list_by_user(self, user_id: str) -> list[dict[str, Any]]:
        try:
            client = await self._get_client()
            cursor = 0
            results: list[dict[str, Any]] = []
            while True:
                cursor, keys = await client.scan(
                    cursor=cursor, match=f"{_KEY_PREFIX}*", count=100
                )
                for k in keys:
                    raw = await client.get(k)
                    if raw is None:
                        continue
                    data = json.loads(raw)
                    if data.get("user_id") == user_id:
                        results.append(data)
                if cursor == 0:
                    break
            return sorted(results, key=lambda d: d.get("_updated_at", 0), reverse=True)
        except Exception:
            logger.exception("paper_store.list_fallback", user_id=user_id)
            return [
                v
                for v in self._local_fallback.values()
                if v.get("user_id") == user_id
            ]

    async def evict_expired(self) -> None:
        now = time.monotonic()
        expired = [
            k
            for k, v in self._local_fallback.items()
            if now - v.get("_updated_at", 0) > _SESSIONS_TTL_SECONDS
        ]
        for k in expired:
            del self._local_fallback[k]

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


_store: PaperSessionStore | None = None


async def get_paper_session_store() -> PaperSessionStore:
    global _store  # noqa: PLW0603
    if _store is None:
        _store = PaperSessionStore()
    return _store


def set_paper_session_store(store: PaperSessionStore) -> None:
    global _store  # noqa: PLW0603
    _store = store
