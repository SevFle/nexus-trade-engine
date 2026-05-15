"""Comprehensive structlog capture tests for LiveLoop, KillSwitch, and RiskGate.

Root cause of prior empty captures
-----------------------------------
structlog is configured in ``engine.observability.logging.setup_logging`` with
``structlog.stdlib.LoggerFactory()`` and ``cache_logger_on_first_use=True``.
Capturing at the *stdlib* layer (e.g. swapping ``handler.stream`` to a StringIO)
is fragile: the ``ProcessorFormatter.wrap_for_formatter`` processor must run
*before* the stdlib handler, and ``cache_logger_on_first_use`` can cache a
logger whose configuration predates the test's stream swap.

The fix: use ``structlog.testing.capture_logs()`` which intercepts events at
the structlog processor chain level, *before* they are routed to stdlib. This
works regardless of the LoggerFactory or cache settings.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import structlog
import structlog.testing

from engine.core.brokers.base import (
    BrokerAuthError,
    BrokerConnectionError,
    BrokerRejectError,
    SubmittedOrder,
)
from engine.core.brokers.paper import PaperBroker
from engine.core.live.kill_switch import KillSwitch, KillSwitchError
from engine.core.live.loop import LiveLoop, UnknownOrderError
from engine.core.oms import (
    AckEvent,
    CancelEvent,
    FillEvent,
    Order,
    OrderEvent,
    OrderSide,
    OrderStatus,
    OrderType,
    PartialFillEvent,
)
from engine.core.oms.events import ExpireEvent
from engine.core.oms.risk import (
    Approve,
    KillSwitchCheck,
    MaxOrderNotional,
    MaxOrderQuantity,
    Reject,
    RiskGate,
)
from engine.observability.metrics import NullBackend, RecordingBackend, set_metrics


def _market_buy(
    symbol: str = "AAPL",
    qty: str = "10",
    order_type: OrderType = OrderType.MARKET,
) -> Order:
    return Order(
        symbol=symbol,
        side=OrderSide.BUY,
        order_type=order_type,
        quantity=Decimal(qty),
    )


def _market_sell(symbol: str = "AAPL", qty: str = "10") -> Order:
    return Order(
        symbol=symbol,
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        quantity=Decimal(qty),
    )


def _gate(
    *,
    kill_switch: KillSwitch,
    max_qty: Decimal = Decimal("1000"),
    max_notional: Decimal | None = None,
) -> RiskGate:
    checks: list = [
        KillSwitchCheck(switch=kill_switch),
        MaxOrderQuantity(limit=max_qty),
    ]
    if max_notional is not None:
        checks.append(MaxOrderNotional(limit=max_notional))
    return RiskGate(checks=checks)


class _AuthFailBroker:
    @property
    def name(self) -> str:
        return "auth-fail"

    async def submit(self, order: Order) -> SubmittedOrder:
        raise BrokerAuthError("invalid api key")

    async def cancel(self, *, order_id: uuid.UUID, broker_order_id: str) -> None:
        raise NotImplementedError

    async def events(self) -> AsyncIterator[OrderEvent]:
        return
        yield


class _RejectBroker:
    @property
    def name(self) -> str:
        return "reject"

    async def submit(self, order: Order) -> SubmittedOrder:
        raise BrokerRejectError("insufficient buying power", broker_code="MARGIN")

    async def cancel(self, *, order_id: uuid.UUID, broker_order_id: str) -> None:
        raise NotImplementedError

    async def events(self) -> AsyncIterator[OrderEvent]:
        return
        yield


class _ConnectionBroker:
    @property
    def name(self) -> str:
        return "no-conn"

    async def submit(self, order: Order) -> SubmittedOrder:
        raise BrokerConnectionError("dns failed")

    async def cancel(self, *, order_id: uuid.UUID, broker_order_id: str) -> None:
        raise NotImplementedError

    async def events(self) -> AsyncIterator[OrderEvent]:
        return
        yield


@pytest.fixture(autouse=True)
def _reset_metrics_singleton():
    yield
    set_metrics(NullBackend())


def _find_log(events: list[dict], event_name: str) -> dict | None:
    for entry in events:
        if entry.get("event") == event_name:
            return entry
    return None


def _find_all_logs(events: list[dict], event_name: str) -> list[dict]:
    return [e for e in events if e.get("event") == event_name]


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


# ===================================================================
# LiveLoop: structlog capture tests
# ===================================================================


class TestLiveLoopBrokerConnectionLog:
    """Verify that ``live_loop.broker_connection_error`` is emitted with
    the correct structured fields when the broker raises
    ``BrokerConnectionError``."""

    async def test_connection_error_emits_structured_warning(self):
        with structlog.testing.capture_logs() as cap:
            ks = KillSwitch()
            loop = LiveLoop(
                broker=_ConnectionBroker(),
                risk=_gate(kill_switch=ks),
                kill_switch=ks,
            )
            with pytest.raises(BrokerConnectionError):
                await loop.submit(_market_buy())

        entry = _find_log(cap, "live_loop.broker_connection_error")
        assert entry is not None, (
            f"expected 'live_loop.broker_connection_error' log, "
            f"got events: {[e.get('event') for e in cap]}"
        )
        assert entry["log_level"] == "warning"
        assert "order_id" in entry
        assert entry["symbol"] == "AAPL"

    async def test_connection_error_log_contains_order_id(self):
        with structlog.testing.capture_logs() as cap:
            ks = KillSwitch()
            loop = LiveLoop(
                broker=_ConnectionBroker(),
                risk=_gate(kill_switch=ks),
                kill_switch=ks,
            )
            order = _market_buy()
            with pytest.raises(BrokerConnectionError):
                await loop.submit(order)

        entry = _find_log(cap, "live_loop.broker_connection_error")
        assert entry is not None
        assert entry["order_id"] == str(order.id)

    async def test_connection_error_no_log_for_other_errors(self):
        with structlog.testing.capture_logs() as cap:
            ks = KillSwitch()
            loop = LiveLoop(
                broker=_RejectBroker(),
                risk=_gate(kill_switch=ks),
                kill_switch=ks,
            )
            await loop.submit(_market_buy())

        assert _find_log(cap, "live_loop.broker_connection_error") is None


class TestLiveLoopPersisterFailureLog:
    """Verify that ``live_loop.persister_failed`` is emitted with
    error context when the persister callback raises."""

    async def test_failing_persister_emits_structured_warning(self):
        exc_msg = "disk full"

        def angry_persister(_order):
            raise RuntimeError(exc_msg)

        with structlog.testing.capture_logs() as cap:
            ks = KillSwitch()
            broker = PaperBroker(price_for=lambda s: Decimal("100"))
            loop = LiveLoop(
                broker=broker,
                risk=_gate(kill_switch=ks),
                persister=angry_persister,
            )
            result = await loop.submit(_market_buy())

        assert result.status == OrderStatus.SUBMITTED
        entry = _find_log(cap, "live_loop.persister_failed")
        assert entry is not None, (
            f"expected 'live_loop.persister_failed' log, "
            f"got events: {[e.get('event') for e in cap]}"
        )
        assert entry["log_level"] == "warning"
        assert entry["error_type"] == "RuntimeError"
        assert exc_msg in entry["error_message"]

    async def test_persister_failure_log_contains_order_id(self):
        order = _market_buy()

        def fail(_o):
            raise ValueError("bad")

        with structlog.testing.capture_logs() as cap:
            ks = KillSwitch()
            broker = PaperBroker(price_for=lambda s: Decimal("100"))
            loop = LiveLoop(
                broker=broker,
                risk=_gate(kill_switch=ks),
                persister=fail,
            )
            await loop.submit(order)

        entry = _find_log(cap, "live_loop.persister_failed")
        assert entry is not None
        assert entry["order_id"] == str(order.id)

    async def test_persister_error_message_truncated_at_200_chars(self):
        long_msg = "x" * 500

        def fail(_o):
            raise RuntimeError(long_msg)

        with structlog.testing.capture_logs() as cap:
            ks = KillSwitch()
            broker = PaperBroker(price_for=lambda s: Decimal("100"))
            loop = LiveLoop(
                broker=broker,
                risk=_gate(kill_switch=ks),
                persister=fail,
            )
            await loop.submit(_market_buy())

        entry = _find_log(cap, "live_loop.persister_failed")
        assert entry is not None
        assert len(entry["error_message"]) <= 200

    async def test_multiple_persister_failures_each_logged(self):
        call_count = 0

        def fail_on_first(_o):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("first failure")

        with structlog.testing.capture_logs() as cap:
            ks = KillSwitch()
            broker = PaperBroker(price_for=lambda s: Decimal("100"))
            loop = LiveLoop(
                broker=broker,
                risk=_gate(kill_switch=ks),
                persister=fail_on_first,
            )
            await loop.submit(_market_buy())
            await loop.submit(_market_buy())

        failures = _find_all_logs(cap, "live_loop.persister_failed")
        assert len(failures) == 1
        assert failures[0]["error_message"] == "first failure"

    async def test_no_persister_failure_log_when_no_persister(self):
        with structlog.testing.capture_logs() as cap:
            ks = KillSwitch()
            broker = PaperBroker(price_for=lambda s: Decimal("100"))
            loop = LiveLoop(
                broker=broker,
                risk=_gate(kill_switch=ks),
            )
            await loop.submit(_market_buy())

        assert _find_log(cap, "live_loop.persister_failed") is None

    async def test_no_persister_failure_log_when_persister_succeeds(self):
        saved: list[Order] = []

        with structlog.testing.capture_logs() as cap:
            ks = KillSwitch()
            broker = PaperBroker(price_for=lambda s: Decimal("100"))
            loop = LiveLoop(
                broker=broker,
                risk=_gate(kill_switch=ks),
                persister=saved.append,
            )
            await loop.submit(_market_buy())

        assert _find_log(cap, "live_loop.persister_failed") is None
        assert len(saved) == 1


class TestLiveLoopNoSpuriousLogs:
    """Verify that happy-path operations do not emit warning/error logs."""

    async def test_successful_submit_emits_no_structlog_warnings(self):
        with structlog.testing.capture_logs() as cap:
            ks = KillSwitch()
            broker = PaperBroker(price_for=lambda s: Decimal("100"))
            loop = LiveLoop(broker=broker, risk=_gate(kill_switch=ks))
            await loop.submit(_market_buy())

        warning_or_above = [
            e for e in cap if e.get("log_level") in ("warning", "error", "critical")
        ]
        assert warning_or_above == []

    async def test_risk_rejection_no_structlog_warnings_from_loop(self):
        with structlog.testing.capture_logs() as cap:
            ks = KillSwitch()
            ks.engage(reason="manual")
            broker = PaperBroker(price_for=lambda s: Decimal("100"))
            loop = LiveLoop(broker=broker, risk=_gate(kill_switch=ks))
            await loop.submit(_market_buy())

        loop_warnings = [
            e
            for e in cap
            if e.get("event", "").startswith("live_loop.")
            and e.get("log_level") in ("warning", "error")
        ]
        assert loop_warnings == []


# ===================================================================
# LiveLoop: metrics + logging integration
# ===================================================================


class TestLiveLoopMetricsLoggingIntegration:
    """Verify metrics and logging are emitted together on error paths."""

    async def test_connection_error_emits_both_metric_and_log(self):
        metrics = RecordingBackend()
        with structlog.testing.capture_logs() as cap:
            ks = KillSwitch()
            loop = LiveLoop(
                broker=_ConnectionBroker(),
                risk=_gate(kill_switch=ks),
                kill_switch=ks,
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
        assert _find_log(cap, "live_loop.broker_connection_error") is not None

    async def test_auth_error_emits_metric_and_engages_kill_switch(self):
        metrics = RecordingBackend()
        with structlog.testing.capture_logs():
            ks = KillSwitch()
            loop = LiveLoop(
                broker=_AuthFailBroker(),
                risk=_gate(kill_switch=ks),
                kill_switch=ks,
                metrics=metrics,
            )
            with pytest.raises(BrokerAuthError):
                await loop.submit(_market_buy())

        assert ks.is_engaged()
        assert (
            _counter_with(
                metrics,
                "oms.submit.outcome",
                {"outcome": "broker_auth_error"},
            )
            == 1
        )

    async def test_persister_failure_emits_both_metric_gauge_and_log(self):
        metrics = RecordingBackend()

        def fail(_o):
            raise RuntimeError("boom")

        with structlog.testing.capture_logs() as cap:
            ks = KillSwitch()
            broker = PaperBroker(price_for=lambda s: Decimal("100"))
            loop = LiveLoop(
                broker=broker,
                risk=_gate(kill_switch=ks),
                persister=fail,
                metrics=metrics,
            )
            await loop.submit(_market_buy())

        assert _counter_total(metrics, "oms.submit.attempted") == 1
        assert _gauge_value(metrics, "oms.open_orders") == 1.0
        assert _find_log(cap, "live_loop.persister_failed") is not None


# ===================================================================
# LiveLoop: event application edge cases
# ===================================================================


class TestLiveLoopEventEdgeCases:
    async def test_apply_cancel_event(self):
        ks = KillSwitch()
        broker = PaperBroker(price_for=lambda s: Decimal("100"))
        loop = LiveLoop(broker=broker, risk=_gate(kill_switch=ks))

        order = await loop.submit(_market_buy())
        broker_id = order.broker_order_id
        assert broker_id is not None

        updated = await loop.apply_broker_event(
            CancelEvent(occurred_at=datetime.now(tz=UTC), requested=False, reason="user"),
            broker_order_id=broker_id,
        )
        assert updated.status == OrderStatus.CANCELLED
        assert updated.is_terminal

    async def test_apply_partial_fill_then_final_fill(self):
        ks = KillSwitch()
        broker = PaperBroker(price_for=lambda s: Decimal("100"))
        loop = LiveLoop(broker=broker, risk=_gate(kill_switch=ks))

        order = await loop.submit(_market_buy(qty="100"))
        broker_id = order.broker_order_id
        assert broker_id is not None

        partial = await loop.apply_broker_event(
            PartialFillEvent(
                occurred_at=datetime.now(tz=UTC),
                fill_quantity=Decimal("60"),
                fill_price=Decimal("100"),
            ),
            broker_order_id=broker_id,
        )
        assert partial.status == OrderStatus.PARTIALLY_FILLED
        assert partial.filled_quantity == Decimal("60")
        assert not partial.is_terminal

        filled = await loop.apply_broker_event(
            FillEvent(
                occurred_at=datetime.now(tz=UTC),
                fill_quantity=Decimal("40"),
                fill_price=Decimal("101"),
            ),
            broker_order_id=broker_id,
        )
        assert filled.status == OrderStatus.FILLED
        assert filled.filled_quantity == Decimal("100")
        assert filled.is_terminal

    async def test_apply_expire_event(self):
        ks = KillSwitch()
        broker = PaperBroker(price_for=lambda s: Decimal("100"))
        loop = LiveLoop(broker=broker, risk=_gate(kill_switch=ks))

        order = await loop.submit(_market_buy())
        broker_id = order.broker_order_id

        expired = await loop.apply_broker_event(
            ExpireEvent(occurred_at=datetime.now(tz=UTC)),
            broker_order_id=broker_id,
        )
        assert expired.status == OrderStatus.EXPIRED
        assert expired.is_terminal

    async def test_unknown_broker_id_raises_unknown_order_error(self):
        ks = KillSwitch()
        broker = PaperBroker(price_for=lambda s: Decimal("100"))
        loop = LiveLoop(broker=broker, risk=_gate(kill_switch=ks))

        with pytest.raises(UnknownOrderError, match="no order tracked"):
            await loop.apply_broker_event(
                AckEvent(occurred_at=datetime.now(tz=UTC), broker_order_id="UNKNOWN"),
                broker_order_id="UNKNOWN",
            )

    async def test_multiple_orders_tracked_independently(self):
        ks = KillSwitch()
        broker = PaperBroker(price_for=lambda s: Decimal("100"))
        loop = LiveLoop(broker=broker, risk=_gate(kill_switch=ks))

        o1 = await loop.submit(_market_buy(symbol="AAPL"))
        o2 = await loop.submit(_market_buy(symbol="MSFT"))

        assert loop.get(o1.id) is not None
        assert loop.get(o2.id) is not None
        assert loop.get(o1.id) is not loop.get(o2.id)
        assert len(loop) == 2

    async def test_open_orders_after_mixed_states(self):
        ks = KillSwitch()
        broker = PaperBroker(price_for=lambda s: Decimal("100"))
        loop = LiveLoop(broker=broker, risk=_gate(kill_switch=ks))

        o1 = await loop.submit(_market_buy())
        events = await broker.drain_events()
        for ev in events:
            await loop.apply_broker_event(ev, broker_order_id=o1.broker_order_id)

        await loop.submit(_market_buy())
        await loop.submit(_market_buy())

        opens = loop.open_orders()
        assert len(opens) == 2
        assert all(not o.is_terminal for o in opens)

    async def test_get_returns_none_for_unknown_id(self):
        ks = KillSwitch()
        broker = PaperBroker(price_for=lambda s: Decimal("100"))
        loop = LiveLoop(broker=broker, risk=_gate(kill_switch=ks))

        assert loop.get(uuid.uuid4()) is None


class TestLiveLoopMetricsWithEvents:
    async def test_event_applied_counter_tracks_all_event_types(self):
        metrics = RecordingBackend()
        ks = KillSwitch()
        broker = PaperBroker(price_for=lambda s: Decimal("100"))
        loop = LiveLoop(
            broker=broker, risk=_gate(kill_switch=ks), metrics=metrics
        )

        order = await loop.submit(_market_buy())
        broker_id = order.broker_order_id

        await loop.apply_broker_event(
            AckEvent(occurred_at=datetime.now(tz=UTC), broker_order_id=broker_id),
            broker_order_id=broker_id,
        )
        await loop.apply_broker_event(
            FillEvent(
                occurred_at=datetime.now(tz=UTC),
                fill_quantity=order.quantity,
                fill_price=Decimal("100"),
            ),
            broker_order_id=broker_id,
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

    async def test_gauge_tracks_open_orders_across_lifecycle(self):
        metrics = RecordingBackend()
        ks = KillSwitch()
        broker = PaperBroker(price_for=lambda s: Decimal("100"))
        loop = LiveLoop(
            broker=broker, risk=_gate(kill_switch=ks), metrics=metrics
        )

        order = await loop.submit(_market_buy())
        assert _gauge_value(metrics, "oms.open_orders") == 1.0

        broker_id = order.broker_order_id
        events = await broker.drain_events()
        for ev in events:
            await loop.apply_broker_event(ev, broker_order_id=broker_id)

        assert _gauge_value(metrics, "oms.open_orders") == 0.0

    async def test_submit_side_tags_in_metrics(self):
        metrics = RecordingBackend()
        ks = KillSwitch()
        broker = PaperBroker(price_for=lambda s: Decimal("100"))
        loop = LiveLoop(
            broker=broker, risk=_gate(kill_switch=ks), metrics=metrics
        )

        await loop.submit(_market_buy())
        await loop.submit(_market_sell())

        assert (
            _counter_with(
                metrics,
                "oms.submit.attempted",
                {"symbol": "AAPL", "side": "buy"},
            )
            == 1
        )
        assert (
            _counter_with(
                metrics,
                "oms.submit.attempted",
                {"symbol": "AAPL", "side": "sell"},
            )
            == 1
        )


# ===================================================================
# KillSwitch: structlog capture tests
# ===================================================================


class TestKillSwitchEngageLog:
    async def test_engage_emits_error_log(self):
        with structlog.testing.capture_logs() as cap:
            ks = KillSwitch()
            ks.engage(reason="manual_panic", actor="operator")

        entry = _find_log(cap, "kill_switch.engaged")
        assert entry is not None, (
            f"expected 'kill_switch.engaged', "
            f"got: {[e.get('event') for e in cap]}"
        )
        assert entry["log_level"] == "error"
        assert entry["reason"] == "manual_panic"
        assert entry["actor"] == "operator"

    async def test_engage_noop_emits_warning_log(self):
        with structlog.testing.capture_logs() as cap:
            ks = KillSwitch()
            ks.engage(reason="first", actor="sys")
            ks.engage(reason="second", actor="sys")

        noop = _find_log(cap, "kill_switch.engage_noop")
        assert noop is not None
        assert noop["log_level"] == "warning"
        assert noop["new_reason"] == "second"
        assert noop["actor"] == "sys"

    def test_engage_requires_nonempty_reason(self):
        ks = KillSwitch()
        with pytest.raises(ValueError, match="non-empty reason"):
            ks.engage(reason="")

    def test_engage_idempotent_returns_false_on_second_call(self):
        ks = KillSwitch()
        assert ks.engage(reason="panic") is True
        assert ks.engage(reason="panic") is False


class TestKillSwitchDisengageLog:
    async def test_disengage_emits_warning_log(self):
        with structlog.testing.capture_logs() as cap:
            ks = KillSwitch()
            ks.engage(reason="panic")
            ks.disengage(confirmation="I_UNDERSTAND_THE_RISK", actor="admin")

        entry = _find_log(cap, "kill_switch.disengaged")
        assert entry is not None, (
            f"expected 'kill_switch.disengaged', "
            f"got: {[e.get('event') for e in cap]}"
        )
        assert entry["log_level"] == "warning"
        assert entry["actor"] == "admin"
        assert entry["prior_reason"] == "panic"

    def test_disengage_requires_confirmation_token(self):
        ks = KillSwitch()
        ks.engage(reason="panic")
        with pytest.raises(KillSwitchError, match="confirmation token"):
            ks.disengage(confirmation="WRONG")

    def test_disengage_noop_when_already_disengaged(self):
        ks = KillSwitch()
        assert (
            ks.disengage(confirmation="I_UNDERSTAND_THE_RISK") is False
        )


class TestKillSwitchObserverLog:
    def test_failing_observer_emits_warning_log(self):
        def bad_observer(_snap):
            raise RuntimeError("observer crashed")

        with structlog.testing.capture_logs() as cap:
            ks = KillSwitch()
            ks.add_observer(bad_observer)
            ks.engage(reason="panic")

        entry = _find_log(cap, "kill_switch.observer_failed")
        assert entry is not None, (
            f"expected 'kill_switch.observer_failed', "
            f"got: {[e.get('event') for e in cap]}"
        )
        assert entry["log_level"] == "warning"
        assert entry["error_type"] == "RuntimeError"

    def test_observer_failure_does_not_block_transition(self):
        def bad_observer(_snap):
            raise RuntimeError("boom")

        ks = KillSwitch()
        ks.add_observer(bad_observer)
        result = ks.engage(reason="panic")
        assert result is True
        assert ks.is_engaged()

    def test_multiple_observers_all_called_even_if_one_fails(self):
        calls: list[str] = []

        def good_observer(_snap):
            calls.append("good")

        def bad_observer(_snap):
            calls.append("bad")
            raise RuntimeError("fail")

        ks = KillSwitch()
        ks.add_observer(good_observer)
        ks.add_observer(bad_observer)
        ks.add_observer(lambda s: calls.append("also_good"))
        ks.engage(reason="panic")

        assert "good" in calls
        assert "bad" in calls
        assert "also_good" in calls


class TestKillSwitchMetricsLog:
    def test_engage_emits_metrics_and_log(self):
        metrics = RecordingBackend()
        with structlog.testing.capture_logs() as cap:
            ks = KillSwitch(metrics=metrics)
            ks.engage(reason="panic", actor="bot")

        assert (
            _counter_with(metrics, "kill_switch.engaged", {"actor": "bot"}) == 1
        )
        assert _gauge_value(metrics, "kill_switch.state") == 1.0
        assert _find_log(cap, "kill_switch.engaged") is not None

    def test_disengage_emits_metrics_and_log(self):
        metrics = RecordingBackend()
        with structlog.testing.capture_logs() as cap:
            ks = KillSwitch(metrics=metrics)
            ks.engage(reason="panic")
            ks.disengage(confirmation="I_UNDERSTAND_THE_RISK")

        assert _counter_total(metrics, "kill_switch.disengaged") == 1
        assert _gauge_value(metrics, "kill_switch.state") == 0.0
        assert _find_log(cap, "kill_switch.disengaged") is not None


# ===================================================================
# RiskGate: structlog capture tests
# ===================================================================


class TestRiskGateRejectLog:
    async def test_kill_switch_reject_emits_warning_log(self):
        ks = KillSwitch()
        ks.engage(reason="manual")
        gate = _gate(kill_switch=ks)

        with structlog.testing.capture_logs() as cap:
            result = gate.evaluate(_market_buy())

        assert isinstance(result, Reject)
        entry = _find_log(cap, "oms.risk_rejected")
        assert entry is not None, (
            f"expected 'oms.risk_rejected', "
            f"got: {[e.get('event') for e in cap]}"
        )
        assert entry["log_level"] == "warning"
        assert entry["check"] == "KillSwitchCheck"
        assert "kill-switch engaged" in entry["reason"]

    async def test_max_qty_reject_emits_warning_log(self):
        ks = KillSwitch()
        gate = _gate(kill_switch=ks, max_qty=Decimal("5"))

        with structlog.testing.capture_logs() as cap:
            result = gate.evaluate(_market_buy(qty="10"))

        assert isinstance(result, Reject)
        entry = _find_log(cap, "oms.risk_rejected")
        assert entry is not None
        assert entry["check"] == "MaxOrderQuantity"

    async def test_max_notional_reject_emits_warning_log(self):
        ks = KillSwitch()
        gate = _gate(kill_switch=ks, max_notional=Decimal("500"))

        with structlog.testing.capture_logs() as cap:
            result = gate.evaluate(
                _market_buy(qty="10"), reference_price=Decimal("100")
            )

        assert isinstance(result, Reject)
        entry = _find_log(cap, "oms.risk_rejected")
        assert entry is not None
        assert entry["check"] == "MaxOrderNotional"

    async def test_approve_emits_no_warning_log(self):
        ks = KillSwitch()
        gate = _gate(kill_switch=ks)

        with structlog.testing.capture_logs() as cap:
            result = gate.evaluate(_market_buy())

        assert isinstance(result, Approve)
        assert _find_log(cap, "oms.risk_rejected") is None

    async def test_reject_log_includes_order_id_and_symbol(self):
        ks = KillSwitch()
        ks.engage(reason="test")
        gate = _gate(kill_switch=ks)
        order = _market_buy(symbol="TSLA")

        with structlog.testing.capture_logs() as cap:
            gate.evaluate(order)

        entry = _find_log(cap, "oms.risk_rejected")
        assert entry is not None
        assert entry["symbol"] == "TSLA"
        assert entry["order_id"] == str(order.id)
        assert entry["quantity"] == str(order.quantity)


class TestRiskGateMetrics:
    async def test_rejected_check_emits_rejection_metric(self):
        metrics = RecordingBackend()
        ks = KillSwitch()
        ks.engage(reason="test")
        gate = RiskGate(
            checks=[KillSwitchCheck(switch=ks)],
            metrics=metrics,
        )

        gate.evaluate(_market_buy())

        assert (
            _counter_with(
                metrics,
                "oms.risk.check",
                {"check": "KillSwitchCheck", "outcome": "reject"},
            )
            == 1
        )
        assert _counter_total(metrics, "oms.risk.rejected") == 1

    async def test_approved_check_emits_approve_metric(self):
        metrics = RecordingBackend()
        ks = KillSwitch()
        gate = RiskGate(
            checks=[KillSwitchCheck(switch=ks), MaxOrderQuantity(limit=Decimal("1000"))],
            metrics=metrics,
        )

        gate.evaluate(_market_buy())

        assert (
            _counter_with(
                metrics,
                "oms.risk.check",
                {"check": "KillSwitchCheck", "outcome": "approve"},
            )
            == 1
        )
        assert _counter_total(metrics, "oms.risk.approved") == 1

    async def test_first_reject_short_circuits_remaining_checks(self):
        metrics = RecordingBackend()
        ks = KillSwitch()
        ks.engage(reason="test")
        gate = RiskGate(
            checks=[
                KillSwitchCheck(switch=ks),
                MaxOrderQuantity(limit=Decimal("1000")),
            ],
            metrics=metrics,
        )

        gate.evaluate(_market_buy())

        assert (
            _counter_with(
                metrics,
                "oms.risk.check",
                {"check": "MaxOrderQuantity", "outcome": "approve"},
            )
            == 0
        )


# ===================================================================
# End-to-end: full loop lifecycle with logging + metrics
# ===================================================================


class TestFullLifecycleWithLogging:
    async def test_submit_fill_lifecycle_logs_no_spurious_warnings(self):
        with structlog.testing.capture_logs() as cap:
            metrics = RecordingBackend()
            ks = KillSwitch()
            broker = PaperBroker(price_for=lambda s: Decimal("100"))
            loop = LiveLoop(
                broker=broker,
                risk=_gate(kill_switch=ks),
                metrics=metrics,
            )

            order = await loop.submit(_market_buy())
            broker_id = order.broker_order_id
            events = await broker.drain_events()
            for ev in events:
                await loop.apply_broker_event(ev, broker_order_id=broker_id)

        warnings = [
            e for e in cap if e.get("log_level") in ("warning", "error", "critical")
        ]
        assert warnings == []
        assert _counter_total(metrics, "oms.submit.attempted") == 1
        assert _counter_total(metrics, "oms.event.applied") >= 1

    async def test_risk_rejected_then_engage_kill_switch_flow(self):
        with structlog.testing.capture_logs() as cap:
            metrics = RecordingBackend()
            ks = KillSwitch()
            broker = PaperBroker(price_for=lambda s: Decimal("100"))
            loop = LiveLoop(
                broker=broker,
                risk=_gate(kill_switch=ks),
                kill_switch=ks,
                metrics=metrics,
            )

            o1 = await loop.submit(_market_buy())
            assert o1.status == OrderStatus.SUBMITTED

            ks.engage(reason="max_drawdown_exceeded")
            o2 = await loop.submit(_market_buy())
            assert o2.status == OrderStatus.REJECTED

        assert _find_log(cap, "kill_switch.engaged") is not None
        assert _find_log(cap, "oms.risk_rejected") is not None
        assert (
            _counter_with(
                metrics,
                "oms.submit.outcome",
                {"outcome": "submitted"},
            )
            == 1
        )
        assert (
            _counter_with(
                metrics,
                "oms.submit.outcome",
                {"outcome": "risk_rejected"},
            )
            == 1
        )

    async def test_broker_rejected_then_connection_error_flow(self):
        with structlog.testing.capture_logs() as cap:
            metrics = RecordingBackend()
            ks = KillSwitch()

            reject_loop = LiveLoop(
                broker=_RejectBroker(),
                risk=_gate(kill_switch=ks),
                kill_switch=ks,
                metrics=metrics,
            )
            r = await reject_loop.submit(_market_buy())
            assert r.status == OrderStatus.REJECTED
            assert not ks.is_engaged()

            conn_loop = LiveLoop(
                broker=_ConnectionBroker(),
                risk=_gate(kill_switch=ks),
                kill_switch=ks,
                metrics=metrics,
            )
            with pytest.raises(BrokerConnectionError):
                await conn_loop.submit(_market_buy())
            assert not ks.is_engaged()

        assert _find_log(cap, "live_loop.broker_connection_error") is not None
        assert _find_log(cap, "kill_switch.engaged") is None
