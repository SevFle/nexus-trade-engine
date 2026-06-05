"""Per-connection subscription registry (SEV-275).

Each open WebSocket owns one :class:`SubscriptionRegistry` that
tracks which Redis / EventBus channels it is currently subscribed
to. The registry enforces:

- *idempotency* — subscribing twice to the same channel is a no-op
  and returns the existing subscription unchanged.
- *caps* — per-family limits on the number of distinct keys a single
  connection can hold. ``market`` is capped at
  :data:`~engine.api.websocket.constants.MAX_SYMBOL_SUBS_PER_CONNECTION`,
  ``portfolio`` and ``orders`` at 1 each (one user = one channel per
  family).
- *safe unsubscribe* — unsubscribing an unknown channel is a no-op
  rather than raising. Lets the route handler treat client input
  as advisory and keeps the protocol forgiving.

The registry is intentionally pure-Python and async-safe through a
single ``asyncio.Lock`` — it never reaches into Redis itself. The
:class:`~engine.api.websocket.redis_bridge.WSRedisBridge` consumes
the diff produced by ``diff_subscriptions`` and talks to Redis.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from engine.api.websocket.channels import Channel, ChannelFamily
from engine.api.websocket.constants import (
    MAX_SYMBOL_SUBS_PER_CONNECTION,
    MAX_USER_CHANNELS_PER_CONNECTION,
)
from engine.api.websocket.exceptions import SubscriptionLimitError


@dataclass
class SubscriptionState:
    """Snapshot returned by :meth:`SubscriptionRegistry.snapshot`."""

    portfolio: set[str] = field(default_factory=set)
    orders: set[str] = field(default_factory=set)
    market: set[str] = field(default_factory=set)
    market_depth: set[str] = field(default_factory=set)

    def total(self) -> int:
        return sum(len(getattr(self, f)) for f in ("portfolio", "orders", "market", "market_depth"))


class SubscriptionRegistry:
    """Tracks the channels a single connection is currently subscribed to.

    Internally keyed by family — each family gets its own set so the
    caps are independent (a client can hold 500 market symbols
    *and* its own portfolio channel without either limit being
    affected by the other).
    """

    _CAPS: dict[ChannelFamily, int] = {
        "portfolio": MAX_USER_CHANNELS_PER_CONNECTION,
        "orders": MAX_USER_CHANNELS_PER_CONNECTION,
        "market": MAX_SYMBOL_SUBS_PER_CONNECTION,
        "market_depth": MAX_SYMBOL_SUBS_PER_CONNECTION,
    }

    def __init__(self) -> None:
        self._subs: dict[ChannelFamily, set[str]] = {
            "portfolio": set(),
            "orders": set(),
            "market": set(),
            "market_depth": set(),
        }
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Subscribe / unsubscribe
    # ------------------------------------------------------------------
    async def subscribe(self, channel: Channel) -> bool:
        """Add ``channel``. Returns ``True`` if it was newly added.

        Raises :class:`SubscriptionLimitError` if adding it would
        breach the per-family cap.
        """
        async with self._lock:
            bucket = self._subs[channel.family]
            if channel.key in bucket:
                return False
            cap = self._CAPS[channel.family]
            if len(bucket) >= cap:
                raise SubscriptionLimitError(
                    reason=f"too_many_subscriptions:{channel.family}",
                )
            bucket.add(channel.key)
            return True

    async def unsubscribe(self, channel: Channel) -> bool:
        """Remove ``channel``. Returns ``True`` if it was present."""
        async with self._lock:
            bucket = self._subs[channel.family]
            if channel.key not in bucket:
                return False
            bucket.discard(channel.key)
            return True

    async def unsubscribe_family(self, family: ChannelFamily) -> set[str]:
        """Drop every subscription in ``family``. Returns the removed keys."""
        async with self._lock:
            removed = set(self._subs[family])
            self._subs[family].clear()
            return removed

    async def clear(self) -> None:
        """Drop every subscription. Called on disconnect."""
        async with self._lock:
            for bucket in self._subs.values():
                bucket.clear()

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    def is_subscribed(self, channel: Channel) -> bool:
        return channel.key in self._subs[channel.family]

    def channels(self) -> list[Channel]:
        out: list[Channel] = []
        for family, keys in self._subs.items():
            for key in keys:
                out.append(Channel(family=family, key=key))
        return out

    def channel_names(self) -> list[str]:
        return [c.name for c in self.channels()]

    def count(self, family: ChannelFamily) -> int:
        return len(self._subs[family])

    def total(self) -> int:
        return sum(len(b) for b in self._subs.values())

    def snapshot(self) -> SubscriptionState:
        return SubscriptionState(
            portfolio=set(self._subs["portfolio"]),
            orders=set(self._subs["orders"]),
            market=set(self._subs["market"]),
            market_depth=set(self._subs["market_depth"]),
        )


__all__ = ["SubscriptionRegistry", "SubscriptionState"]
