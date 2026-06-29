"""Broker-agnostic request/response DTOs + BrokerClient Protocol (gh#136).

These are the broker-neutral shapes concrete adapters (Alpaca, IBKR, …)
translate to and from. Keeping them in a shared module means the live-
trading loop and tests can talk about orders / positions / the market
clock without depending on any single broker's SDK.

Public surface:

- :class:`BrokerOrderRequest` — what the engine wants to send.
- :class:`BrokerOrderStatus` — the broker's view of that order.
- :class:`BrokerClock` — is the market open / when does it next change.
- :class:`BrokerPosition` — a held position (qty + cost basis).
- :class:`BrokerClient` — the Protocol concrete adapters implement.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Awaitable


def _to_decimal(value: object, default: Decimal = Decimal("0")) -> Decimal:
    """Best-effort decimal coercion that tolerates ``None`` / bad strings."""
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return default


def _parse_dt(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp; ``None`` if absent / unparseable."""
    if not value or not isinstance(value, str):
        return None
    # Alpaca emits a trailing ``Z``; ``fromisoformat`` (3.11+) handles it,
    # but normalise defensively so older / mixed shapes still parse.
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(frozen=True)
class BrokerOrderRequest:
    """Engine → broker order request.

    Mirrors the subset of Alpaca's ``/v2/orders`` body the engine uses.
    Concrete adapters translate this to their broker's native shape.
    """

    symbol: str
    side: str  # "buy" | "sell"
    order_type: str  # "market" | "limit" | "stop" | "stop_limit"
    qty: Decimal
    limit_price: Decimal | None = None
    time_in_force: str = "day"
    client_order_id: str | None = None

    def to_payload(self) -> dict[str, str]:
        """Serialise to the Alpaca ``/v2/orders`` JSON body shape."""
        payload: dict[str, str] = {
            "symbol": self.symbol,
            "side": self.side,
            "type": self.order_type,
            "qty": format(self.qty, "f"),
            "time_in_force": self.time_in_force,
        }
        if self.limit_price is not None:
            payload["limit_price"] = format(self.limit_price, "f")
        if self.client_order_id is not None:
            payload["client_order_id"] = self.client_order_id
        return payload


@dataclass(frozen=True)
class BrokerOrderStatus:
    """Broker → engine order status snapshot.

    Populated from a broker order response (or a GET order poll). The
    OMS uses ``status`` to decide whether to keep waiting, ``filled_qty``
    + ``filled_avg_price`` to record fills, and ``broker_order_id`` to
    correlate subsequent events.
    """

    broker_order_id: str
    status: str
    symbol: str
    qty: Decimal
    filled_qty: Decimal
    filled_avg_price: Decimal | None
    created_at: datetime | None = None

    @property
    def is_filled(self) -> bool:
        return self.status == "filled"

    @classmethod
    def from_response(cls, data: dict) -> BrokerOrderStatus:
        """Build a status from an Alpaca order JSON object.

        Defensive: tolerates missing keys (Alpaca omits ``filled_avg_price``
        until the first fill) and bad numeric strings.
        """
        return cls(
            broker_order_id=str(data.get("id", "")),
            status=str(data.get("status", "")),
            symbol=str(data.get("symbol", "")),
            qty=_to_decimal(data.get("qty")),
            filled_qty=_to_decimal(data.get("filled_qty")),
            filled_avg_price=_to_decimal_or_none(data.get("filled_avg_price")),
            created_at=_parse_dt(data.get("created_at")),
        )


def _to_decimal_or_none(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


@dataclass(frozen=True)
class BrokerClock:
    """Market clock snapshot."""

    is_open: bool
    timestamp: datetime | None
    next_open: datetime | None
    next_close: datetime | None

    @classmethod
    def from_response(cls, data: dict) -> BrokerClock:
        return cls(
            is_open=bool(data.get("is_open", False)),
            timestamp=_parse_dt(data.get("timestamp")),
            next_open=_parse_dt(data.get("next_open")),
            next_close=_parse_dt(data.get("next_close")),
        )


@dataclass(frozen=True)
class BrokerPosition:
    """A held position for one symbol."""

    symbol: str
    qty: Decimal
    side: str  # "long" | "short"
    avg_entry_price: Decimal
    market_value: Decimal
    unrealized_pl: Decimal

    @property
    def is_long(self) -> bool:
        return self.side == "long"

    @classmethod
    def from_response(cls, data: dict) -> BrokerPosition:
        return cls(
            symbol=str(data.get("symbol", "")),
            qty=_to_decimal(data.get("qty")),
            side=str(data.get("side", "long")),
            avg_entry_price=_to_decimal(data.get("avg_entry_price")),
            market_value=_to_decimal(data.get("market_value")),
            unrealized_pl=_to_decimal(data.get("unrealized_pl")),
        )


@runtime_checkable
class BrokerClient(Protocol):
    """Low-level per-broker client contract.

    Concrete adapters (e.g. :class:`~engine.core.brokers.alpaca.AlpacaTradingClient`)
    implement this; the higher-level :class:`~engine.core.brokers.base.BrokerAdapter`
    Protocol wraps a BrokerClient to feed the OMS state machine.
    """

    @property
    def name(self) -> str: ...

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def submit_order(
        self, request: BrokerOrderRequest
    ) -> Awaitable[BrokerOrderStatus]: ...

    async def get_order(
        self, broker_order_id: str
    ) -> Awaitable[BrokerOrderStatus]: ...

    async def cancel_order(self, broker_order_id: str) -> None: ...

    async def get_clock(self) -> Awaitable[BrokerClock]: ...

    async def get_position(
        self, symbol: str
    ) -> Awaitable[BrokerPosition]: ...


__all__ = [
    "BrokerClient",
    "BrokerClock",
    "BrokerOrderRequest",
    "BrokerOrderStatus",
    "BrokerPosition",
]
