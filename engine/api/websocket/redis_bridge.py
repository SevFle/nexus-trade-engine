"""Redis pub/sub → ConnectionManager bridge (SEV-275).

Subscribes to the engine's Redis / Valkey pub/sub channels and
dispatches deserialized frames to the appropriate
:class:`~engine.api.websocket.connection_manager_v2.ConnectionManagerV2`
-recipients. Survives Redis reconnects and routes unparseable
payloads to a dead-letter log without crashing.

Channel coverage
----------------
At startup the bridge subscribes to the patterns:

- ``portfolio:*``    — per-user portfolio events
- ``orders:*``       — per-user order events
- ``market:*``       — per-symbol market ticks (NOT ``market_depth:*``)
- ``market_depth:*`` — per-symbol depth

so a single pod receives every event regardless of which connection
is local. Cross-pod fan-out therefore "just works" — every pod
subscribes and each connection filters by its subscription registry.

Why pub/sub not Streams
-----------------------
The plan calls out Redis Streams with consumer-group ack for order
events as a future hardening. For the initial PR we keep the
behaviour contract of "live updates while connected" — clients
reconcile missed events via the REST order history endpoint on
reconnect. The bridge is structured so swapping the consumer for a
``XREADGROUP`` loop is a localised change (see ``_consume_pubsub``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import TYPE_CHECKING, Any

import structlog

from engine.api.websocket import ws_metrics as mx
from engine.api.websocket.channels import Channel, parse

if TYPE_CHECKING:
    from engine.api.websocket.connection_manager_v2 import ConnectionManagerV2

logger = structlog.get_logger()


# Patterns we subscribe to on Redis. ``psubscribe`` lets us match the
# full key-space under each family with one round-trip.
_SUBSCRIBE_PATTERNS: tuple[str, ...] = (
    "portfolio:*",
    "orders:*",
    "market:*",
    "market_depth:*",
)

_RECONNECT_BACKOFF_SECONDS = (0.5, 1.0, 2.0, 5.0, 10.0, 30.0)


class WSRedisBridge:
    """Background task that pumps Redis pub/sub into the ConnectionManager.

    Owns no state of its own beyond configuration and a reference to
    the manager. Reconnects on drop with exponential backoff (capped
    at 30s). Stopped cleanly by cancelling the run task — the next
    ``await`` in the consume loop raises ``CancelledError`` and the
    finally block unsubscribes.
    """

    def __init__(
        self,
        manager: ConnectionManagerV2,
        *,
        redis_url: str = "redis://localhost:6379/0",
        patterns: tuple[str, ...] = _SUBSCRIBE_PATTERNS,
    ) -> None:
        self._manager = manager
        self._redis_url = redis_url
        self._patterns = patterns
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._redis: Any = None
        self._pubsub: Any = None
        # Health snapshot fields read by the health endpoint.
        self.last_message_ts: float | None = None
        self.lag_seconds: float = 0.0
        self.messages_seen: int = 0
        self.errors: int = 0
        self.dead_letter: int = 0
        self.connected_since: float | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> asyncio.Task[None]:
        if self._task is not None and not self._task.done():
            return self._task
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="ws-redis-bridge")
        return self._task

    async def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self._task, timeout=timeout)
        except TimeoutError:
            self._task.cancel()
            with contextlib.suppress(Exception):
                await self._task
        finally:
            await self._close_pubsub()
            await self._close_redis()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    async def _run(self) -> None:
        backoff_idx = 0
        while not self._stop.is_set():
            try:
                await self._connect()
                backoff_idx = 0  # reset on a successful connection
                await self._consume_pubsub()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.errors += 1
                mx.bridge_error(reason=type(exc).__name__)
                logger.warning(
                    "ws_bridge.error",
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:200],
                )
            finally:
                await self._close_pubsub()
                await self._close_redis()

            if self._stop.is_set():
                break
            delay = _RECONNECT_BACKOFF_SECONDS[
                min(backoff_idx, len(_RECONNECT_BACKOFF_SECONDS) - 1)
            ]
            backoff_idx += 1
            logger.info("ws_bridge.reconnect", delay=delay)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except TimeoutError:
                pass

    async def _connect(self) -> None:
        # Import lazily so tests that don't have valkey/redis installed
        # can still import the bridge module.
        try:
            from valkey.asyncio import Valkey
        except ImportError:
            from redis.asyncio import Redis as Valkey  # type: ignore[no-redef]

        self._redis = Valkey.from_url(self._redis_url)
        await self._redis.ping()
        self.connected_since = time.monotonic()
        logger.info("ws_bridge.connected", url=self._redis_url, patterns=list(self._patterns))

    async def _close_redis(self) -> None:
        if self._redis is not None:
            with contextlib.suppress(Exception):
                await self._redis.aclose()
            self._redis = None
            self.connected_since = None

    async def _close_pubsub(self) -> None:
        if self._pubsub is not None:
            with contextlib.suppress(Exception):
                await self._pubsub.unsubscribe()
            with contextlib.suppress(Exception):
                await self._pubsub.punsubscribe(*self._patterns)
            with contextlib.suppress(Exception):
                await self._pubsub.close()
            self._pubsub = None

    async def _consume_pubsub(self) -> None:
        if self._redis is None:
            raise RuntimeError("redis_not_connected")
        self._pubsub = self._redis.pubsub()
        await self._pubsub.psubscribe(*self._patterns)
        async for message in self._pubsub.listen():
            if self._stop.is_set():
                break
            if not isinstance(message, dict):
                continue
            kind = message.get("type")
            if kind not in ("pmessage", "message"):
                continue
            await self._handle_message(message)

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------
    async def _handle_message(self, message: dict[str, Any]) -> None:
        channel_name = message.get("channel")
        raw_data = message.get("data")
        if isinstance(channel_name, bytes):
            channel_name = channel_name.decode("utf-8", errors="replace")
        if isinstance(raw_data, bytes):
            try:
                raw_data = raw_data.decode("utf-8")
            except UnicodeDecodeError:
                self.dead_letter += 1
                mx.dead_letter(reason="decode_error")
                return

        channel = parse(channel_name or "")
        if channel is None:
            # Not a channel we model — pubsub gossip from another subsystem.
            return

        try:
            payload = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
            if not isinstance(payload, dict):
                raise ValueError("payload must be a JSON object")
        except (ValueError, TypeError) as exc:
            self.dead_letter += 1
            mx.dead_letter(reason="parse_error")
            logger.warning(
                "ws_bridge.dead_letter",
                channel=channel_name,
                error_type=type(exc).__name__,
                error_message=str(exc)[:200],
            )
            return

        frame = self._to_frame(channel, payload)
        if frame is None:
            return

        # Compute lag if the source event carries a timestamp.
        lag = self._extract_lag(payload)
        if lag is not None:
            self.lag_seconds = lag
            mx.bridge_lag(lag)
        self.last_message_ts = time.monotonic()
        self.messages_seen += 1

        delivered = await self._manager.publish_to_channel(channel, frame)
        logger.debug(
            "ws_bridge.delivered",
            channel=channel_name,
            delivered=delivered,
        )

    # ------------------------------------------------------------------
    # Frame shaping
    # ------------------------------------------------------------------
    def _to_frame(self, channel: Channel, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Translate a raw EventBus payload into a wire frame.

        The engine's EventBus emits ``{"type": ..., "data": ..., "timestamp": ...}``.
        We re-shape that into the family-specific frames defined in
        :mod:`engine.api.websocket.schemas`. Payloads missing the
        expected keys are forwarded as ``data`` rather than rejected —
        the bridge is a pass-through, not a validator.
        """
        event_type = payload.get("type") or payload.get("event_type")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        ts = payload.get("timestamp")

        if channel.family == "portfolio":
            return {
                "type": "portfolio.updated",
                "v": "1.0",
                "channel": channel.name,
                "event_type": str(event_type) if event_type else "portfolio.updated",
                "timestamp": ts,
                "data": data,
            }
        if channel.family == "orders":
            return {
                "type": str(event_type) if event_type else "order.event",
                "v": "1.0",
                "channel": channel.name,
                "event_type": str(event_type) if event_type else "order.event",
                "timestamp": ts,
                "data": data,
            }
        if channel.family == "market_depth":
            return {
                "type": "market.depth",
                "v": "1.0",
                "channel": channel.name,
                "symbol": channel.key,
                "timestamp": ts,
                "data": data,
            }
        if channel.family == "market":
            return {
                "type": "market.tick",
                "v": "1.0",
                "channel": channel.name,
                "symbol": channel.key,
                "timestamp": ts,
                "data": data,
            }
        return None

    def _extract_lag(self, payload: dict[str, Any]) -> float | None:
        ts = payload.get("timestamp") or (
            payload.get("data") or {}
        ).get("timestamp")
        if not ts:
            return None
        try:
            from datetime import datetime

            if isinstance(ts, (int, float)):
                event_epoch = float(ts)
            else:
                # ISO-8601 with timezone; fall back to fromisoformat.
                event_epoch = datetime.fromisoformat(str(ts)).timestamp()
            return max(0.0, time.time() - event_epoch)
        except (ValueError, TypeError):
            return None

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        return {
            "patterns": list(self._patterns),
            "messages_seen": self.messages_seen,
            "errors": self.errors,
            "dead_letter": self.dead_letter,
            "lag_seconds": round(self.lag_seconds, 4) if self.lag_seconds else 0.0,
            "last_message_ts": self.last_message_ts,
            "connected": self._redis is not None,
        }


__all__ = ["WSRedisBridge"]
