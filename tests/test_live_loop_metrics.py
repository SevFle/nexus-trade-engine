"""Tests for LiveLoop metrics emission (gh#109/#111 follow-up).

The loop emits four metrics through the active ``MetricsBackend``:

- ``oms.submit.attempted`` — counter, every ``submit`` call.
- ``oms.submit.outcome`` — counter, exactly once per submit, tagged
  with ``outcome ∈ {submitted, risk_rejected, broker_rejected,
  broker_auth_error, broker_connection_error}``.
- ``oms.event.applied`` — counter, every successful broker-event
  application; tagged with the post-event ``status``.
- ``oms.open_orders`` — gauge, set after every state change.
"""

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
from engine.core.live.loop import LiveLoop
from engine.core.oms import (
    AckEvent,
    FillEvent,
    Order,
    OrderEvent,
    OrderSide,
    OrderType,
)
from engine.core.oms.risk import KillSwitchCheck, MaxOrderQuantity, RiskGate
from engine.observability.metrics import RecordingBackend


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


def _counter_total(backend: RecordingBackend, name: str) -> float:
    return sum(v for (n, _t), v in backend.counters.items() if n == name)


def _counter_with(
    backend: RecordingBackend, name: str, tags: dict[str, str]
) -> float:
    expected = tuple(sorted(tags.items()))
    return sum(
        v
        for (n, t), v in backend.counters.items()
        if n == name and all(item in t for item in expected)
    )


def _gauge_value(backend: RecordingBackend, name: str) -> float | None:
    matches = [v for (n, _t), v in backend.gauges.items() if n == name]
    return matches[-1] if matches else None


@pytest.fixture
def metrics() -> RecordingBackend:
    return RecordingBackend()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestSubmitHappyPath:
    async def test_emits_attempted_and_submitted_outcome(self, metrics):
        ks = KillSwitch()
        broker = PaperBroker(price_for=lambda s: Decimal("100"))
        loop = LiveLoop(
            broker=broker, risk=_gate(kill_switch=ks), metrics=metrics
        )

        await loop.submit(_market_buy())

        assert _counter_total(metrics, "oms.submit.attempted") == 1
        assert (
            _counter_with(
                metrics,
                "oms.submit.outcome",
                {"outcome": "submitted", "symbol": "AAPL"},
            )
            == 1
        )
        # Open-order gauge moved to 1 after submit.
        assert _gauge_value(metrics, "oms.open_orders") == 1.0


# ---------------------------------------------------------------------------
# Submit rejection paths
# ---------------------------------------------------------------------------


class TestRiskRejected:
    async def test_kill_switch_engaged_emits_risk_rejected(self, metrics):
        ks = KillSwitch()
        ks.engage(reason="manual", actor="test")
        broker = PaperBroker(price_for=lambda s: Decimal("100"))
        loop = LiveLoop(
            broker=broker, risk=_gate(kill_switch=ks), metrics=metrics
        )

        await loop.submit(_market_buy())

        assert (
            _counter_with(
                metrics,
                "oms.submit.outcome",
                {"outcome": "risk_rejected"},
            )
            == 1
        )
        # Rejected order is terminal -> open_orders gauge stays at 0.
        assert _gauge_value(metrics, "oms.open_orders") == 0.0


class TestBrokerReject:
    async def test_broker_reject_emits_broker_rejected(self, metrics):
        ks = KillSwitch()
        loop = LiveLoop(
            broker=_RejectBroker(),
            risk=_gate(kill_switch=ks),
            metrics=metrics,
        )

        await loop.submit(_market_buy())

        assert (
            _counter_with(
                metrics,
                "oms.submit.outcome",
                {"outcome": "broker_rejected"},
            )
            == 1
        )
        assert not ks.is_engaged()


class TestBrokerAuth:
    async def test_auth_error_emits_outcome_then_engages_kill_switch(self, metrics):
        ks = KillSwitch()
        loop = LiveLoop(
            broker=_AuthFailBroker(),
            risk=_gate(kill_switch=ks),
            kill_switch=ks,
            metrics=metrics,
        )

        with pytest.raises(BrokerAuthError):
            await loop.submit(_market_buy())

        assert (
            _counter_with(
                metrics,
                "oms.submit.outcome",
                {"outcome": "broker_auth_error"},
            )
            == 1
        )
        assert ks.is_engaged()


class TestBrokerConnection:
    async def test_connection_error_emits_outcome_and_reraises(self, metrics):
        ks = KillSwitch()
        loop = LiveLoop(
            broker=_ConnectionBroker(),
            risk=_gate(kill_switch=ks),
            metrics=metrics,
        )

        with pytest.raises(BrokerConnectionError):
            await loop.submit(_market_buy())

        assert (
            _counter_with(
                metrics,
                "oms.submit.outcome",
                {"outcome": "broker_connection_error"},
            )
            == 1
        )


# ---------------------------------------------------------------------------
# Event application
# ---------------------------------------------------------------------------


class TestEventApplied:
    async def test_ack_then_fill_emits_event_applied_with_status(self, metrics):
        ks = KillSwitch()
        broker = PaperBroker(price_for=lambda s: Decimal("100"))
        loop = LiveLoop(
            broker=broker, risk=_gate(kill_switch=ks), metrics=metrics
        )

        order = await loop.submit(_market_buy())
        assert order.broker_order_id is not None

        await loop.apply_broker_event(
            AckEvent(
                occurred_at=datetime.now(tz=UTC),
                broker_order_id=order.broker_order_id,
            ),
            broker_order_id=order.broker_order_id,
        )
        await loop.apply_broker_event(
            FillEvent(
                occurred_at=datetime.now(tz=UTC),
                fill_quantity=order.quantity,
                fill_price=Decimal("100"),
            ),
            broker_order_id=order.broker_order_id,
        )

        assert _counter_total(metrics, "oms.event.applied") == 2
        assert (
            _counter_with(
                metrics,
                "oms.event.applied",
                {"event_type": "AckEvent", "status": "acknowledged"},
            )
            == 1
        )
        assert (
            _counter_with(
                metrics,
                "oms.event.applied",
                {"event_type": "FillEvent", "status": "filled"},
            )
            == 1
        )
        # Filled is terminal -> gauge back to 0.
        assert _gauge_value(metrics, "oms.open_orders") == 0.0


# ---------------------------------------------------------------------------
# Default backend resolution
# ---------------------------------------------------------------------------


class TestDefaultBackend:
    async def test_resolves_get_metrics_when_not_injected(self):
        from engine.observability.metrics import NullBackend, set_metrics

        recording = RecordingBackend()
        set_metrics(recording)
        try:
            ks = KillSwitch()
            broker = PaperBroker(price_for=lambda s: Decimal("100"))
            loop = LiveLoop(broker=broker, risk=_gate(kill_switch=ks))
            await loop.submit(_market_buy())
            assert _counter_total(recording, "oms.submit.attempted") == 1
        finally:
            set_metrics(NullBackend())
