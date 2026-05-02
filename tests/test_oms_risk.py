"""Unit tests for OMS pre-flight risk checks (gh#111 follow-up)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from engine.core.live.kill_switch import KillSwitch
from engine.core.oms import Order, OrderSide, OrderType
from engine.core.oms.risk import (
    Approve,
    KillSwitchCheck,
    MaxOrderNotional,
    MaxOrderQuantity,
    Reject,
    RiskCheck,
    RiskGate,
)


def _order(qty: str = "10", symbol: str = "AAPL") -> Order:
    return Order(
        symbol=symbol,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal(qty),
    )


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class TestResultTypes:
    def test_approve_is_truthy_via_isinstance(self):
        assert isinstance(Approve(), Approve)

    def test_reject_carries_reason(self):
        r = Reject(reason="too big")
        assert r.reason == "too big"


# ---------------------------------------------------------------------------
# MaxOrderQuantity
# ---------------------------------------------------------------------------


class TestMaxOrderQuantity:
    def test_rejects_constructor_zero_limit(self):
        with pytest.raises(ValueError):
            MaxOrderQuantity(limit=Decimal("0"))

    def test_below_limit_approves(self):
        check = MaxOrderQuantity(limit=Decimal("100"))
        assert isinstance(check(_order("50")), Approve)

    def test_at_limit_approves(self):
        check = MaxOrderQuantity(limit=Decimal("100"))
        assert isinstance(check(_order("100")), Approve)

    def test_above_limit_rejects(self):
        check = MaxOrderQuantity(limit=Decimal("100"))
        result = check(_order("101"))
        assert isinstance(result, Reject)
        assert "exceeds max" in result.reason


# ---------------------------------------------------------------------------
# MaxOrderNotional
# ---------------------------------------------------------------------------


class TestMaxOrderNotional:
    def test_rejects_constructor_zero_limit(self):
        with pytest.raises(ValueError):
            MaxOrderNotional(limit=Decimal("0"))

    def test_no_price_approves(self):
        check = MaxOrderNotional(limit=Decimal("1000"))
        # qty=10, no price → can't compute notional → approve.
        assert isinstance(check(_order("10")), Approve)

    def test_zero_price_approves(self):
        check = MaxOrderNotional(limit=Decimal("1000"))
        assert isinstance(
            check(_order("10"), reference_price=Decimal("0")), Approve
        )

    def test_below_limit_approves(self):
        check = MaxOrderNotional(limit=Decimal("1000"))
        # 10 * 50 = 500 < 1000.
        assert isinstance(
            check(_order("10"), reference_price=Decimal("50")), Approve
        )

    def test_above_limit_rejects(self):
        check = MaxOrderNotional(limit=Decimal("1000"))
        # 10 * 200 = 2000 > 1000.
        result = check(_order("10"), reference_price=Decimal("200"))
        assert isinstance(result, Reject)
        assert "notional" in result.reason


# ---------------------------------------------------------------------------
# KillSwitchCheck
# ---------------------------------------------------------------------------


class TestKillSwitchCheck:
    def test_disengaged_approves(self):
        ks = KillSwitch()
        check = KillSwitchCheck(switch=ks)
        assert isinstance(check(_order()), Approve)

    def test_engaged_rejects_with_reason(self):
        ks = KillSwitch()
        ks.engage(reason="manual_panic")
        check = KillSwitchCheck(switch=ks)
        result = check(_order())
        assert isinstance(result, Reject)
        assert "kill-switch engaged" in result.reason
        assert "manual_panic" in result.reason


# ---------------------------------------------------------------------------
# RiskGate
# ---------------------------------------------------------------------------


class TestRiskGate:
    def test_empty_chain_approves_everything(self):
        gate = RiskGate(checks=[])
        assert isinstance(gate.evaluate(_order()), Approve)

    def test_first_reject_wins(self):
        gate = RiskGate(
            checks=[
                MaxOrderQuantity(limit=Decimal("5")),
                MaxOrderNotional(limit=Decimal("10")),
            ]
        )
        # qty=10 trips MaxOrderQuantity first; MaxOrderNotional never runs.
        result = gate.evaluate(_order("10"), reference_price=Decimal("100"))
        assert isinstance(result, Reject)
        assert "exceeds max 5" in result.reason

    def test_all_approve_means_gate_approves(self):
        gate = RiskGate(
            checks=[
                MaxOrderQuantity(limit=Decimal("100")),
                MaxOrderNotional(limit=Decimal("10000")),
            ]
        )
        result = gate.evaluate(_order("10"), reference_price=Decimal("50"))
        assert isinstance(result, Approve)

    def test_kill_switch_short_circuits_gate(self):
        ks = KillSwitch()
        ks.engage(reason="emergency")
        gate = RiskGate(
            checks=[
                KillSwitchCheck(switch=ks),
                # Even a permissive subsequent check shouldn't matter.
                MaxOrderQuantity(limit=Decimal("999999")),
            ]
        )
        result = gate.evaluate(_order())
        assert isinstance(result, Reject)
        assert "kill-switch engaged" in result.reason


# ---------------------------------------------------------------------------
# Protocol contract
# ---------------------------------------------------------------------------


class TestRiskCheckProtocol:
    def test_lambda_satisfies_protocol(self):
        # A bare callable with the right signature is a valid RiskCheck.
        check = lambda order, *, reference_price=None: Approve()  # noqa: E731
        assert isinstance(check, RiskCheck)
        gate = RiskGate(checks=[check])
        assert isinstance(gate.evaluate(_order()), Approve)
