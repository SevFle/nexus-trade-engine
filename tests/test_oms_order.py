"""Tests for engine.core.oms.order — Order entity state machine (gh#111)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from engine.core.oms.events import (
    AckEvent,
    CancelEvent,
    ExpireEvent,
    FillEvent,
    PartialFillEvent,
    RejectEvent,
    SubmitEvent,
)
from engine.core.oms.order import IllegalTransitionError, Order, OverFillError
from engine.core.oms.states import OrderSide, OrderStatus, OrderType


def _ts() -> datetime:
    return datetime(2025, 1, 1, tzinfo=UTC)


def _order(**overrides) -> Order:
    defaults = dict(
        symbol="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("100"),
    )
    defaults.update(overrides)
    return Order(**defaults)


class TestOrderPostInit:
    def test_zero_quantity_raises(self):
        with pytest.raises(ValueError, match="positive"):
            _order(quantity=Decimal("0"))

    def test_negative_quantity_raises(self):
        with pytest.raises(ValueError, match="positive"):
            _order(quantity=Decimal("-1"))

    def test_limit_without_price_raises(self):
        with pytest.raises(ValueError, match="limit_price"):
            _order(order_type=OrderType.LIMIT, limit_price=None)

    def test_limit_with_zero_price_raises(self):
        with pytest.raises(ValueError, match="limit_price"):
            _order(order_type=OrderType.LIMIT, limit_price=Decimal("0"))

    def test_limit_with_valid_price(self):
        o = _order(order_type=OrderType.LIMIT, limit_price=Decimal("150"))
        assert o.limit_price == Decimal("150")

    def test_stop_without_price_raises(self):
        with pytest.raises(ValueError, match="stop_price"):
            _order(order_type=OrderType.STOP, stop_price=None)

    def test_stop_with_zero_price_raises(self):
        with pytest.raises(ValueError, match="stop_price"):
            _order(order_type=OrderType.STOP, stop_price=Decimal("0"))

    def test_stop_with_valid_price(self):
        o = _order(order_type=OrderType.STOP, stop_price=Decimal("140"))
        assert o.stop_price == Decimal("140")

    def test_stop_limit_without_both_prices_raises(self):
        with pytest.raises(ValueError):
            _order(
                order_type=OrderType.STOP_LIMIT,
                limit_price=Decimal("150"),
                stop_price=None,
            )

    def test_stop_limit_with_both_prices(self):
        o = _order(
            order_type=OrderType.STOP_LIMIT,
            limit_price=Decimal("150"),
            stop_price=Decimal("140"),
        )
        assert o.limit_price == Decimal("150")
        assert o.stop_price == Decimal("140")

    def test_market_needs_no_prices(self):
        o = _order(order_type=OrderType.MARKET)
        assert o.limit_price is None
        assert o.stop_price is None


class TestOrderProperties:
    def test_remaining_quantity_initial(self):
        o = _order()
        assert o.remaining_quantity == Decimal("100")

    def test_is_terminal_initial(self):
        o = _order()
        assert o.is_terminal is False

    def test_default_status_is_new(self):
        o = _order()
        assert o.status == OrderStatus.NEW

    def test_default_id_is_uuid(self):
        o = _order()
        assert isinstance(o.id, uuid.UUID)


class TestSubmitTransition:
    def test_new_to_submitted(self):
        o = _order()
        result = o.apply_event(SubmitEvent(occurred_at=_ts(), broker_order_id="B123"))
        assert result.status == OrderStatus.SUBMITTED
        assert result.broker_order_id == "B123"

    def test_submitted_is_not_terminal(self):
        o = _order().apply_event(SubmitEvent(occurred_at=_ts()))
        assert o.is_terminal is False


class TestAckTransition:
    def test_submitted_to_acknowledged(self):
        o = _order().apply_event(SubmitEvent(occurred_at=_ts()))
        result = o.apply_event(AckEvent(occurred_at=_ts(), broker_order_id="B456"))
        assert result.status == OrderStatus.ACKNOWLEDGED
        assert result.broker_order_id == "B456"


class TestPartialFillTransition:
    def test_acknowledged_to_partially_filled(self):
        o = (
            _order()
            .apply_event(SubmitEvent(occurred_at=_ts()))
            .apply_event(AckEvent(occurred_at=_ts(), broker_order_id="B1"))
        )
        result = o.apply_event(
            PartialFillEvent(
                occurred_at=_ts(),
                fill_quantity=Decimal("40"),
                fill_price=Decimal("150"),
            )
        )
        assert result.status == OrderStatus.PARTIALLY_FILLED
        assert result.filled_quantity == Decimal("40")
        assert result.remaining_quantity == Decimal("60")

    def test_partial_fill_updates_avg_price(self):
        o = (
            _order()
            .apply_event(SubmitEvent(occurred_at=_ts()))
            .apply_event(AckEvent(occurred_at=_ts(), broker_order_id="B1"))
        )
        o = o.apply_event(
            PartialFillEvent(
                occurred_at=_ts(),
                fill_quantity=Decimal("50"),
                fill_price=Decimal("100"),
            )
        )
        o = o.apply_event(
            PartialFillEvent(
                occurred_at=_ts(),
                fill_quantity=Decimal("50"),
                fill_price=Decimal("200"),
            )
        )
        assert o.average_fill_price == Decimal("150")

    def test_last_partial_fill_transitions_to_filled(self):
        o = (
            _order()
            .apply_event(SubmitEvent(occurred_at=_ts()))
            .apply_event(AckEvent(occurred_at=_ts(), broker_order_id="B1"))
            .apply_event(
                PartialFillEvent(
                    occurred_at=_ts(),
                    fill_quantity=Decimal("50"),
                    fill_price=Decimal("100"),
                )
            )
        )
        result = o.apply_event(
            PartialFillEvent(
                occurred_at=_ts(),
                fill_quantity=Decimal("50"),
                fill_price=Decimal("110"),
            )
        )
        assert result.status == OrderStatus.FILLED
        assert result.is_terminal


class TestFillTransition:
    def test_acknowledged_to_filled(self):
        o = (
            _order()
            .apply_event(SubmitEvent(occurred_at=_ts()))
            .apply_event(AckEvent(occurred_at=_ts(), broker_order_id="B1"))
        )
        result = o.apply_event(
            FillEvent(
                occurred_at=_ts(),
                fill_quantity=Decimal("100"),
                fill_price=Decimal("150"),
            )
        )
        assert result.status == OrderStatus.FILLED
        assert result.filled_quantity == Decimal("100")
        assert result.remaining_quantity == Decimal("0")
        assert result.is_terminal

    def test_fill_must_match_quantity(self):
        o = (
            _order()
            .apply_event(SubmitEvent(occurred_at=_ts()))
            .apply_event(AckEvent(occurred_at=_ts(), broker_order_id="B1"))
        )
        with pytest.raises(OverFillError, match="PartialFillEvent"):
            o.apply_event(
                FillEvent(
                    occurred_at=_ts(),
                    fill_quantity=Decimal("50"),
                    fill_price=Decimal("150"),
                )
            )


class TestOverFill:
    def test_overfill_raises(self):
        o = (
            _order()
            .apply_event(SubmitEvent(occurred_at=_ts()))
            .apply_event(AckEvent(occurred_at=_ts(), broker_order_id="B1"))
        )
        with pytest.raises(OverFillError, match="exceeding"):
            o.apply_event(
                PartialFillEvent(
                    occurred_at=_ts(),
                    fill_quantity=Decimal("101"),
                    fill_price=Decimal("150"),
                )
            )


class TestCancelTransition:
    def test_cancel_requested(self):
        o = _order().apply_event(SubmitEvent(occurred_at=_ts()))
        result = o.apply_event(CancelEvent(occurred_at=_ts(), requested=True))
        assert result.status == OrderStatus.CANCEL_REQUESTED

    def test_cancel_confirmed(self):
        o = (
            _order()
            .apply_event(SubmitEvent(occurred_at=_ts()))
            .apply_event(CancelEvent(occurred_at=_ts(), requested=True))
        )
        result = o.apply_event(CancelEvent(occurred_at=_ts(), requested=False))
        assert result.status == OrderStatus.CANCELLED
        assert result.is_terminal

    def test_direct_cancel_from_new(self):
        o = _order()
        result = o.apply_event(CancelEvent(occurred_at=_ts(), requested=False))
        assert result.status == OrderStatus.CANCELLED


class TestRejectTransition:
    def test_submitted_to_rejected(self):
        o = _order().apply_event(SubmitEvent(occurred_at=_ts()))
        result = o.apply_event(RejectEvent(occurred_at=_ts(), reason="insufficient funds"))
        assert result.status == OrderStatus.REJECTED
        assert result.reject_reason == "insufficient funds"
        assert result.is_terminal


class TestExpireTransition:
    def test_acknowledged_to_expired(self):
        o = (
            _order()
            .apply_event(SubmitEvent(occurred_at=_ts()))
            .apply_event(AckEvent(occurred_at=_ts(), broker_order_id="B1"))
        )
        result = o.apply_event(ExpireEvent(occurred_at=_ts()))
        assert result.status == OrderStatus.EXPIRED
        assert result.is_terminal


class TestIllegalTransitions:
    def test_filled_to_submitted_raises(self):
        o = (
            _order()
            .apply_event(SubmitEvent(occurred_at=_ts()))
            .apply_event(AckEvent(occurred_at=_ts(), broker_order_id="B1"))
            .apply_event(
                FillEvent(
                    occurred_at=_ts(),
                    fill_quantity=Decimal("100"),
                    fill_price=Decimal("150"),
                )
            )
        )
        with pytest.raises(IllegalTransitionError):
            o.apply_event(SubmitEvent(occurred_at=_ts()))

    def test_cancelled_to_acknowledged_raises(self):
        o = _order().apply_event(CancelEvent(occurred_at=_ts()))
        with pytest.raises(IllegalTransitionError):
            o.apply_event(AckEvent(occurred_at=_ts()))

    def test_rejected_to_submitted_raises(self):
        o = _order().apply_event(RejectEvent(occurred_at=_ts()))
        with pytest.raises(IllegalTransitionError):
            o.apply_event(SubmitEvent(occurred_at=_ts()))

    def test_new_to_filled_raises(self):
        o = _order()
        with pytest.raises(IllegalTransitionError):
            o.apply_event(
                FillEvent(
                    occurred_at=_ts(),
                    fill_quantity=Decimal("100"),
                    fill_price=Decimal("150"),
                )
            )


class TestUnknownEvent:
    def test_unknown_event_type_raises_type_error(self):
        o = _order()
        with pytest.raises(TypeError, match="unknown event type"):
            o.apply_event("not_an_event")  # type: ignore[arg-type]


class TestImmutability:
    def test_apply_event_returns_new_order(self):
        o = _order()
        result = o.apply_event(SubmitEvent(occurred_at=_ts()))
        assert result is not o
        assert o.status == OrderStatus.NEW
        assert result.status == OrderStatus.SUBMITTED

    def test_order_is_frozen(self):
        o = _order()
        with pytest.raises(AttributeError):
            o.status = OrderStatus.FILLED  # type: ignore[misc]


class TestVWAPComputation:
    def test_single_fill_price(self):
        o = (
            _order()
            .apply_event(SubmitEvent(occurred_at=_ts()))
            .apply_event(AckEvent(occurred_at=_ts(), broker_order_id="B1"))
            .apply_event(
                FillEvent(
                    occurred_at=_ts(),
                    fill_quantity=Decimal("100"),
                    fill_price=Decimal("150"),
                )
            )
        )
        assert o.average_fill_price == Decimal("150")

    def test_two_partial_fills_vwap(self):
        o = (
            _order()
            .apply_event(SubmitEvent(occurred_at=_ts()))
            .apply_event(AckEvent(occurred_at=_ts(), broker_order_id="B1"))
            .apply_event(
                PartialFillEvent(
                    occurred_at=_ts(),
                    fill_quantity=Decimal("60"),
                    fill_price=Decimal("100"),
                )
            )
            .apply_event(
                PartialFillEvent(
                    occurred_at=_ts(),
                    fill_quantity=Decimal("40"),
                    fill_price=Decimal("200"),
                )
            )
        )
        expected = (Decimal("100") * Decimal("60") + Decimal("200") * Decimal("40")) / Decimal("100")
        assert o.average_fill_price == expected

    def test_three_partial_fills_vwap(self):
        o = (
            _order(quantity=Decimal("100"))
            .apply_event(SubmitEvent(occurred_at=_ts()))
            .apply_event(AckEvent(occurred_at=_ts(), broker_order_id="B1"))
            .apply_event(
                PartialFillEvent(
                    occurred_at=_ts(),
                    fill_quantity=Decimal("30"),
                    fill_price=Decimal("100"),
                )
            )
            .apply_event(
                PartialFillEvent(
                    occurred_at=_ts(),
                    fill_quantity=Decimal("30"),
                    fill_price=Decimal("110"),
                )
            )
            .apply_event(
                PartialFillEvent(
                    occurred_at=_ts(),
                    fill_quantity=Decimal("40"),
                    fill_price=Decimal("120"),
                )
            )
        )
        assert o.filled_quantity == Decimal("100")
        assert o.status == OrderStatus.FILLED
        expected = (
            Decimal("100") * Decimal("30")
            + Decimal("110") * Decimal("30")
            + Decimal("120") * Decimal("40")
        ) / Decimal("100")
        assert o.average_fill_price == expected


class TestFullLifecycle:
    def test_full_buy_lifecycle(self):
        o = (
            _order(symbol="TSLA", side=OrderSide.BUY, quantity=Decimal("50"))
            .apply_event(SubmitEvent(occurred_at=_ts(), broker_order_id="B1"))
            .apply_event(AckEvent(occurred_at=_ts(), broker_order_id="B1"))
            .apply_event(
                FillEvent(
                    occurred_at=_ts(),
                    fill_quantity=Decimal("50"),
                    fill_price=Decimal("200"),
                )
            )
        )
        assert o.status == OrderStatus.FILLED
        assert o.symbol == "TSLA"
        assert o.filled_quantity == Decimal("50")

    def test_lifecycle_with_partial_then_fill(self):
        o = (
            _order()
            .apply_event(SubmitEvent(occurred_at=_ts()))
            .apply_event(AckEvent(occurred_at=_ts(), broker_order_id="B1"))
            .apply_event(
                PartialFillEvent(
                    occurred_at=_ts(),
                    fill_quantity=Decimal("30"),
                    fill_price=Decimal("100"),
                )
            )
            .apply_event(
                FillEvent(
                    occurred_at=_ts(),
                    fill_quantity=Decimal("70"),
                    fill_price=Decimal("110"),
                )
            )
        )
        assert o.status == OrderStatus.FILLED
        assert o.filled_quantity == Decimal("100")

    def test_cancel_after_partial_fill(self):
        o = (
            _order()
            .apply_event(SubmitEvent(occurred_at=_ts()))
            .apply_event(AckEvent(occurred_at=_ts(), broker_order_id="B1"))
            .apply_event(
                PartialFillEvent(
                    occurred_at=_ts(),
                    fill_quantity=Decimal("30"),
                    fill_price=Decimal("100"),
                )
            )
            .apply_event(CancelEvent(occurred_at=_ts(), requested=True))
            .apply_event(CancelEvent(occurred_at=_ts(), requested=False))
        )
        assert o.status == OrderStatus.CANCELLED
        assert o.filled_quantity == Decimal("30")
