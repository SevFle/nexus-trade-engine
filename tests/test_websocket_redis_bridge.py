"""Unit tests for engine.api.websocket.redis_bridge (SEV-275).

Uses a fake pubsub to avoid spinning up a real Redis instance. The
goal is to verify the dispatch / dead-letter / reconnect paths, not
the underlying client behaviour.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from engine.api.websocket.channels import for_market, for_portfolio
from engine.api.websocket.connection_manager_v2 import ConnectionManagerV2
from engine.api.websocket.models import Principal
from engine.api.websocket.redis_bridge import WSRedisBridge


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _FakePubSub:
    """Async iterator yielding pre-queued messages."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.subscribed_patterns: list[str] = []
        self.closed = False

    async def psubscribe(self, *patterns: str) -> None:
        self.subscribed_patterns.extend(patterns)

    async def punsubscribe(self, *patterns: str) -> None:
        return None

    async def unsubscribe(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True

    def feed(self, message: dict[str, Any]) -> None:
        self._queue.put_nowait(message)

    def stop(self) -> None:
        self._queue.put_nowait(None)

    async def listen(self):
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item


class _FakeRedis:
    def __init__(self) -> None:
        self.pubsub_obj = _FakePubSub()
        self.ping_count = 0
        self.closed = False

    async def ping(self) -> None:
        self.ping_count += 1

    def pubsub(self) -> _FakePubSub:
        return self.pubsub_obj

    async def aclose(self) -> None:
        self.closed = True


class _Manager(ConnectionManagerV2):
    """Subclass that exposes the fake WS for test assertions."""



def _make_principal() -> Principal:
    return Principal(
        user_id=uuid.uuid4(),
        email="t@example.com",
        role="user",
        scopes=frozenset({"portfolio:read", "orders:read", "market:read"}),
    )


class _FakeWS:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.sent: list[dict] = []
        self.scope: dict = {}
        self.headers: dict = {}
        self.query_params: dict = {}

    async def accept(self, subprotocol: str | None = None) -> None: ...

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def close(self, code: int = 1000, reason: str | None = None) -> None: ...

    def __hash__(self) -> int:
        return hash(self.id)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _FakeWS) and other.id == self.id


# ---------------------------------------------------------------------------
# Dispatch tests (call _handle_message directly)
# ---------------------------------------------------------------------------
class TestDispatch:
    async def test_portfolio_event_routes_to_subscriber(self):
        manager = ConnectionManagerV2()
        bridge = WSRedisBridge(manager, redis_url="redis://localhost:0")
        ws = _FakeWS()
        p = _make_principal()
        conn = await manager.register(ws, p)
        await manager.subscribe(conn, for_portfolio(p.user_id))

        await bridge._handle_message(
            {
                "type": "pmessage",
                "channel": f"portfolio:{p.user_id}".encode(),
                "data": json.dumps(
                    {
                        "type": "portfolio.updated",
                        "data": {"user_id": str(p.user_id), "nav": 100},
                        "timestamp": "2026-01-01T00:00:00Z",
                    }
                ).encode(),
            }
        )
        assert conn.outbound.qsize() == 1
        frame = conn.outbound.get_nowait()
        assert frame["type"] == "portfolio.updated"
        assert frame["channel"] == f"portfolio:{p.user_id}"

    async def test_market_tick_routes_to_subscribers(self):
        manager = ConnectionManagerV2()
        bridge = WSRedisBridge(manager, redis_url="redis://localhost:0")
        ws_a, ws_b = _FakeWS(), _FakeWS()
        ca = await manager.register(ws_a, _make_principal())
        cb = await manager.register(ws_b, _make_principal())
        await manager.subscribe(ca, for_market("AAPL"))
        await manager.subscribe(cb, for_market("AAPL"))

        await bridge._handle_message(
            {
                "type": "pmessage",
                "channel": b"market:AAPL",
                "data": json.dumps({"type": "market.tick", "data": {"last": 123}}).encode(),
            }
        )
        assert ca.outbound.qsize() == 1
        assert cb.outbound.qsize() == 1

    async def test_dead_letter_unparseable_payload(self):
        manager = ConnectionManagerV2()
        bridge = WSRedisBridge(manager, redis_url="redis://localhost:0")
        await bridge._handle_message(
            {
                "type": "pmessage",
                "channel": b"market:AAPL",
                "data": b"not valid json",
            }
        )
        assert bridge.dead_letter == 1
        assert bridge.messages_seen == 0

    async def test_dead_letter_non_object_payload(self):
        manager = ConnectionManagerV2()
        bridge = WSRedisBridge(manager, redis_url="redis://localhost:0")
        await bridge._handle_message(
            {
                "type": "pmessage",
                "channel": b"market:AAPL",
                "data": json.dumps([1, 2, 3]).encode(),
            }
        )
        assert bridge.dead_letter == 1

    async def test_unknown_channel_ignored(self):
        manager = ConnectionManagerV2()
        bridge = WSRedisBridge(manager, redis_url="redis://localhost:0")
        await bridge._handle_message(
            {"type": "pmessage", "channel": b"unknown:foo", "data": b"{}"}
        )
        assert bridge.messages_seen == 0
        assert bridge.dead_letter == 0

    async def test_non_message_type_ignored(self):
        manager = ConnectionManagerV2()
        bridge = WSRedisBridge(manager, redis_url="redis://localhost:0")
        await bridge._handle_message(
            {"type": "psubscribe", "channel": b"market:AAPL", "data": b"1"}
        )
        assert bridge.messages_seen == 0


# ---------------------------------------------------------------------------
# Lag extraction
# ---------------------------------------------------------------------------
class TestLag:
    def test_iso_timestamp_yields_lag(self):
        from datetime import UTC, datetime

        manager = ConnectionManagerV2()
        bridge = WSRedisBridge(manager)
        ts = datetime.now(tz=UTC).isoformat()
        lag = bridge._extract_lag({"timestamp": ts})
        assert lag is not None and lag >= 0.0

    def test_numeric_timestamp_yields_lag(self):
        import time

        manager = ConnectionManagerV2()
        bridge = WSRedisBridge(manager)
        lag = bridge._extract_lag({"timestamp": time.time()})
        assert lag is not None and lag >= 0.0

    def test_missing_timestamp_returns_none(self):
        manager = ConnectionManagerV2()
        bridge = WSRedisBridge(manager)
        assert bridge._extract_lag({}) is None

    def test_garbage_timestamp_returns_none(self):
        manager = ConnectionManagerV2()
        bridge = WSRedisBridge(manager)
        assert bridge._extract_lag({"timestamp": "garbage"}) is None


# ---------------------------------------------------------------------------
# Lifecycle / snapshot
# ---------------------------------------------------------------------------
class TestSnapshot:
    def test_initial_snapshot(self):
        manager = ConnectionManagerV2()
        bridge = WSRedisBridge(manager)
        snap = bridge.snapshot()
        assert snap["messages_seen"] == 0
        assert snap["errors"] == 0
        assert snap["dead_letter"] == 0
        assert snap["connected"] is False


class TestReconnectLoop:
    async def test_run_gives_up_when_connection_fails(self, monkeypatch):
        """If Redis is unreachable, _run cycles through the backoff
        table and exits cleanly when stop() is called."""

        manager = ConnectionManagerV2()
        bridge = WSRedisBridge(manager, redis_url="redis://invalid:0")

        # Patch _connect to always fail fast.
        async def boom(self):
            raise OSError("nope")

        monkeypatch.setattr(WSRedisBridge, "_connect", boom)
        # Compress backoff so the test doesn't sleep for 30s.
        monkeypatch.setattr(
            "engine.api.websocket.redis_bridge._RECONNECT_BACKOFF_SECONDS",
            (0.01, 0.01, 0.01),
        )

        bridge.start()
        await asyncio.sleep(0.05)
        await bridge.stop(timeout=1.0)
        # Loop saw at least one error.
        assert bridge.errors >= 1
