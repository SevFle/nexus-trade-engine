"""WebSocket connection managers — pub/sub fan-out (gh#7, SEV-298).

This module hosts two process-local registries:

- :class:`ConnectionManager` — the primary, **channel-based pub/sub**
  manager (SEV-298). Tracks open WebSocket connections by string id and
  routes messages to subscribers of named channels. Supports
  ``connect``/``disconnect`` lifecycle, ``subscribe``/``unsubscribe``
  channel membership, ``broadcast`` to a channel and ``broadcast_all``
  to every connection. Sends run concurrently via :func:`asyncio.gather`
  and any connection whose send fails (e.g. ``WebSocketDisconnect``) is
  removed automatically along with its channel memberships.

- :class:`UserTopicManager` — the legacy per-user, topic-scoped registry
  (gh#7). ``{user_id: {websocket: {topic, ...}}}``. Broadcasts walk a
  user's open connections and push in parallel. Used by the authenticated
  ``/ws`` route handler (:mod:`engine.api.routes.websocket`) and the
  EventBus bridge (:mod:`engine.api.websocket.bridge`). Kept verbatim;
  renamed out of the way so the channel-based :class:`ConnectionManager`
  can own that name.

Multi-replica deployments need the same broadcasts to fan out across
processes — that's a follow-up that wires these managers to a Redis
pubsub channel (``valkey``-backed) consumed on a worker task. The
shapes exposed here are what that work will consume; the wire-up is
intentionally out of scope.
"""

from __future__ import annotations

import asyncio
import contextlib
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import WebSocketDisconnect

if TYPE_CHECKING:
    import uuid

    from fastapi import WebSocket


logger = structlog.get_logger()


class Topic(StrEnum):
    """Broadcast channels addressable by clients (gh#7 per-user topics)."""

    PORTFOLIO = "portfolio"
    BACKTEST = "backtest"
    ORDER = "order"
    ALERT = "alert"


VALID_TOPICS: frozenset[str] = frozenset(t.value for t in Topic)


# ---------------------------------------------------------------------------
# Channel-based pub/sub manager (SEV-298)
# ---------------------------------------------------------------------------


class ConnectionManager:
    """Channel-based pub/sub WebSocket connection manager.

    Maintains two registries:

    - :attr:`connections` — ``{connection_id: WebSocket}``
    - :attr:`channel_subscriptions` — ``{channel: {connection_id, ...}}``

    All mutating operations serialize on an :class:`asyncio.Lock` so they
    are safe to call concurrently from many route handlers / event
    listeners. Broadcasts snapshot the recipient set under the lock, then
    fan out the actual sends concurrently via :func:`asyncio.gather`
    *outside* the lock so one slow client can't stall the others.

    A send that raises (``WebSocketDisconnect`` or any other exception)
    is treated as a dead connection: it is detached and pruned from every
    channel it belonged to. Empty channels are removed to keep the
    registry tidy.
    """

    def __init__(self) -> None:
        self.connections: dict[str, WebSocket] = {}
        self.channel_subscriptions: dict[str, set[str]] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self, connection_id: str, ws: WebSocket) -> None:
        """Register an open WebSocket under ``connection_id``.

        Reconnecting with an id that already exists replaces the socket
        (the new socket inherits no prior channel membership — call
        :meth:`subscribe` again for the new connection). Stale
        subscriptions left over from a previous socket with the same id
        are cleared so messages are never routed to a replaced handle.
        """
        async with self._lock:
            self.connections[connection_id] = ws
            # Defensive: drop any orphan memberships left over from a
            # prior connection that reused this id without disconnecting.
            for members in self.channel_subscriptions.values():
                members.discard(connection_id)
            self._prune_empty_channels_locked()
        logger.info(
            "ws.connected",
            connection_id=connection_id,
            total_connections=len(self.connections),
        )

    async def disconnect(self, connection_id: str) -> None:
        """Detach a connection and remove it from every channel.

        Idempotent: disconnecting an unknown id is a no-op.
        """
        async with self._lock:
            removed = self.connections.pop(connection_id, None)
            for members in self.channel_subscriptions.values():
                members.discard(connection_id)
            self._prune_empty_channels_locked()
        if removed is not None:
            logger.info(
                "ws.disconnected",
                connection_id=connection_id,
                total_connections=len(self.connections),
            )

    # ------------------------------------------------------------------
    # Channel membership
    # ------------------------------------------------------------------

    async def subscribe(self, connection_id: str, channel: str) -> bool:
        """Add ``connection_id`` to ``channel``.

        Returns ``True`` if the connection exists and was subscribed
        (or was already a member), ``False`` if the connection is not
        registered — subscribing an unknown id is a no-op so we never
        accumulate orphan memberships that broadcasts would chase.
        """
        async with self._lock:
            if connection_id not in self.connections:
                return False
            self.channel_subscriptions.setdefault(channel, set()).add(connection_id)
            return True

    async def unsubscribe(self, connection_id: str, channel: str) -> bool:
        """Remove ``connection_id`` from ``channel``.

        Returns ``True`` if the connection was a member, ``False``
        otherwise. Empty channels are pruned.
        """
        async with self._lock:
            members = self.channel_subscriptions.get(channel)
            if members is None:
                return False
            existed = connection_id in members
            members.discard(connection_id)
            self._prune_empty_channels_locked()
            return existed

    # ------------------------------------------------------------------
    # Broadcast
    # ------------------------------------------------------------------

    async def broadcast(self, channel: str, message: Any) -> int:
        """Send ``message`` to every connection subscribed to ``channel``.

        Sends run concurrently via :func:`asyncio.gather`. Any connection
        whose send fails is auto-cleaned up (detached + pruned from all
        channels). Returns the number of *successful* deliveries.
        """
        async with self._lock:
            members = self.channel_subscriptions.get(channel)
            if not members:
                return 0
            recipients = list(members)

        sent = await self._fanout(recipients, message)
        logger.debug(
            "ws.broadcast",
            channel=channel,
            recipients=len(recipients),
            delivered=sent,
        )
        return sent

    async def broadcast_all(self, message: Any) -> int:
        """Send ``message`` to *every* open connection regardless of channel.

        Like :meth:`broadcast` this fans out concurrently and auto-cleans
        any connection that fails to receive. Returns the number of
        successful deliveries.
        """
        async with self._lock:
            recipients = list(self.connections)

        sent = await self._fanout(recipients, message)
        logger.debug(
            "ws.broadcast_all",
            recipients=len(recipients),
            delivered=sent,
        )
        return sent

    async def send(self, connection_id: str, message: Any) -> bool:
        """Send ``message`` to a single connection by id.

        Returns ``True`` on success, ``False`` if the connection is gone
        or the send failed (a failed send triggers cleanup just like a
        broadcast failure).
        """
        sent = await self._fanout([connection_id], message)
        return sent == 1

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _fanout(self, recipients: list[str], message: Any) -> int:
        """Concurrently deliver ``message`` to ``recipients`` and clean up failures.

        ``recipients`` is a snapshot taken under the lock; the actual
        sends (and the cleanup of any that fail) happen without holding
        the lock so a single unresponsive client can't block the rest.
        """
        if not recipients:
            return 0
        results = await asyncio.gather(
            *(self._safe_send(cid, message) for cid in recipients),
            return_exceptions=True,
        )

        failed: list[str] = []
        delivered = 0
        for cid, result in zip(recipients, results, strict=True):
            if result is True:
                delivered += 1
            else:
                failed.append(cid)

        if failed:
            await self._cleanup_failed(failed)
        return delivered

    async def _safe_send(self, connection_id: str, message: Any) -> bool:
        """Deliver one message; return ``True`` on success, ``False`` on failure.

        ``WebSocketDisconnect`` and any other send error are swallowed
        and reported as a failure so the caller can clean the dead
        connection up.
        """
        ws = self.connections.get(connection_id)
        if ws is None:
            return False
        try:
            await ws.send_json(message)
        except WebSocketDisconnect:
            logger.info(
                "ws.send_disconnected", connection_id=connection_id
            )
            return False
        except Exception as exc:
            logger.warning(
                "ws.send_failed",
                connection_id=connection_id,
                error_type=type(exc).__name__,
                error_message=str(exc)[:200],
            )
            return False

    async def _cleanup_failed(self, connection_ids: list[str]) -> None:
        """Detach every id whose send failed and prune their memberships."""
        if not connection_ids:
            return
        async with self._lock:
            for cid in connection_ids:
                self.connections.pop(cid, None)
                for members in self.channel_subscriptions.values():
                    members.discard(cid)
            self._prune_empty_channels_locked()

    def _prune_empty_channels_locked(self) -> None:
        """Drop channels with no members. Caller must hold the lock."""
        empty = [ch for ch, members in self.channel_subscriptions.items() if not members]
        for ch in empty:
            del self.channel_subscriptions[ch]

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def connection_count(self) -> int:
        return len(self.connections)

    @property
    def channel_count(self) -> int:
        return len(self.channel_subscriptions)

    def is_connected(self, connection_id: str) -> bool:
        return connection_id in self.connections

    def is_subscribed(self, connection_id: str, channel: str) -> bool:
        return connection_id in self.channel_subscriptions.get(channel, set())

    def get_subscribers(self, channel: str) -> frozenset[str]:
        return frozenset(self.channel_subscriptions.get(channel, set()))

    def get_subscriptions(self, connection_id: str) -> frozenset[str]:
        return frozenset(
            ch
            for ch, members in self.channel_subscriptions.items()
            if connection_id in members
        )


# ---------------------------------------------------------------------------
# Per-user, topic-scoped manager (gh#7 — legacy)
# ---------------------------------------------------------------------------


class UserTopicManager:
    """Tracks open WebSocket connections per user + their topic subs (gh#7).

    Process-local registry: ``{user_id: {websocket: subscribed_topics}}``.
    Broadcasts to a user walk that user's open connections and push the
    message in parallel via :func:`asyncio.gather`.
    """

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


# Process-singleton accessor for the per-user topic manager (gh#7) —
# keeps it outside the FastAPI DI graph so domain code (event listeners)
# can import it cheaply.
_MANAGER: UserTopicManager | None = None


def get_manager() -> UserTopicManager:
    global _MANAGER  # noqa: PLW0603 - process-wide singleton
    if _MANAGER is None:
        _MANAGER = UserTopicManager()
    return _MANAGER


__all__ = [
    "VALID_TOPICS",
    "ConnectionManager",
    "Topic",
    "UserTopicManager",
    "get_manager",
]
