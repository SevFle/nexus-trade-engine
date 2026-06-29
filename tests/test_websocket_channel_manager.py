"""Tests for the channel-based :class:`ConnectionManager` (SEV-298).

Covers the two hardening fixes tracked in this cycle:

1. ``_fanout`` snapshots the *actual* WebSocket handles under the asyncio
   lock so a reconnect mid-broadcast can't redirect a send to a
   replacement socket, and cleanup never evicts a freshly-reconnected
   healthy handle.
2. ``subscribe`` enforces a prefix-based owned-channel ACL (plus an
   optional ``channel_name_validator`` callback) so a caller can only
   join its own ``user:{id}`` channels.

It also pins the long-latent regression where ``_safe_send`` failed to
``return True`` on success — every successful delivery used to be
counted as a failure and the connection torn down.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import WebSocketDisconnect

from engine.api.websocket.manager import ConnectionManager


class _FakeWS:
    """Minimal WebSocket stand-in. Captures every send_json call.

    ``fail`` raises a generic ``RuntimeError``; ``disconnect`` raises
    ``WebSocketDisconnect`` to exercise the manager's dead-connection
    path. ``on_send`` is an optional hook invoked *before* the send so a
    test can simulate a reconnect happening mid-broadcast.
    """

    def __init__(
        self,
        *,
        fail: bool = False,
        disconnect: bool = False,
        on_send=None,
    ) -> None:
        self.sent: list[dict] = []
        self.fail = fail
        self.disconnect = disconnect
        self.on_send = on_send
        self.id = uuid.uuid4()

    async def send_json(self, payload: dict) -> None:
        if self.on_send is not None:
            self.on_send(self)
        if self.disconnect:
            raise WebSocketDisconnect
        if self.fail:
            raise RuntimeError("simulated send failure")
        self.sent.append(payload)

    def __hash__(self) -> int:
        return hash(self.id)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _FakeWS) and other.id == self.id


@pytest.fixture
def manager() -> ConnectionManager:
    return ConnectionManager()


# ---------------------------------------------------------------------------
# _fanout — snapshot the actual handle under the lock
# ---------------------------------------------------------------------------


class TestFanoutSnapshotsHandle:
    async def test_send_targets_original_handle_after_reconnect(self, manager):
        """A reconnect during fan-out must NOT redirect the in-flight send."""
        original = _FakeWS()
        replacement = _FakeWS()

        await manager.connect("conn-1", original, user_id="u1")
        await manager.subscribe("conn-1", "portfolio")

        # When the original handle is asked to send, a brand-new socket
        # reconnects under the same id — swapping the registry entry.
        def reconnect_mid_send(_ws):
            awaitable = manager.connect("conn-1", replacement, user_id="u1")
            # Schedule the reconnect synchronously by driving it to
            # completion inside the running loop.
            import asyncio

            asyncio.get_running_loop().create_task(awaitable)

        original.on_send = reconnect_mid_send

        delivered = await manager.broadcast("portfolio", {"v": 1})
        # Let the scheduled reconnect land.
        import asyncio

        await asyncio.sleep(0)

        assert delivered == 1
        # The ORIGINAL handle received the message...
        assert original.sent == [{"v": 1}]
        # ...the replacement did NOT, even though it now owns the id.
        assert replacement.sent == []

    async def test_reconnected_healthy_handle_not_evicted(self, manager):
        """A failed send whose handle was already replaced must not pull
        the fresh, healthy replacement down with it."""
        stale = _FakeWS(disconnect=True)  # send raises WebSocketDisconnect
        fresh = _FakeWS()

        await manager.connect("conn-1", stale, user_id="u1")
        await manager.subscribe("conn-1", "portfolio")

        # The stale socket's send triggers a reconnect before failing.
        def reconnect_then_fail(_ws):
            import asyncio

            asyncio.get_running_loop().create_task(
                manager.connect("conn-1", fresh, user_id="u1")
            )

        stale.on_send = reconnect_then_fail

        delivered = await manager.broadcast("portfolio", {"v": 2})
        import asyncio

        await asyncio.sleep(0)

        # The stale send failed, so nothing was delivered this round.
        assert delivered == 0
        # But the fresh handle is still registered — cleanup must not
        # evict it just because the stale handle (same id) failed.
        assert manager.is_connected("conn-1")
        assert manager.connections["conn-1"] is fresh

    async def test_successful_send_not_counted_as_failure(self, manager):
        """Regression: _safe_send must return True so healthy sockets
        are neither under-counted nor evicted after a successful send."""
        ws = _FakeWS()
        await manager.connect("conn-1", ws, user_id="u1")
        await manager.subscribe("conn-1", "portfolio")

        delivered = await manager.broadcast("portfolio", {"v": 3})

        assert delivered == 1
        assert ws.sent == [{"v": 3}]
        # The connection must survive a successful delivery.
        assert manager.is_connected("conn-1")
        assert manager.is_subscribed("conn-1", "portfolio")

    async def test_failed_send_evicts_only_that_connection(self, manager):
        good = _FakeWS()
        bad = _FakeWS(disconnect=True)
        await manager.connect("good", good)
        await manager.connect("bad", bad)
        await manager.subscribe("good", "alerts")
        await manager.subscribe("bad", "alerts")

        delivered = await manager.broadcast("alerts", {"v": 4})

        assert delivered == 1
        assert good.sent == [{"v": 4}]
        assert manager.is_connected("good")
        assert not manager.is_connected("bad")
        # Bad connection pruned from the channel membership too.
        assert manager.get_subscribers("alerts") == frozenset({"good"})


# ---------------------------------------------------------------------------
# broadcast / broadcast_all / send
# ---------------------------------------------------------------------------


class TestBroadcast:
    async def test_only_subscribed_recipients(self, manager):
        a, b = _FakeWS(), _FakeWS()
        await manager.connect("a", a)
        await manager.connect("b", b)
        await manager.subscribe("a", "portfolio")
        await manager.subscribe("b", "alerts")

        delivered = await manager.broadcast("portfolio", {"v": 1})

        assert delivered == 1
        assert a.sent == [{"v": 1}]
        assert b.sent == []

    async def test_no_subscribers_returns_zero(self, manager):
        assert await manager.broadcast("ghost", {"v": 1}) == 0

    async def test_broadcast_all_reaches_every_connection(self, manager):
        a, b = _FakeWS(), _FakeWS()
        await manager.connect("a", a)
        await manager.connect("b", b)

        delivered = await manager.broadcast_all({"v": 9})

        assert delivered == 2
        assert a.sent == [{"v": 9}]
        assert b.sent == [{"v": 9}]

    async def test_send_single_connection(self, manager):
        ws = _FakeWS()
        await manager.connect("x", ws)

        assert await manager.send("x", {"v": 5}) is True
        assert ws.sent == [{"v": 5}]

    async def test_send_unknown_connection(self, manager):
        assert await manager.send("ghost", {"v": 5}) is False


# ---------------------------------------------------------------------------
# subscribe — prefix-based owned-channel ACL + optional validator
# ---------------------------------------------------------------------------


class TestSubscribeAcl:
    async def test_owner_match_allows_owned_channel(self, manager):
        ws = _FakeWS()
        await manager.connect("c1", ws, user_id="42")

        assert await manager.subscribe("c1", "user:42") is True
        assert manager.is_subscribed("c1", "user:42")

        # Owned sub-channels under the same id are allowed too.
        assert await manager.subscribe("c1", "user:42:portfolio") is True
        assert manager.is_subscribed("c1", "user:42:portfolio")

    async def test_owner_mismatch_denies_owned_channel(self, manager):
        ws = _FakeWS()
        await manager.connect("c1", ws, user_id="42")

        assert await manager.subscribe("c1", "user:99") is False
        assert not manager.is_subscribed("c1", "user:99")

    async def test_anonymous_denied_owned_channel(self, manager):
        ws = _FakeWS()
        await manager.connect("c1", ws)  # no user_id

        assert await manager.subscribe("c1", "user:42") is False
        assert not manager.is_subscribed("c1", "user:42")

    async def test_bare_user_and_empty_id_denied(self, manager):
        ws = _FakeWS()
        await manager.connect("c1", ws, user_id="42")

        assert await manager.subscribe("c1", "user") is False
        assert await manager.subscribe("c1", "user:") is False
        assert not manager.is_subscribed("c1", "user")
        assert not manager.is_subscribed("c1", "user:")

    async def test_non_user_channels_allowed_regardless_of_owner(self, manager):
        anon = _FakeWS()
        owned = _FakeWS()
        await manager.connect("anon", anon)  # anonymous
        await manager.connect("owned", owned, user_id="1")

        assert await manager.subscribe("anon", "portfolio") is True
        assert await manager.subscribe("owned", "alerts") is True
        # 'users' (plural) is NOT an owned channel — must be allowed.
        assert await manager.subscribe("anon", "users:global") is True

    async def test_subscribe_unknown_connection_is_noop(self, manager):
        assert await manager.subscribe("ghost", "portfolio") is False


class TestChannelNameValidator:
    async def test_validator_can_deny_extra_channels(self):
        mgr = ConnectionManager(
            channel_name_validator=lambda cid, ch: ch.startswith("allow:")
        )
        ws = _FakeWS()
        await mgr.connect("c1", ws)

        assert await mgr.subscribe("c1", "allow:topic") is True
        assert await mgr.subscribe("c1", "deny:topic") is False
        assert mgr.is_subscribed("c1", "allow:topic")
        assert not mgr.is_subscribed("c1", "deny:topic")

    async def test_validator_cannot_widen_owned_channel_acl(self):
        # Validator always returns True, yet the built-in owned-channel
        # ACL must still reject a user:{id} channel for the wrong caller.
        mgr = ConnectionManager(channel_name_validator=lambda cid, ch: True)
        ws = _FakeWS()
        await mgr.connect("c1", ws, user_id="1")

        assert await mgr.subscribe("c1", "user:99") is False
        assert not mgr.is_subscribed("c1", "user:99")
        # The owner's own channel is still allowed.
        assert await mgr.subscribe("c1", "user:1") is True

    async def test_validator_raising_is_treated_as_denial(self):
        def boom(_cid, _ch):
            raise RuntimeError("validator exploded")

        mgr = ConnectionManager(channel_name_validator=boom)
        ws = _FakeWS()
        await mgr.connect("c1", ws)

        assert await mgr.subscribe("c1", "portfolio") is False
        assert not mgr.is_subscribed("c1", "portfolio")


# ---------------------------------------------------------------------------
# connect / disconnect — owner bookkeeping & reconnect semantics
# ---------------------------------------------------------------------------


class TestConnectDisconnect:
    async def test_connect_records_owner(self, manager):
        ws = _FakeWS()
        await manager.connect("c1", ws, user_id="u1")

        assert manager._connection_owners["c1"] == "u1"

    async def test_disconnect_clears_owner(self, manager):
        ws = _FakeWS()
        await manager.connect("c1", ws, user_id="u1")
        await manager.disconnect("c1")

        assert "c1" not in manager._connection_owners
        assert not manager.is_connected("c1")

    async def test_disconnect_unknown_is_noop(self, manager):
        await manager.disconnect("ghost")  # must not raise
        assert manager.connection_count == 0

    async def test_reconnect_clears_old_subscriptions(self, manager):
        old = _FakeWS()
        new = _FakeWS()
        await manager.connect("c1", old, user_id="u1")
        await manager.subscribe("c1", "portfolio")

        # Reconnect under the same id with a fresh socket.
        await manager.connect("c1", new, user_id="u1")

        # The new socket inherits no prior channel membership.
        assert not manager.is_subscribed("c1", "portfolio")
        assert await manager.broadcast("portfolio", {"v": 1}) == 0
        assert old.sent == []
        assert new.sent == []
