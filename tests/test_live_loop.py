"""Unit tests for the live-loop driver (gh#109 follow-up)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from engine.core.brokers.base import (
    BrokerAuthError,
    BrokerConnectionError,
    BrokerRejectError,
    SubmittedOrder,
)
from engine.core.brokers.paper import PaperBroker
from engine.core.live.kill_switch import KillSwitch
from engine.core.live.loop import LiveLoop, UnknownOrderError
from engine.core.oms import (
    AckEvent,
    Order,
    OrderEvent,
    OrderSide,
    OrderStatus,
    OrderType,
)
from engine.core.oms.risk import KillSwitchCheck, MaxOrderQuantity, RiskGate


def _market_buy(qty: str = "10") -> Order:
    return Order(
        symbol="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal(qty),
    )


def _gate(*, kill_switch: KillSwitch, max_qty: Decimal = Decimal("1000")) -> RiskGate:
    return RiskGate(
        checks=[
            KillSwitchCheck(switch=kill_switch),
            MaxOrderQuantity(limit=max_qty),
        ]
    )


# ---------------------------------------------------------------------------
# Faked broker variants for error-path tests
# ---------------------------------------------------------------------------


class _AuthFailBroker:
    @property
    def name(self) -> str:
        return "auth-fail"

    async def submit(self, order: Order) -> SubmittedOrder:
        raise BrokerAuthError("invalid api key")

    async def cancel(self, *, order_id: uuid.UUID, broker_order_id: str) -> None:
        raise NotImplementedError

    async def events(self) -> AsyncIterator[OrderEvent]:  # type: ignore[override]
        return
        yield  # pragma: no cover


class _RejectBroker:
    @property
    def name(self) -> str:
        return "reject"

    async def submit(self, order: Order) -> SubmittedOrder:
        raise BrokerRejectError("insufficient buying power", broker_code="MARGIN")

    async def cancel(self, *, order_id: uuid.UUID, broker_order_id: str) -> None:
        raise NotImplementedError

    async def events(self) -> AsyncIterator[OrderEvent]:  # type: ignore[override]
        return
        yield  # pragma: no cover


class _ConnectionBroker:
    @property
    def name(self) -> str:
        return "no-conn"

    async def submit(self, order: Order) -> SubmittedOrder:
        raise BrokerConnectionError("dns failed")

    async def cancel(self, *, order_id: uuid.UUID, broker_order_id: str) -> None:
        raise NotImplementedError

    async def events(self) -> AsyncIterator[OrderEvent]:  # type: ignore[override]
        return
        yield  # pragma: no cover


# ---------------------------------------------------------------------------
# Submit happy path
# ---------------------------------------------------------------------------


class TestSubmitHappyPath:
    async def test_submit_via_paper_broker(self):
        ks = KillSwitch()
        broker = PaperBroker(price_for=lambda s: Decimal("100"))
        loop = LiveLoop(broker=broker, risk=_gate(kill_switch=ks))

        order = _market_buy()
        result = await loop.submit(order)
        assert result.status == OrderStatus.SUBMITTED
        assert result.broker_order_id is not None
        assert len(loop) == 1
        assert loop.get(order.id) is result

    async def test_persister_called_per_transition(self):
        ks = KillSwitch()
        broker = PaperBroker(price_for=lambda s: Decimal("100"))
        captured: list[Order] = []

        loop = LiveLoop(
            broker=broker,
            risk=_gate(kill_switch=ks),
            persister=captured.append,
        )
        await loop.submit(_market_buy())
        assert len(captured) == 1
        assert captured[0].status == OrderStatus.SUBMITTED


# ---------------------------------------------------------------------------
# Risk-gate rejection
# ---------------------------------------------------------------------------


class TestRiskRejection:
    async def test_kill_switch_blocks_submit(self):
        ks = KillSwitch()
        ks.engage(reason="manual_panic")
        broker = PaperBroker(price_for=lambda s: Decimal("100"))
        loop = LiveLoop(broker=broker, risk=_gate(kill_switch=ks))

        order = _market_buy()
        result = await loop.submit(order)
        assert result.status == OrderStatus.REJECTED
        assert "kill-switch engaged" in (result.reject_reason or "")
        # Broker should not have seen the order — its event queue is empty.
        events = await broker.drain_events()
        assert events == []

    async def test_max_qty_blocks_submit(self):
        ks = KillSwitch()
        broker = PaperBroker(price_for=lambda s: Decimal("100"))
        loop = LiveLoop(
            broker=broker,
            risk=_gate(kill_switch=ks, max_qty=Decimal("5")),
        )
        order = _market_buy(qty="10")
        result = await loop.submit(order)
        assert result.status == OrderStatus.REJECTED


# ---------------------------------------------------------------------------
# Broker-error policy
# ---------------------------------------------------------------------------


class TestBrokerErrors:
    async def test_auth_error_engages_kill_switch_and_reraises(self):
        ks = KillSwitch()
        loop = LiveLoop(
            broker=_AuthFailBroker(),
            risk=_gate(kill_switch=ks),
            kill_switch=ks,
        )
        with pytest.raises(BrokerAuthError):
            await loop.submit(_market_buy())
        assert ks.is_engaged()
        snap = ks.snapshot()
        assert snap.reason == "broker_auth_error"

    async def test_reject_error_marks_order_rejected_no_kill_switch(self):
        ks = KillSwitch()
        loop = LiveLoop(
            broker=_RejectBroker(),
            risk=_gate(kill_switch=ks),
            kill_switch=ks,
        )
        result = await loop.submit(_market_buy())
        assert result.status == OrderStatus.REJECTED
        assert "MARGIN" in (result.reject_reason or "")
        assert ks.is_engaged() is False

    async def test_connection_error_reraises_no_state_change(self):
        ks = KillSwitch()
        loop = LiveLoop(
            broker=_ConnectionBroker(),
            risk=_gate(kill_switch=ks),
            kill_switch=ks,
        )
        with pytest.raises(BrokerConnectionError):
            await loop.submit(_market_buy())
        # Connection error should NOT engage the kill-switch — it's
        # transient and the caller is expected to retry.
        assert ks.is_engaged() is False


# ---------------------------------------------------------------------------
# Event consumption
# ---------------------------------------------------------------------------


class TestApplyBrokerEvent:
    async def test_fill_event_completes_order(self):
        ks = KillSwitch()
        broker = PaperBroker(price_for=lambda s: Decimal("100"))
        loop = LiveLoop(broker=broker, risk=_gate(kill_switch=ks))

        order = _market_buy("10")
        submitted = await loop.submit(order)
        broker_id = submitted.broker_order_id
        # Drain the Ack and Fill that PaperBroker emitted.
        events = await broker.drain_events()
        ack, fill = events[0], events[1]

        # Apply Ack → ACKNOWLEDGED.
        ack_result = await loop.apply_broker_event(ack, broker_order_id=broker_id)
        assert ack_result.status == OrderStatus.ACKNOWLEDGED

        # Apply Fill → FILLED.
        fill_result = await loop.apply_broker_event(fill, broker_order_id=broker_id)
        assert fill_result.status == OrderStatus.FILLED
        assert fill_result.filled_quantity == Decimal("10")

    async def test_unknown_broker_id_raises(self):
        ks = KillSwitch()
        broker = PaperBroker(price_for=lambda s: Decimal("100"))
        loop = LiveLoop(broker=broker, risk=_gate(kill_switch=ks))

        # No submit happened — the registry is empty.
        with pytest.raises(UnknownOrderError):
            await loop.apply_broker_event(
                AckEvent(occurred_at=datetime.now(tz=UTC), broker_order_id="X"),
                broker_order_id="X",
            )


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------


class TestLookups:
    async def test_open_orders_excludes_terminals(self):
        ks = KillSwitch()
        broker = PaperBroker(price_for=lambda s: Decimal("100"))
        loop = LiveLoop(broker=broker, risk=_gate(kill_switch=ks))

        # Submit one and walk it to FILLED.
        order = _market_buy("10")
        result = await loop.submit(order)
        broker_id = result.broker_order_id
        events = await broker.drain_events()
        for ev in events:
            await loop.apply_broker_event(ev, broker_order_id=broker_id)

        # Submit a second and leave it pending submit (don't apply Ack).
        order2 = _market_buy("5")
        await loop.submit(order2)

        opens = loop.open_orders()
        assert len(opens) == 1
        assert opens[0].id == order2.id


# ---------------------------------------------------------------------------
# Persister failures don't break the loop
# ---------------------------------------------------------------------------


class TestPersisterRobustness:
    async def test_failing_persister_does_not_raise(self):
        ks = KillSwitch()
        broker = PaperBroker(price_for=lambda s: Decimal("100"))

        def angry(_order):
            raise RuntimeError("disk full")

        loop = LiveLoop(
            broker=broker,
            risk=_gate(kill_switch=ks),
            persister=angry,
        )
        # The submit must complete despite the persister raising.
        result = await loop.submit(_market_buy())
        assert result.status == OrderStatus.SUBMITTED
