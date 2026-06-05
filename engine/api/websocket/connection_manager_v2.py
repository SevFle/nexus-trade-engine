"""Stateful ConnectionManager for the WebSocket API (SEV-275).

Layered over the existing :class:`~engine.api.websocket.manager.ConnectionManager`
from gh#7 (which still backs the legacy ``/api/v1/ws`` endpoint),
this new manager adds:

- *Per-connection* :class:`asyncio.Queue` with bounded capacity, so a
  slow client cannot pin an event loop indefinitely. Overflow
  triggers a slow-consumer disconnect after a configurable grace
  budget (close code 1008, ``policy_violation``).
- *Per-user* and *per-symbol* fan-out maps. User events reach every
  connection of the addressee user; market data reaches every
  connection subscribed to the symbol regardless of owner.
- *Drainable shutdown*. On graceful close the manager broadcasts a
  ``server_shutdown`` frame, waits up to ``drain_timeout`` for the
  outbound queues to flush, and then tears connections down.
- *Health snapshot*. Exposes connection count, subscription count,
  and bridge lag to the ``/health/websocket`` route.

The legacy :class:`~engine.api.websocket.manager.ConnectionManager`
remains in place for the older ``/api/v1/ws`` endpoint; this module
backs the new SEV-275 routes under ``/ws`` and ``/ws/{family}``.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from engine.api.websocket import ws_metrics as mx
from engine.api.websocket.channels import Channel
from engine.api.websocket.constants import (
    DRAIN_TIMEOUT_SECONDS,
    OUTBOUND_QUEUE_CAPACITY,
    SLOW_CONSUMER_GRACE_FRAMES,
    CloseCode,
)
from engine.api.websocket.models import Principal
from engine.api.websocket.subscriptions import SubscriptionRegistry

if TYPE_CHECKING:
    from fastapi import WebSocket

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Internal connection record
# ---------------------------------------------------------------------------
@dataclass
class _Connection:
    """Tracks the runtime state of a single authenticated connection.

    The outbound queue is consumed by a dedicated sender task; the
    receive loop runs in parallel and only mutates the subscription
    registry. Disconnect is centralised through
    :meth:`ConnectionManager.disconnect`.
    """

    id: str
    ws: WebSocket
    principal: Principal
    subscriptions: SubscriptionRegistry = field(default_factory=SubscriptionRegistry)
    outbound: asyncio.Queue[dict[str, Any] | None] = field(
        default_factory=lambda: asyncio.Queue(maxsize=OUTBOUND_QUEUE_CAPACITY)
    )
    sender_task: asyncio.Task[None] | None = None
    # Number of frames dropped while the queue was full. When this
    # crosses SLOW_CONSUMER_GRACE_FRAMES the connection is torn down.
    dropped: int = 0
    closed: bool = False
    close_code: int = CloseCode.GOING_AWAY

    @property
    def user_key(self) -> str:
        return str(self.principal.user_id)

    def can_buffer(self) -> bool:
        """Whether another frame can be queued without breaching the
        slow-consumer grace budget. Returns ``False`` once we've
        already dropped past the threshold — the manager will
        disconnect the connection."""
        return self.dropped < SLOW_CONSUMER_GRACE_FRAMES


# ---------------------------------------------------------------------------
# ConnectionManager
# ---------------------------------------------------------------------------
class ConnectionManagerV2:
    """Tracks open connections and routes outbound frames to them.

    Two indexes — by user and by symbol — make fan-out O(connections)
    rather than O(all connections). Subscription updates move keys
    between buckets atomically under ``_lock``.
    """

    def __init__(
        self,
        *,
        queue_capacity: int = OUTBOUND_QUEUE_CAPACITY,
        drain_timeout: float = DRAIN_TIMEOUT_SECONDS,
        slow_consumer_grace: int = SLOW_CONSUMER_GRACE_FRAMES,
    ) -> None:
        self._queue_capacity = queue_capacity
        self._drain_timeout = drain_timeout
        self._slow_consumer_grace = slow_consumer_grace

        # {conn_id: _Connection}
        self._conns: dict[str, _Connection] = {}
        # {user_id: {conn_id, ...}}
        self._by_user: dict[str, set[str]] = {}
        # {(family, key): {conn_id, ...}}
        self._by_channel: dict[tuple[str, str], set[str]] = {}

        self._lock = asyncio.Lock()
        self._shutting_down = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def register(
        self,
        ws: WebSocket,
        principal: Principal,
        *,
        connection_id: str | None = None,
    ) -> _Connection:
        """Bind a freshly authenticated WebSocket.

        Returns the internal ``_Connection`` so the route handler can
        pass it to subsequent subscribe / disconnect calls. The
        caller should *not* retain the WebSocket elsewhere — the
        manager owns the close path.
        """
        conn_id = connection_id or uuid.uuid4().hex
        conn = _Connection(
            id=conn_id,
            ws=ws,
            principal=principal,
            outbound=asyncio.Queue(maxsize=self._queue_capacity),
        )
        async with self._lock:
            if self._shutting_down:
                # Don't accept new connections during shutdown.
                conn.closed = True
                with contextlib.suppress(Exception):
                    await ws.close(code=CloseCode.GOING_AWAY, reason="shutdown")
                raise RuntimeError("shutting_down")
            self._conns[conn_id] = conn
            self._by_user.setdefault(conn.user_key, set()).add(conn_id)
        mx.set_connections(self.total_connections())
        logger.info(
            "ws_v2.registered",
            ws_conn=conn_id,
            user_id=conn.user_key,
            total=self.total_connections(),
        )
        return conn

    async def disconnect(self, conn: _Connection, *, code: int = CloseCode.GOING_AWAY) -> None:
        """Detach the connection and close the underlying socket.

        Idempotent — safe to call from both the receive loop and the
        sender task. The first invocation cancels the sender task and
        closes the socket; subsequent calls are no-ops.
        """
        if conn.closed:
            return
        conn.closed = True
        conn.close_code = code
        async with self._lock:
            self._conns.pop(conn.id, None)
            user_conns = self._by_user.get(conn.user_key)
            if user_conns is not None:
                user_conns.discard(conn.id)
                if not user_conns:
                    self._by_user.pop(conn.user_key, None)
            # Drop channel memberships.
            for ch in conn.subscriptions.channels():
                members = self._by_channel.get((ch.family, ch.key))
                if members is not None:
                    members.discard(conn.id)
                    if not members:
                        self._by_channel.pop((ch.family, ch.key), None)
            await conn.subscriptions.clear()
            mx.set_connections(self.total_connections())
            mx.set_subscriptions(self.total_subscriptions())
        # Cancel the sender task first so we don't race the close.
        if conn.sender_task is not None and not conn.sender_task.done():
            conn.sender_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await conn.sender_task
        # Drain the close handshake. We don't care if the peer is gone.
        with contextlib.suppress(Exception):
            await conn.ws.close(code=code, reason="disconnect")
        logger.info(
            "ws_v2.disconnected",
            ws_conn=conn.id,
            user_id=conn.user_key,
            code=code,
            total=self.total_connections(),
        )

    async def shutdown_all(self, *, reason: str = "shutdown") -> None:
        """Broadcast ``server_shutdown`` and drain every connection."""
        async with self._lock:
            self._shutting_down = True
            live = list(self._conns.values())
        if not live:
            return
        logger.info("ws_v2.shutdown_all", connections=len(live))
        # Broadcast the shutdown frame first so well-behaved clients
        # receive it before the close.
        for conn in live:
            await self._enqueue(
                conn,
                {"type": "server_shutdown", "reason": reason, "drain_seconds": self._drain_timeout},
            )
        # Wait for queues to drain, then close.
        try:
            await asyncio.wait_for(
                self._drain_queues(live),
                timeout=self._drain_timeout + 1.0,
            )
        except TimeoutError:
            logger.warning("ws_v2.shutdown_drain_timeout")
        for conn in live:
            await self.disconnect(conn, code=CloseCode.GOING_AWAY)

    async def _drain_queues(self, conns: list[_Connection]) -> None:
        """Wait until every connection's queue is empty."""
        while True:
            pending = [c for c in conns if not c.outbound.empty() and not c.closed]
            if not pending:
                return
            await asyncio.sleep(0.05)

    # ------------------------------------------------------------------
    # Sender task — pumps the outbound queue to the wire.
    # ------------------------------------------------------------------
    async def spawn_sender(self, conn: _Connection) -> None:
        """Start the per-connection sender loop.

        Pulled out of :meth:`register` so callers control *when* the
        sender starts — some tests want to register a connection
        without immediately consuming its queue.
        """
        if conn.sender_task is not None and not conn.sender_task.done():
            return conn.sender_task  # type: ignore[return-value]
        conn.sender_task = asyncio.create_task(
            self._sender_loop(conn), name=f"ws-sender-{conn.id}"
        )
        return conn.sender_task

    async def _sender_loop(self, conn: _Connection) -> None:
        try:
            while True:
                frame = await conn.outbound.get()
                if frame is None:
                    return  # sentinel — graceful drain
                if conn.closed:
                    return
                try:
                    await conn.ws.send_json(frame)
                    mx.message_sent(family=str(frame.get("type", "")))
                except Exception:
                    mx.message_dropped(reason="send_error")
                    # The receive loop will detect the broken socket
                    # and call disconnect; just bail out here.
                    return
        except asyncio.CancelledError:
            raise
        finally:
            # Trigger cleanup if the sender exits unexpectedly — but
            # only if we're not already mid-teardown. ``disconnect``
            # may have set ``closed=True`` itself; in that case
            # re-entering would race the sender task cancellation.
            if not conn.closed:
                asyncio.create_task(
                    self.disconnect(conn, code=CloseCode.INTERNAL_ERROR)
                )

    # ------------------------------------------------------------------
    # Subscriptions
    # ------------------------------------------------------------------
    async def subscribe(self, conn: _Connection, channel: Channel) -> bool:
        """Subscribe ``conn`` to ``channel``. Idempotent. Returns
        ``True`` if the subscription was newly added."""
        added = await conn.subscriptions.subscribe(channel)
        if not added:
            return False
        async with self._lock:
            self._by_channel.setdefault((channel.family, channel.key), set()).add(conn.id)
            mx.set_subscriptions(self.total_subscriptions())
        return True

    async def unsubscribe(self, conn: _Connection, channel: Channel) -> bool:
        removed = await conn.subscriptions.unsubscribe(channel)
        if not removed:
            return False
        async with self._lock:
            members = self._by_channel.get((channel.family, channel.key))
            if members is not None:
                members.discard(conn.id)
                if not members:
                    self._by_channel.pop((channel.family, channel.key), None)
            mx.set_subscriptions(self.total_subscriptions())
        return True

    # ------------------------------------------------------------------
    # Broadcast helpers
    # ------------------------------------------------------------------
    async def publish_to_channel(self, channel: Channel, frame: dict[str, Any]) -> int:
        """Push ``frame`` to every connection subscribed to ``channel``.

        Returns the number of recipients the frame was queued to. A
        full outbound queue counts as a dropped frame, not a
        recipient. Slow consumers are scheduled for disconnect after
        the grace budget is exhausted.
        """
        async with self._lock:
            members = list(self._by_channel.get((channel.family, channel.key), ()))
        if not members:
            return 0
        delivered = 0
        to_disconnect: list[_Connection] = []
        for conn_id in members:
            conn = self._conns.get(conn_id)
            if conn is None or conn.closed:
                continue
            if await self._enqueue(conn, frame):
                delivered += 1
            elif not conn.can_buffer():
                to_disconnect.append(conn)
        # Disconnect slow consumers outside the loop to avoid mutating
        # state we're iterating over.
        for conn in to_disconnect:
            asyncio.create_task(self.disconnect(conn, code=CloseCode.POLICY_VIOLATION))
        return delivered

    async def publish_to_user(
        self, user_id: uuid.UUID | str, channel: Channel, frame: dict[str, Any]
    ) -> int:
        """Push ``frame`` to every connection of ``user_id`` that is
        subscribed to ``channel``. Convenience wrapper — most call
        sites know the user but not the channel name."""
        key = str(user_id)
        async with self._lock:
            conn_ids = list(self._by_user.get(key, ()))
        delivered = 0
        for conn_id in conn_ids:
            conn = self._conns.get(conn_id)
            if conn is None or conn.closed:
                continue
            if not conn.subscriptions.is_subscribed(channel):
                continue
            if await self._enqueue(conn, frame):
                delivered += 1
        return delivered

    async def broadcast(self, frame: dict[str, Any]) -> int:
        """Push ``frame`` to *every* open connection. Used for
        ``server_shutdown`` and operator notices only."""
        async with self._lock:
            conns = list(self._conns.values())
        delivered = 0
        for conn in conns:
            if await self._enqueue(conn, frame):
                delivered += 1
        return delivered

    # ------------------------------------------------------------------
    # Introspection / health
    # ------------------------------------------------------------------
    def total_connections(self) -> int:
        return len(self._conns)

    def total_subscriptions(self) -> int:
        return sum(len(s) for s in self._by_channel.values())

    def user_connection_count(self, user_id: uuid.UUID | str) -> int:
        return len(self._by_user.get(str(user_id), ()))

    def snapshot(self) -> dict[str, Any]:
        return {
            "connections": self.total_connections(),
            "subscriptions": self.total_subscriptions(),
            "by_family": {
                family: sum(
                    1
                    for (f, _key), members in self._by_channel.items()
                    if f == family
                    for _ in members
                )
                for family in ("portfolio", "orders", "market", "market_depth")
            },
            "shutting_down": self._shutting_down,
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _enqueue(self, conn: _Connection, frame: dict[str, Any]) -> bool:
        """Try to enqueue ``frame``. Returns ``False`` if the queue is full.

        Implements the slow-consumer grace budget: the first
        ``slow_consumer_grace`` overflows still succeed (the warning
        frame is what we want the client to receive), after that we
        give up.
        """
        if conn.closed:
            return False
        try:
            conn.outbound.put_nowait(frame)
            return True
        except asyncio.QueueFull:
            conn.dropped += 1
            mx.message_dropped(reason="queue_full")
            if conn.dropped <= self._slow_consumer_grace:
                # Try to drop the *oldest* pending frame and replace
                # with the warning. Skip if we can't grab one fast.
                try:
                    conn.outbound.get_nowait()
                    conn.outbound.put_nowait(frame)
                    return True
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    return False
            return False


# ---------------------------------------------------------------------------
# Process-singleton accessor (parallel to the legacy manager).
# ---------------------------------------------------------------------------
_MANAGER_V2: ConnectionManagerV2 | None = None


def get_manager_v2() -> ConnectionManagerV2:
    global _MANAGER_V2  # noqa: PLW0603 - process-wide singleton
    if _MANAGER_V2 is None:
        _MANAGER_V2 = ConnectionManagerV2()
    return _MANAGER_V2


def set_manager_v2(manager: ConnectionManagerV2 | None) -> None:
    """Test helper: replace or clear the process singleton."""
    global _MANAGER_V2  # noqa: PLW0603
    _MANAGER_V2 = manager


__all__ = [
    "ConnectionManagerV2",
    "get_manager_v2",
    "set_manager_v2",
]
