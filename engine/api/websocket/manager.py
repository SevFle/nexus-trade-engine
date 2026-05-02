"""Connection manager — per-user WebSocket fan-out (gh#7).

Process-local registry: ``{user_id: {websocket: subscribed_topics}}``.
Broadcasts to a user happen by walking that user's open connections
and pushing the message in parallel via ``asyncio.gather``.

Multi-replica deployments need the same broadcasts to fan out across
processes — that's a follow-up that wires this manager to a Redis
pubsub channel (``valkey``-backed) and consumes other replicas'
messages on a worker task. This file exposes the shape that work
will consume; the wire-up is intentionally not in this PR.
"""

from __future__ import annotations

import asyncio
import contextlib
from enum import Enum
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    import uuid

    from fastapi import WebSocket


logger = structlog.get_logger()


class Topic(str, Enum):
    """Broadcast channels addressable by clients."""

    PORTFOLIO = "portfolio"
    BACKTEST = "backtest"
    ORDER = "order"
    ALERT = "alert"


VALID_TOPICS: frozenset[str] = frozenset(t.value for t in Topic)


class ConnectionManager:
    """Tracks open WebSocket connections per user + their topic subs."""

    def __init__(self) -> None:
        # {user_id: {ws: {topic, ...}}}
        self._conns: dict[uuid.UUID, dict[WebSocket, set[str]]] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def attach(self, user_id: uuid.UUID, ws: WebSocket) -> None:
        async with self._lock:
            self._conns.setdefault(user_id, {})[ws] = set()
        logger.info(
            "ws.attached",
            user_id=str(user_id),
            user_open_connections=self.user_connection_count(user_id),
        )

    async def detach(self, user_id: uuid.UUID, ws: WebSocket) -> None:
        async with self._lock:
            user_conns = self._conns.get(user_id)
            if user_conns is None:
                return
            user_conns.pop(ws, None)
            if not user_conns:
                self._conns.pop(user_id, None)
        logger.info(
            "ws.detached",
            user_id=str(user_id),
            user_open_connections=self.user_connection_count(user_id),
        )

    # ------------------------------------------------------------------
    # Subscriptions
    # ------------------------------------------------------------------

    async def subscribe(
        self, user_id: uuid.UUID, ws: WebSocket, topics: list[str]
    ) -> set[str]:
        """Add ``topics`` to this connection's subscription set.

        Unknown topics are silently dropped — the route handler is
        responsible for validating and reporting back if it wants to.
        Returns the resulting subscription set.
        """
        valid = {t for t in topics if t in VALID_TOPICS}
        async with self._lock:
            user_conns = self._conns.get(user_id)
            if user_conns is None or ws not in user_conns:
                return set()
            user_conns[ws] |= valid
            return set(user_conns[ws])

    async def unsubscribe(
        self, user_id: uuid.UUID, ws: WebSocket, topics: list[str]
    ) -> set[str]:
        async with self._lock:
            user_conns = self._conns.get(user_id)
            if user_conns is None or ws not in user_conns:
                return set()
            user_conns[ws] -= set(topics)
            return set(user_conns[ws])

    # ------------------------------------------------------------------
    # Broadcast
    # ------------------------------------------------------------------

    async def broadcast(
        self,
        *,
        user_id: uuid.UUID,
        topic: str,
        payload: dict[str, Any],
    ) -> int:
        """Push ``payload`` to every connection of ``user_id`` subscribed to ``topic``.

        Returns the number of recipients. Best-effort: a send that
        fails is logged and the connection is left for the route
        handler's normal disconnect path to clean up.
        """
        if topic not in VALID_TOPICS:
            logger.warning("ws.broadcast_unknown_topic", topic=topic)
            return 0
        message = {"topic": topic, "data": payload}

        async with self._lock:
            user_conns = self._conns.get(user_id)
            if not user_conns:
                return 0
            recipients = [ws for ws, topics in user_conns.items() if topic in topics]

        if not recipients:
            return 0

        async def _send(ws: WebSocket) -> None:
            with contextlib.suppress(Exception):
                await ws.send_json(message)

        await asyncio.gather(*(_send(ws) for ws in recipients))
        return len(recipients)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def user_connection_count(self, user_id: uuid.UUID) -> int:
        return len(self._conns.get(user_id, {}))

    def total_connections(self) -> int:
        return sum(len(v) for v in self._conns.values())


# Process-singleton accessor — keeps the manager outside the FastAPI
# DI graph so domain code (event listeners) can import it cheaply.
_MANAGER: ConnectionManager | None = None


def get_manager() -> ConnectionManager:
    global _MANAGER  # noqa: PLW0603 - process-wide singleton
    if _MANAGER is None:
        _MANAGER = ConnectionManager()
    return _MANAGER
