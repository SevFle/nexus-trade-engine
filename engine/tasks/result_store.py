"""Redis-backed backtest result store for cross-process result sharing.

Both the API server and taskiq worker use this module to store and
retrieve backtest results via Valkey/Redis, replacing the in-process
dict that only worked with FastAPI BackgroundTasks.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

import structlog

from engine.config import settings

if TYPE_CHECKING:
    from valkey.asyncio import Valkey

logger = structlog.get_logger()

_KEY_PREFIX = "backtest:result:"
_RESULTS_TTL_SECONDS = 3600


def _key(backtest_id: str) -> str:
    return f"{_KEY_PREFIX}{backtest_id}"


class BacktestResultStore:
    """Shared backtest result store backed by Valkey/Redis."""

    def __init__(self, client: Valkey | None = None) -> None:
        self._client = client
        self._local_fallback: dict[str, tuple[float, str, dict[str, Any]]] = {}

    async def _get_client(self) -> Valkey:
        if self._client is not None:
            return self._client
        from valkey.asyncio import Valkey as _Valkey  # noqa: PLC0415

        self._client = _Valkey.from_url(settings.valkey_url)
        return self._client

    async def set_running(
        self, backtest_id: str, user_id: str, strategy_name: str, symbol: str
    ) -> None:
        data = {
            "user_id": user_id,
            "status": "running",
            "strategy_name": strategy_name,
            "symbol": symbol,
            "updated_at": time.monotonic(),
        }
        try:
            client = await self._get_client()
            await client.set(_key(backtest_id), json.dumps(data), ex=_RESULTS_TTL_SECONDS)
        except Exception:
            logger.exception("result_store.set_running_fallback", backtest_id=backtest_id)
            self._local_fallback[backtest_id] = (time.monotonic(), user_id, data)

    async def set_completed(
        self, backtest_id: str, user_id: str, result_data: dict[str, Any]
    ) -> None:
        data = {
            "user_id": user_id,
            "status": "completed",
            **result_data,
            "updated_at": time.monotonic(),
        }
        try:
            client = await self._get_client()
            await client.set(_key(backtest_id), json.dumps(data), ex=_RESULTS_TTL_SECONDS)
        except Exception:
            logger.exception("result_store.set_completed_fallback", backtest_id=backtest_id)
            self._local_fallback[backtest_id] = (time.monotonic(), user_id, data)

    async def set_failed(
        self,
        backtest_id: str,
        user_id: str,
        strategy_name: str,
        symbol: str,
        error: str,
        error_type: str,
    ) -> None:
        data = {
            "user_id": user_id,
            "status": "failed",
            "strategy_name": strategy_name,
            "symbol": symbol,
            "error": error,
            "error_type": error_type,
            "updated_at": time.monotonic(),
        }
        try:
            client = await self._get_client()
            await client.set(_key(backtest_id), json.dumps(data), ex=_RESULTS_TTL_SECONDS)
        except Exception:
            logger.exception("result_store.set_failed_fallback", backtest_id=backtest_id)
            self._local_fallback[backtest_id] = (time.monotonic(), user_id, data)

    async def get(self, backtest_id: str) -> dict[str, Any] | None:
        try:
            client = await self._get_client()
            raw = await client.get(_key(backtest_id))
            if raw is not None:
                return json.loads(raw)
        except Exception:
            logger.exception("result_store.get_redis_failed", backtest_id=backtest_id)

        entry = self._local_fallback.get(backtest_id)
        if entry is not None:
            return entry[2]
        return None

    async def delete(self, backtest_id: str) -> None:
        try:
            client = await self._get_client()
            await client.delete(_key(backtest_id))
        except Exception:
            logger.exception("result_store.delete_failed", backtest_id=backtest_id)
        self._local_fallback.pop(backtest_id, None)

    async def evict_expired(self) -> None:
        now = time.monotonic()
        expired = [
            k
            for k, (ts, _uid, _data) in self._local_fallback.items()
            if now - ts > _RESULTS_TTL_SECONDS
        ]
        for k in expired:
            del self._local_fallback[k]

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


_store: BacktestResultStore | None = None


async def get_result_store() -> BacktestResultStore:
    global _store  # noqa: PLW0603
    if _store is None:
        _store = BacktestResultStore()
    return _store


def set_result_store(store: BacktestResultStore) -> None:
    global _store  # noqa: PLW0603
    _store = store
