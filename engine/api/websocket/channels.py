"""Deterministic channel naming for the WebSocket API (SEV-275).

Channel names are the join of *family* (portfolio / orders / market /
market_depth) and *key* (user UUID for per-user families, ticker
symbol for market data):

    portfolio:{user_id}
    orders:{user_id}
    market:{symbol}
    market_depth:{symbol}

Names are required to round-trip: ``parse(name)`` reconstructs the
``(family, key)`` tuple that produced it. This is critical because
the Redis bridge routes inbound pub/sub messages by parsing the
channel name off the wire — any drift between builder and parser
silently drops events.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Literal

from engine.api.websocket.constants import (
    CHANNEL_MARKET,
    CHANNEL_MARKET_DEPTH,
    CHANNEL_ORDERS,
    CHANNEL_PORTFOLIO,
)

ChannelFamily = Literal["portfolio", "orders", "market", "market_depth"]

_SYMBOL_RE = re.compile(r"^[A-Z0-9_.\-]{1,32}$")


@dataclass(frozen=True, slots=True)
class Channel:
    """Parsed channel descriptor."""

    family: ChannelFamily
    key: str  # user_id (str form) or symbol

    @property
    def name(self) -> str:
        return f"{self.family}:{self.key}"

    @property
    def is_user_scoped(self) -> bool:
        return self.family in ("portfolio", "orders")

    @property
    def is_symbol_scoped(self) -> bool:
        return self.family in ("market", "market_depth")

    def user_id(self) -> uuid.UUID | None:
        if not self.is_user_scoped:
            return None
        try:
            return uuid.UUID(self.key)
        except (ValueError, TypeError):
            return None


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def for_portfolio(user_id: uuid.UUID | str) -> Channel:
    return Channel(family="portfolio", key=str(user_id))


def for_orders(user_id: uuid.UUID | str) -> Channel:
    return Channel(family="orders", key=str(user_id))


def for_market(symbol: str) -> Channel:
    """Build a market-data channel for ``symbol``.

    Symbols must match ``^[A-Z0-9_.-]{1,32}$``. The strict regex
    blocks the most common Redis-channel-injection vectors
    (whitespace, ``*``, control chars, path traversal).
    """
    symbol = symbol.strip().upper()
    if not _SYMBOL_RE.match(symbol):
        raise ValueError(f"invalid market symbol: {symbol!r}")
    return Channel(family="market", key=symbol)


def for_market_depth(symbol: str) -> Channel:
    symbol = symbol.strip().upper()
    if not _SYMBOL_RE.match(symbol):
        raise ValueError(f"invalid market symbol: {symbol!r}")
    return Channel(family="market_depth", key=symbol)


# ---------------------------------------------------------------------------
# Parser (inverse of the builders)
# ---------------------------------------------------------------------------
_FAMILY_BY_PREFIX: dict[str, ChannelFamily] = {
    CHANNEL_PORTFOLIO: "portfolio",
    CHANNEL_ORDERS: "orders",
    CHANNEL_MARKET: "market",
    CHANNEL_MARKET_DEPTH: "market_depth",
}


def parse(name: str) -> Channel | None:
    """Inverse of the builders. Returns ``None`` on unrecognised input
    rather than raising — pub/sub traffic may include unrelated
    channels, and the bridge should silently drop those."""
    if not name or ":" not in name:
        return None
    family, _, key = name.partition(":")
    if not family or not key:
        return None
    if family not in _FAMILY_BY_PREFIX:
        return None
    return Channel(family=_FAMILY_BY_PREFIX[family], key=key)


__all__ = [
    "Channel",
    "ChannelFamily",
    "for_market",
    "for_market_depth",
    "for_orders",
    "for_portfolio",
    "parse",
]
