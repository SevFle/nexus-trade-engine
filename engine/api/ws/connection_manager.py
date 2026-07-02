"""Connection manager with room semantics (SEV-275).

Tracks WebSocket connections, manages room membership, and handles
message fan-out with bounded send queues and backpressure.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from fastapi import WebSocket

    from engine.api.ws.protocol import OutboundMessage

from engine.api.ws.exceptions import (
    ConnectionLimitError,
    QueueFullError,
    SubscriptionLimitError,
)
from engine.api.ws.metrics import ws_metrics
from engine.api.ws.protocol import AckMessage, CloseMessage

logger = structlog.get_logger()


@dataclass
class ConnectionInfo:
    websocket: WebSocket
    user_id: str
    scopes: list[str]
    rooms: set[str] = field(default_factory=set)
    send_queue: asyncio.Queue[OutboundMessage | None] = field(
        default_factory=lambda: asyncio.Queue(maxsize=256)
    )
    last_seen: float = field(default_factory=time.monotonic)
    connected_at: float = field(default_factory=time.monotonic)
    metadata: dict[str, Any] = field(default_factory=dict)
    sender_task: asyncio.Task[None] | None = field(default=None, repr=False)


class ConnectionManager:
    """Manages WebSocket connections with room-based fan-out."""

    def __init__(
        self,
        max_connections: int = 5000,
        send_queue_size: int = 256,
        max_subscriptions_per_connection: int = 50,
        heartbeat_interval: float = 30.0,
    ) -> None:
        self._connections: dict[str, ConnectionInfo] = {}
        self._rooms: dict[str, set[str]] = {}
        self._lock = asyncio.Lock()
        self._max_connections = max_connections
        self._send_queue_size = send_queue_size
        self._max_subscriptions = max_subscriptions_per_connection
        self._heartbeat_interval = heartbeat_interval
        self._seq_counters: dict[str, int] = {}
        self._global_heartbeat_task: asyncio.Task[None] | None = None
        self._shutting_down: bool = False

    async def register(
        self,
        ws: WebSocket,
        user_id: str,
        scopes: list[str],
        metadata: dict[str, Any] | None = None,
    ) -> str:
        connection_id = uuid.uuid4().hex
        async with self._lock:
            if self._shutting_down:
                raise ConnectionLimitError(code=1011, reason="server shutting down")
            if len(self._connections) >= self._max_connections:
                raise ConnectionLimitError(code=1011, reason="max connections reached")
            info = ConnectionInfo(
                websocket=ws,
                user_id=user_id,
                scopes=scopes,
                metadata=metadata or {},
                send_queue=asyncio.Queue(maxsize=self._send_queue_size),
            )
            self._connections[connection_id] = info
            user_room = f"user:{user_id}"
            self._rooms.setdefault(user_room, set()).add(connection_id)
            info.rooms.add(user_room)
            info.sender_task = asyncio.create_task(
                self._sender_loop(connection_id),
                name=f"ws-sender-{connection_id[:8]}",
            )
            if self._global_heartbeat_task is None or self._global_heartbeat_task.done():
                self._global_heartbeat_task = asyncio.create_task(
                    self._heartbeat_loop(), name="ws-global-heartbeat"
                )

        ws_metrics.metrics.counter("sev_ws_connections_total")
        ws_metrics.metrics.gauge("sev_ws_active_connections", len(self._connections))
        logger.info(
            "ws.registered",
            connection_id=connection_id[:8],
            user_id=user_id,
        )
        return connection_id

    async def unregister(self, connection_id: str, reason: str = "client_disconnect") -> None:
        async with self._lock:
            info = self._connections.pop(connection_id, None)
            if info is None:
                return
            for room in list(info.rooms):
                members = self._rooms.get(room)
                if members is not None:
                    members.discard(connection_id)
                    if not members:
                        del self._rooms[room]
            if info.sender_task is not None:
                _current = asyncio.current_task()
                if info.sender_task is not _current:
                    info.sender_task.cancel()
            with contextlib.suppress(Exception):
                info.send_queue.put_nowait(None)

        duration_ms = round((time.monotonic() - info.connected_at) * 1000)
        ws_metrics.metrics.gauge("sev_ws_active_connections", len(self._connections))
        if (
            not self._connections
            and self._global_heartbeat_task is not None
            and self._global_heartbeat_task is not asyncio.current_task()
        ):
            self._global_heartbeat_task.cancel()
            self._global_heartbeat_task = None
        logger.info(
            "ws.unregistered",
            connection_id=connection_id[:8],
            user_id=info.user_id,
            reason=reason,
            duration_ms=duration_ms,
            rooms_count=len(info.rooms),
        )

    # ------------------------------------------------------------------
    # Simple client-id-keyed facade (SEV-275 helper API)
    # ------------------------------------------------------------------
    # These methods wrap the room-based core to expose the conventional
    # connect / disconnect / send_personal / broadcast_all surface that
    # callers (and tests) can use without reasoning about rooms. They are
    # purely additive and share the same connection table, heartbeat task
    # and bounded send-queue backpressure as register/unregister/send.

    async def connect(
        self,
        ws: WebSocket,
        client_id: str | None = None,
        *,
        scopes: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Register a WebSocket and acknowledge the connection.

        Enqueues an :class:`AckMessage` so the client is notified it was
        accepted. Returns the opaque connection id, which also serves as
        the ``client_id`` for :meth:`send_personal` / :meth:`disconnect`.
        Raises :class:`ConnectionLimitError` when the server is full or
        shutting down (propagated from :meth:`register`).
        """
        connection_id = await self.register(
            ws,
            user_id=client_id or "",
            scopes=scopes or [],
            metadata=metadata,
        )
        await self.send(connection_id, AckMessage(status="ok"))
        return connection_id

    async def disconnect(self, client_id: str, reason: str = "client_disconnect") -> None:
        """Remove a client and clean up its rooms, sender task and metrics.

        Safe to call multiple times or with an unknown id (no-op).
        """
        await self.unregister(client_id, reason=reason)

    async def send_personal(self, client_id: str, message: OutboundMessage) -> bool:
        """Send a message to a single, specific client.

        Returns ``True`` when the client exists and the message was
        enqueued, ``False`` when the client is unknown (so callers can
        drop dead ids gracefully). May raise :class:`QueueFullError` when
        the per-connection send queue is saturated.
        """
        if client_id not in self._connections:
            return False
        await self.send(client_id, message)
        return True

    async def broadcast_all(self, message: OutboundMessage) -> int:
        """Fan a message out to *every* active connection.

        Returns the number of clients the message was delivered to
        (``0`` when there are no connections). Per-connection send errors
        (queue full / closed socket) are counted as dropped and do not
        abort the remaining fan-out.
        """
        snapshot = list(self._connections.keys())
        if not snapshot:
            return 0
        results = await asyncio.gather(
            *(self._send_one(cid, message) for cid in snapshot),
            return_exceptions=True,
        )
        delivered = 0
        for cid, result in zip(snapshot, results, strict=True):
            if isinstance(result, Exception):
                continue
            # _send_one swallows send failures; treat a still-present
            # connection whose send did not raise as delivered.
            if cid in self._connections:
                delivered += 1
        return delivered

    async def send(self, connection_id: str, message: OutboundMessage) -> None:
        info = self._connections.get(connection_id)
        if info is None:
            return
        try:
            info.send_queue.put_nowait(message)
        except asyncio.QueueFull:
            ws_metrics.metrics.counter(
                "sev_ws_messages_dropped_total", tags={"reason": "queue_full"}
            )
            raise QueueFullError(code=1008, reason="send queue full") from None

    async def broadcast(self, room: str, message: OutboundMessage) -> int:
        async with self._lock:
            members = self._rooms.get(room)
            if not members:
                return 0
            snapshot = frozenset(members)

        sent = 0
        results = await asyncio.gather(
            *(self._send_one(cid, message) for cid in snapshot),
            return_exceptions=True,
        )
        for r in results:
            if r is None:
                sent += 1
        return sent

    async def _send_one(self, connection_id: str, message: OutboundMessage) -> None:
        try:
            await self.send(connection_id, message)
        except QueueFullError:
            ws_metrics.metrics.counter(
                "sev_ws_messages_dropped_total", tags={"reason": "queue_full"}
            )
        except Exception:
            ws_metrics.metrics.counter(
                "sev_ws_messages_dropped_total", tags={"reason": "send_error"}
            )

    async def join_room(self, connection_id: str, room: str) -> None:
        async with self._lock:
            info = self._connections.get(connection_id)
            if info is None:
                return
            if len(info.rooms) >= self._max_subscriptions + 1:
                raise SubscriptionLimitError(code=1008, reason="max subscriptions reached")
            self._rooms.setdefault(room, set()).add(connection_id)
            info.rooms.add(room)
        ws_metrics.metrics.gauge(
            "sev_ws_subscriptions_active",
            sum(len(m) for m in self._rooms.values()),
        )

    async def leave_room(self, connection_id: str, room: str) -> None:
        async with self._lock:
            info = self._connections.get(connection_id)
            if info is None:
                return
            info.rooms.discard(room)
            members = self._rooms.get(room)
            if members is not None:
                members.discard(connection_id)
        ws_metrics.metrics.gauge(
            "sev_ws_subscriptions_active",
            sum(len(m) for m in self._rooms.values()),
        )

    def get_rooms(self, connection_id: str) -> frozenset[str]:
        info = self._connections.get(connection_id)
        if info is None:
            return frozenset()
        return frozenset(info.rooms)

    def get_connection(self, connection_id: str) -> ConnectionInfo | None:
        return self._connections.get(connection_id)

    def next_seq(self, room: str) -> int:
        seq = self._seq_counters.get(room, 0)
        self._seq_counters[room] = seq + 1
        return seq

    def touch(self, connection_id: str) -> None:
        info = self._connections.get(connection_id)
        if info is not None:
            info.last_seen = time.monotonic()

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    @property
    def room_count(self) -> int:
        return len(self._rooms)

    def room_members(self, room: str) -> frozenset[str]:
        return frozenset(self._rooms.get(room, set()))

    def stats(self) -> dict[str, Any]:
        queue_depths = sorted([info.send_queue.qsize() for info in self._connections.values()])
        return {
            "active_connections": len(self._connections),
            "total_rooms": len(self._rooms),
            "subscriptions_active": sum(len(m) for m in self._rooms.values()),
            "queue_depth_p50": queue_depths[len(queue_depths) // 2] if queue_depths else 0,
            "queue_depth_p95": queue_depths[int(len(queue_depths) * 0.95)] if queue_depths else 0,
            "queue_depth_p99": queue_depths[int(len(queue_depths) * 0.99)] if queue_depths else 0,
            "rooms": {room: len(members) for room, members in self._rooms.items()},
        }

    async def _heartbeat_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._heartbeat_interval)
                if not self._connections:
                    break
                now = time.monotonic()
                stale: list[str] = []
                for cid, info in list(self._connections.items()):
                    if now - info.last_seen > self._heartbeat_interval * 2:
                        stale.append(cid)
                for cid in stale:
                    try:
                        await self.unregister(cid, reason="heartbeat_timeout")
                    except Exception:
                        logger.exception(
                            "ws.heartbeat_unregister_failed",
                            connection_id=cid[:8],
                        )
        except asyncio.CancelledError:
            pass

    async def _sender_loop(self, connection_id: str) -> None:
        info = self._connections.get(connection_id)
        if info is None:
            return
        while True:
            msg = await info.send_queue.get()
            if msg is None:
                break
            try:
                await info.websocket.send_json(msg.model_dump(mode="json"))
                ws_metrics.metrics.counter("sev_ws_messages_sent_total")
            except asyncio.CancelledError:
                break
            except Exception:
                ws_metrics.metrics.counter(
                    "sev_ws_messages_dropped_total", tags={"reason": "closed"}
                )
                # Auto-cleanup: the underlying socket is gone, so drop the
                # connection so subsequent fan-out stops queueing to it and
                # the heartbeat/sender bookkeeping stays consistent.
                with contextlib.suppress(Exception):
                    await self.unregister(connection_id, reason="socket_closed")
                break

    async def close_all(self, code: int = 1000, reason: str = "server_shutdown") -> None:
        close_msg = CloseMessage(code=code, reason=reason)
        async with self._lock:
            self._shutting_down = True
            snapshot = list(self._connections.keys())
        for cid in snapshot:
            info = self._connections.get(cid)
            if info is None:
                continue
            with contextlib.suppress(QueueFullError):
                await self.send(cid, close_msg)
            await asyncio.sleep(0.1)
            with contextlib.suppress(Exception):
                await info.websocket.close(code=code, reason=reason)
            with contextlib.suppress(Exception):
                await self.unregister(cid, reason="server_shutdown")
        async with self._lock:
            self._shutting_down = False
