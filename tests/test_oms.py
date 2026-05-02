"""Unit tests for the OMS state machine (gh#111)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from engine.core.oms import (
    VALID_TRANSITIONS,
    AckEvent,
    CancelEvent,
    FillEvent,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    PartialFillEvent,
    RejectEvent,
    SubmitEvent,
    is_terminal,
)
from engine.core.oms.events import ExpireEvent
from engine.core.oms.order import IllegalTransitionError, OverFillError
from engine.core.oms.states import can_transition


_T0 = datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC)


def _t(seconds: int) -> datetime:
    return _T0 + timedelta(seconds=seconds)


def _market_buy(qty: str = "10") -> Order:
    return Order(
        symbol="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal(qty),
    )


def _limit_buy(qty: str = "10", limit: str = "100") -> Order:
    return Order(
        symbol="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal(qty),
        limit_price=Decimal(limit),
    )


# ---------------------------------------------------------------------------
# Order construction
# ---------------------------------------------------------------------------


class TestOrderConstruction:
    def test_zero_quantity_rejected(self):
        with pytest.raises(ValueError):
            Order(symbol="AAPL", quantity=Decimal("0"), order_type=OrderType.MARKET)

    def test_limit_without_price_rejected(self):
        with pytest.raises(ValueError):
            Order(symbol="AAPL", quantity=Decimal("1"), order_type=OrderType.LIMIT)

    def test_stop_without_stop_price_rejected(self):
        with pytest.raises(ValueError):
            Order(symbol="AAPL", quantity=Decimal("1"), order_type=OrderType.STOP)

    def test_initial_status_is_new(self):
        o = _market_buy()
        assert o.status == OrderStatus.NEW
        assert o.filled_quantity == Decimal("0")
        assert o.is_terminal is False


# ---------------------------------------------------------------------------
# Transition table contract
# ---------------------------------------------------------------------------


class TestTransitionTable:
    def test_terminal_states_are_dead_ends(self):
        for terminal in (
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        ):
            assert is_terminal(terminal)
            assert VALID_TRANSITIONS[terminal] == frozenset()

    def test_all_states_present_in_table(self):
        for s in OrderStatus:
            assert s in VALID_TRANSITIONS

    def test_can_transition_helper(self):
        assert can_transition(OrderStatus.NEW, OrderStatus.SUBMITTED)
        assert not can_transition(OrderStatus.FILLED, OrderStatus.SUBMITTED)
        assert not can_transition(OrderStatus.CANCELLED, OrderStatus.NEW)


# ---------------------------------------------------------------------------
# Happy-path lifecycle
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_submit_ack_fully_fill(self):
        o = _market_buy("10")
        o = o.apply_event(SubmitEvent(occurred_at=_t(1), broker_order_id="B1"))
        assert o.status == OrderStatus.SUBMITTED
        assert o.broker_order_id == "B1"

        o = o.apply_event(AckEvent(occurred_at=_t(2), broker_order_id="B1"))
        assert o.status == OrderStatus.ACKNOWLEDGED

        o = o.apply_event(
            FillEvent(
                occurred_at=_t(3),
                fill_quantity=Decimal("10"),
                fill_price=Decimal("100"),
                fill_id="F1",
            )
        )
        assert o.status == OrderStatus.FILLED
        assert o.filled_quantity == Decimal("10")
        assert o.average_fill_price == Decimal("100")
        assert o.is_terminal

    def test_partial_then_full_fills(self):
        o = _market_buy("10")
        o = o.apply_event(SubmitEvent(occurred_at=_t(1)))
        o = o.apply_event(AckEvent(occurred_at=_t(2), broker_order_id="B1"))
        o = o.apply_event(
            PartialFillEvent(
                occurred_at=_t(3),
                fill_quantity=Decimal("4"),
                fill_price=Decimal("100"),
                fill_id="F1",
            )
        )
        assert o.status == OrderStatus.PARTIALLY_FILLED
        assert o.filled_quantity == Decimal("4")
        assert o.average_fill_price == Decimal("100")

        o = o.apply_event(
            PartialFillEvent(
                occurred_at=_t(4),
                fill_quantity=Decimal("6"),
                fill_price=Decimal("101"),
                fill_id="F2",
            )
        )
        assert o.status == OrderStatus.FILLED
        assert o.filled_quantity == Decimal("10")
        # VWAP: (4*100 + 6*101) / 10 = 100.6
        assert o.average_fill_price == Decimal("100.6")

    def test_partial_fill_at_full_quantity_marks_filled(self):
        o = _market_buy("10")
        o = o.apply_event(SubmitEvent(occurred_at=_t(1)))
        o = o.apply_event(AckEvent(occurred_at=_t(2), broker_order_id="B1"))
        o = o.apply_event(
            PartialFillEvent(
                occurred_at=_t(3),
                fill_quantity=Decimal("10"),
                fill_price=Decimal("100"),
                fill_id="F1",
            )
        )
        # Even though it came as a PartialFillEvent, the quantity reaches
        # the order total → status becomes FILLED.
        assert o.status == OrderStatus.FILLED


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


class TestCancel:
    def test_cancel_request_then_confirm(self):
        o = _limit_buy("10", "100")
        o = o.apply_event(SubmitEvent(occurred_at=_t(1)))
        o = o.apply_event(AckEvent(occurred_at=_t(2), broker_order_id="B1"))
        o = o.apply_event(CancelEvent(occurred_at=_t(3), requested=True))
        assert o.status == OrderStatus.CANCEL_REQUESTED
        o = o.apply_event(CancelEvent(occurred_at=_t(4), requested=False))
        assert o.status == OrderStatus.CANCELLED
        assert o.is_terminal

    def test_cancel_lost_race_to_fill(self):
        # Cancel requested, but broker filled before the cancel landed.
        o = _limit_buy("10", "100")
        o = o.apply_event(SubmitEvent(occurred_at=_t(1)))
        o = o.apply_event(AckEvent(occurred_at=_t(2), broker_order_id="B1"))
        o = o.apply_event(CancelEvent(occurred_at=_t(3), requested=True))
        assert o.status == OrderStatus.CANCEL_REQUESTED
        o = o.apply_event(
            FillEvent(
                occurred_at=_t(4),
                fill_quantity=Decimal("10"),
                fill_price=Decimal("100"),
                fill_id="F1",
            )
        )
        assert o.status == OrderStatus.FILLED


# ---------------------------------------------------------------------------
# Rejection / expiry
# ---------------------------------------------------------------------------


class TestRejectExpire:
    def test_reject_from_submitted(self):
        o = _market_buy()
        o = o.apply_event(SubmitEvent(occurred_at=_t(1)))
        o = o.apply_event(RejectEvent(occurred_at=_t(2), reason="insufficient_funds"))
        assert o.status == OrderStatus.REJECTED
        assert o.reject_reason == "insufficient_funds"
        assert o.is_terminal

    def test_expire_from_ack(self):
        o = _limit_buy()
        o = o.apply_event(SubmitEvent(occurred_at=_t(1)))
        o = o.apply_event(AckEvent(occurred_at=_t(2), broker_order_id="B1"))
        o = o.apply_event(ExpireEvent(occurred_at=_t(3)))
        assert o.status == OrderStatus.EXPIRED
        assert o.is_terminal


# ---------------------------------------------------------------------------
# Illegal transitions / over-fills
# ---------------------------------------------------------------------------


class TestErrors:
    def test_illegal_transition_raises(self):
        o = _market_buy()
        # NEW -> ACKNOWLEDGED is not allowed; must go through SUBMITTED.
        with pytest.raises(IllegalTransitionError):
            o.apply_event(AckEvent(occurred_at=_t(1), broker_order_id="B1"))

    def test_terminal_state_rejects_further_events(self):
        o = _market_buy()
        o = o.apply_event(SubmitEvent(occurred_at=_t(1)))
        o = o.apply_event(
            FillEvent(
                occurred_at=_t(2),
                fill_quantity=Decimal("10"),
                fill_price=Decimal("100"),
                fill_id="F1",
            )
        )
        with pytest.raises(IllegalTransitionError):
            o.apply_event(CancelEvent(occurred_at=_t(3)))

    def test_overfill_raises(self):
        o = _market_buy("10")
        o = o.apply_event(SubmitEvent(occurred_at=_t(1)))
        with pytest.raises(OverFillError):
            o.apply_event(
                PartialFillEvent(
                    occurred_at=_t(2),
                    fill_quantity=Decimal("11"),
                    fill_price=Decimal("100"),
                    fill_id="F1",
                )
            )

    def test_fill_event_must_complete_order(self):
        o = _market_buy("10")
        o = o.apply_event(SubmitEvent(occurred_at=_t(1)))
        # FillEvent for less than the full remaining is rejected — caller
        # should use PartialFillEvent.
        with pytest.raises(OverFillError):
            o.apply_event(
                FillEvent(
                    occurred_at=_t(2),
                    fill_quantity=Decimal("5"),
                    fill_price=Decimal("100"),
                    fill_id="F1",
                )
            )


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


class TestImmutability:
    def test_apply_event_returns_new_order(self):
        o1 = _market_buy()
        o2 = o1.apply_event(SubmitEvent(occurred_at=_t(1)))
        assert o1 is not o2
        assert o1.status == OrderStatus.NEW
        assert o2.status == OrderStatus.SUBMITTED

    def test_order_is_frozen(self):
        o = _market_buy()
        with pytest.raises(Exception):
            o.status = OrderStatus.SUBMITTED  # type: ignore[misc]
