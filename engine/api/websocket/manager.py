"""Connection manager — per-user WebSocket fan-out (gh#7 + SEV-275).

Process-local registry: ``{user_id: {websocket: conn_state}}`` where
``conn_state`` carries the connection's subscription set and a
monotonic outbound sequence counter. Broadcasts to a user happen by
walking that user's open connections and pushing the message in
parallel via ``asyncio.gather``.

SEV-275 enhancements over the original gh#7 implementation:

- Adds ``market_data`` channel.
- Wraps every outbound event in a :class:`WSMessage` envelope with a
  per-connection ``seq``, ``correlation_id``, and ``version``.
- Adds ``send_envelope`` for typed sends and keeps the legacy
  ``broadcast`` shape for backwards compatibility.
- Tracks per-connection last-seen timestamps for heartbeat/liveness.

Multi-replica deployments need the same broadcasts to fan out across
processes — that's a follow-up that wires this manager to a Redis
pubsub channel (``valkey``-backed) and consumes other replicas'
messages on a worker task.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import structlog

from engine.api.websocket.constants import VALID_CHANNELS, WS_VERSION
from engine.api.websocket.schemas import WSMessage, new_correlation_id

if TYPE_CHECKING:
    import uuid

    from fastapi import WebSocket


logger = structlog.get_logger()


class Topic(StrEnum):
    """Broadcast channels addressable by clients.

    Aliased as :class:`Channel` in :mod:`engine.api.websocket.constants`;
    kept under its original name for backwards compatibility with the
    pre-SEV-275 imports.
    """

    PORTFOLIO = "portfolio"
    BACKTEST = "backtest"
    ORDER = "order"
    ALERT = "alert"
    MARKET_DATA = "market_data"


# Pre-SEV-275 alias — code that imports ``VALID_TOPICS`` keeps working.
VALID_TOPICS: frozenset[str] = VALID_CHANNELS


@dataclass
class _ConnState:
    """Per-connection bookkeeping.

    Held under ``ConnectionManager._conns[user_id][ws]`` and mutated
    only while holding ``_lock``. The send itself happens outside the
    lock so a slow client never stalls the manager.
    """

    subscriptions: set[str] = field(default_factory=set)
    seq: int = 0
    last_seen: float = field(default_factory=time.monotonic)

    def next_seq(self) -> int:
        """Return the next monotonic sequence number for this conn."""
        current = self.seq
        self.seq = current + 1
        return current


class ConnectionManager:
    """Tracks open WebSocket connections per user + their channel subs."""

    def __init__(self) -> None:
        # {user_id: {ws: _ConnState}}
        self._conns: dict[uuid.UUID, dict[WebSocket, _ConnState]] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def attach(self, user_id: uuid.UUID, ws: WebSocket) -> None:
        async with self._lock:
            self._conns.setdefault(user_id, {})[ws] = _ConnState()
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

    def touch(self, user_id: uuid.UUID, ws: WebSocket) -> None:
        """Update the connection's last-seen timestamp (no lock — used
        on every inbound frame, contention would be punitive)."""
        user_conns = self._conns.get(user_id)
        if user_conns is None:
            return
        state = user_conns.get(ws)
        if state is None:
            return
        state.last_seen = time.monotonic()

    # ------------------------------------------------------------------
    # Subscriptions
    # ------------------------------------------------------------------

    async def subscribe(
        self, user_id: uuid.UUID, ws: WebSocket, channels: list[str]
    ) -> set[str]:
        """Add ``channels`` to this connection's subscription set.

        Unknown channels are silently dropped — the route handler is
        responsible for validating and reporting back if it wants to.
        Returns the resulting subscription set.
        """
        valid = {c for c in channels if c in VALID_CHANNELS}
        async with self._lock:
            user_conns = self._conns.get(user_id)
            if user_conns is None or ws not in user_conns:
                return set()
            user_conns[ws].subscriptions |= valid
            return set(user_conns[ws].subscriptions)

    async def unsubscribe(
        self, user_id: uuid.UUID, ws: WebSocket, channels: list[str]
    ) -> set[str]:
        async with self._lock:
            user_conns = self._conns.get(user_id)
            if user_conns is None or ws not in user_conns:
                return set()
            user_conns[ws].subscriptions -= set(channels)
            return set(user_conns[ws].subscriptions)

    # ------------------------------------------------------------------
    # Send paths
    # ------------------------------------------------------------------

    async def broadcast(
        self,
        *,
        user_id: uuid.UUID,
        topic: str,
        payload: dict[str, Any],
        event: str | None = None,
        correlation_id: str | None = None,
    ) -> int:
        """Push ``payload`` to every connection of ``user_id`` subscribed
        to ``topic`` (a.k.a. channel).

        Returns the number of recipients. Best-effort: a send that
        fails is logged and the connection is left for the route
        handler's normal disconnect path to clean up.

        ``payload`` is wrapped in a :class:`WSMessage` envelope. Pre-
        SEV-275 callers passed the raw EventBus dict here; that is
        still supported — it ends up nested under ``data``.
        """
        if topic not in VALID_CHANNELS:
            logger.warning("ws.broadcast_unknown_topic", topic=topic)
            return 0

        async with self._lock:
            user_conns = self._conns.get(user_id)
            if not user_conns:
                return 0
            # Snapshot (ws, seq) pairs under the lock so the seq is
            # monotonic even if a parallel broadcast races us.
            targets: list[tuple[WebSocket, int]] = []
            for ws, state in user_conns.items():
                if topic in state.subscriptions:
                    targets.append((ws, state.next_seq()))

        if not targets:
            return 0

        event_name = event or topic
        cid = correlation_id or new_correlation_id()

        async def _send(ws: WebSocket, seq: int) -> None:
            envelope = WSMessage(
                event=event_name,
                channel=topic,
                seq=seq,
                correlation_id=cid,
                version=WS_VERSION,
                data=payload,
            )
            with contextlib.suppress(Exception):
                await ws.send_json(envelope.model_dump(mode="json"))

        await asyncio.gather(*(_send(ws, seq) for ws, seq in targets))
        return len(targets)

    async def send_envelope(
        self,
        *,
        user_id: uuid.UUID,
        ws: WebSocket,
        event: str,
        channel: str,
        data: dict[str, Any],
        correlation_id: str | None = None,
    ) -> bool:
        """Send a typed envelope to a single (user, ws) pair.

        Returns True if the send was attempted (connection exists),
        False if the (user_id, ws) pair isn't registered. Used by the
        control-frame path for things like ``subscribed`` acks.
        """
        async with self._lock:
            user_conns = self._conns.get(user_id)
            if user_conns is None:
                return False
            state = user_conns.get(ws)
            if state is None:
                return False
            seq = state.next_seq()

        envelope = WSMessage(
            event=event,
            channel=channel,
            seq=seq,
            correlation_id=correlation_id or new_correlation_id(),
            version=WS_VERSION,
            data=data,
        )
        with contextlib.suppress(Exception):
            await ws.send_json(envelope.model_dump(mode="json"))
        return True

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def user_connection_count(self, user_id: uuid.UUID) -> int:
        return len(self._conns.get(user_id, {}))

    def total_connections(self) -> int:
        return sum(len(v) for v in self._conns.values())

    def subscriptions(
        self, user_id: uuid.UUID, ws: WebSocket
    ) -> frozenset[str]:
        """Return a *snapshot* of the channels this conn is subscribed to."""
        user_conns = self._conns.get(user_id)
        if user_conns is None:
            return frozenset()
        state = user_conns.get(ws)
        if state is None:
            return frozenset()
        return frozenset(state.subscriptions)


# Process-singleton accessor — keeps the manager outside the FastAPI
# DI graph so domain code (event listeners) can import it cheaply.
_MANAGER: ConnectionManager | None = None


def get_manager() -> ConnectionManager:
    global _MANAGER  # noqa: PLW0603 - process-wide singleton
    if _MANAGER is None:
        _MANAGER = ConnectionManager()
    return _MANAGER


def reset_manager() -> None:
    """Test helper — drop the process singleton so the next
    ``get_manager()`` returns a fresh instance."""
    global _MANAGER  # noqa: PLW0603
    _MANAGER = None


__all__ = [
    "VALID_CHANNELS",
    "VALID_TOPICS",
    "ConnectionManager",
    "Topic",
    "get_manager",
    "reset_manager",
]
