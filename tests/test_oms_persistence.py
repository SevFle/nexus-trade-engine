"""Unit tests for the OMS → DB projection (gh#111 follow-up)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from engine.core.oms import (
    AckEvent,
    FillEvent,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    PartialFillEvent,
    RejectEvent,
    SubmitEvent,
)
from engine.core.oms.persistence import to_orm_dict


_PID = uuid.uuid4()
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
# Status projection
# ---------------------------------------------------------------------------


class TestStatusProjection:
    def test_new_projects_to_pending(self):
        order = _market_buy()
        d = to_orm_dict(order, portfolio_id=_PID)
        assert d["status"] == "pending"

    def test_submitted_also_pending(self):
        order = _market_buy().apply_event(SubmitEvent(occurred_at=_t(1)))
        d = to_orm_dict(order, portfolio_id=_PID)
        # ORM doesn't distinguish NEW/SUBMITTED today.
        assert d["status"] == "pending"

    def test_acknowledged_open(self):
        order = (
            _limit_buy()
            .apply_event(SubmitEvent(occurred_at=_t(1)))
            .apply_event(AckEvent(occurred_at=_t(2), broker_order_id="B"))
        )
        d = to_orm_dict(order, portfolio_id=_PID)
        assert d["status"] == "open"

    def test_partially_filled_projects(self):
        order = (
            _market_buy("10")
            .apply_event(SubmitEvent(occurred_at=_t(1)))
            .apply_event(
                PartialFillEvent(
                    occurred_at=_t(2),
                    fill_quantity=Decimal("4"),
                    fill_price=Decimal("100"),
                    fill_id="F1",
                )
            )
        )
        d = to_orm_dict(order, portfolio_id=_PID)
        assert d["status"] == "partially_filled"

    def test_filled(self):
        order = (
            _market_buy("10")
            .apply_event(SubmitEvent(occurred_at=_t(1)))
            .apply_event(
                FillEvent(
                    occurred_at=_t(2),
                    fill_quantity=Decimal("10"),
                    fill_price=Decimal("100"),
                    fill_id="F1",
                )
            )
        )
        d = to_orm_dict(order, portfolio_id=_PID)
        assert d["status"] == "filled"

    def test_rejected(self):
        order = (
            _market_buy()
            .apply_event(SubmitEvent(occurred_at=_t(1)))
            .apply_event(RejectEvent(occurred_at=_t(2), reason="boom"))
        )
        d = to_orm_dict(order, portfolio_id=_PID)
        assert d["status"] == "rejected"


# ---------------------------------------------------------------------------
# Price projection
# ---------------------------------------------------------------------------


class TestPriceProjection:
    def test_market_unfilled_has_no_price(self):
        order = _market_buy()
        d = to_orm_dict(order, portfolio_id=_PID)
        assert d["price"] is None

    def test_limit_carries_limit_price(self):
        order = _limit_buy(limit="123")
        d = to_orm_dict(order, portfolio_id=_PID)
        assert d["price"] == Decimal("123")

    def test_market_after_fill_uses_avg_fill_price(self):
        order = (
            _market_buy("10")
            .apply_event(SubmitEvent(occurred_at=_t(1)))
            .apply_event(
                FillEvent(
                    occurred_at=_t(2),
                    fill_quantity=Decimal("10"),
                    fill_price=Decimal("101.5"),
                    fill_id="F1",
                )
            )
        )
        d = to_orm_dict(order, portfolio_id=_PID)
        assert d["price"] == Decimal("101.5")

    def test_limit_filled_keeps_limit_price_not_avg(self):
        # Limit carried at 100, filled at 99.5 (improvement). The ORM
        # historically stores the *order* price; avg fill lives elsewhere.
        order = (
            _limit_buy(qty="10", limit="100")
            .apply_event(SubmitEvent(occurred_at=_t(1)))
            .apply_event(
                FillEvent(
                    occurred_at=_t(2),
                    fill_quantity=Decimal("10"),
                    fill_price=Decimal("99.5"),
                    fill_id="F1",
                )
            )
        )
        d = to_orm_dict(order, portfolio_id=_PID)
        assert d["price"] == Decimal("100")


# ---------------------------------------------------------------------------
# filled_at projection
# ---------------------------------------------------------------------------


class TestFilledAt:
    def test_filled_carries_updated_at(self):
        fill_time = _t(2)
        order = (
            _market_buy("10")
            .apply_event(SubmitEvent(occurred_at=_t(1)))
            .apply_event(
                FillEvent(
                    occurred_at=fill_time,
                    fill_quantity=Decimal("10"),
                    fill_price=Decimal("100"),
                    fill_id="F1",
                )
            )
        )
        d = to_orm_dict(order, portfolio_id=_PID)
        assert d["filled_at"] == fill_time

    def test_partially_filled_has_no_filled_at(self):
        order = (
            _market_buy("10")
            .apply_event(SubmitEvent(occurred_at=_t(1)))
            .apply_event(
                PartialFillEvent(
                    occurred_at=_t(2),
                    fill_quantity=Decimal("4"),
                    fill_price=Decimal("100"),
                    fill_id="F1",
                )
            )
        )
        d = to_orm_dict(order, portfolio_id=_PID)
        assert d["filled_at"] is None

    def test_unfilled_has_no_filled_at(self):
        order = _market_buy()
        d = to_orm_dict(order, portfolio_id=_PID)
        assert d["filled_at"] is None


# ---------------------------------------------------------------------------
# Required ORM keys
# ---------------------------------------------------------------------------


class TestKeys:
    def test_dict_has_all_orm_columns(self):
        order = _market_buy()
        d = to_orm_dict(order, portfolio_id=_PID)
        expected = {
            "id",
            "portfolio_id",
            "symbol",
            "side",
            "order_type",
            "quantity",
            "price",
            "status",
            "filled_at",
            "created_at",
        }
        assert set(d.keys()) == expected

    def test_strings_for_enum_columns(self):
        order = _market_buy()
        d = to_orm_dict(order, portfolio_id=_PID)
        assert d["side"] == OrderSide.BUY.value
        assert d["order_type"] == OrderType.MARKET.value

    def test_caller_supplied_portfolio_id_passes_through(self):
        order = _market_buy()
        my_pid = uuid.uuid4()
        d = to_orm_dict(order, portfolio_id=my_pid)
        assert d["portfolio_id"] == my_pid

    def test_status_lookup_consistency(self):
        # Sanity: the status projection table covers every member of
        # OrderStatus. If a new status is added without a mapping, the
        # projection's .get() will silently fall back; this test makes
        # the gap visible.
        from engine.core.oms.persistence import _STATUS_PROJECTION

        assert {s.value for s in OrderStatus} == {
            s.value for s in _STATUS_PROJECTION
        }
