"""Tests for RiskGate metrics emission (gh#111 follow-up).

The gate emits three metrics through the active ``MetricsBackend``:

- ``oms.risk.check`` — counter, one per individual check, tagged with
  ``check``, ``symbol`` and ``outcome ∈ {approve, reject}``.
- ``oms.risk.rejected`` — counter, exactly once when a check rejects
  the order; tagged with the rejecting check's class name.
- ``oms.risk.approved`` — counter, exactly once when every check
  approves.
"""

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
    RiskGate,
)
from engine.observability.metrics import RecordingBackend


def _market_buy(qty: str = "10") -> Order:
    return Order(
        symbol="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal(qty),
    )


def _counter_total(backend: RecordingBackend, name: str) -> float:
    return sum(v for (n, _t), v in backend.counters.items() if n == name)


def _counter_with(backend: RecordingBackend, name: str, tags: dict[str, str]) -> float:
    expected = tuple(sorted(tags.items()))
    return sum(
        v
        for (n, t), v in backend.counters.items()
        if n == name and all(item in t for item in expected)
    )


@pytest.fixture
def metrics() -> RecordingBackend:
    return RecordingBackend()


class TestApproveAllPath:
    def test_each_check_records_approve_then_overall_approved(self, metrics):
        ks = KillSwitch()
        gate = RiskGate(
            checks=[
                KillSwitchCheck(switch=ks),
                MaxOrderQuantity(limit=Decimal("100")),
            ],
            metrics=metrics,
        )

        result = gate.evaluate(_market_buy())

        assert isinstance(result, Approve)
        assert _counter_total(metrics, "oms.risk.check") == 2
        assert (
            _counter_with(
                metrics,
                "oms.risk.check",
                {"check": "KillSwitchCheck", "outcome": "approve"},
            )
            == 1
        )
        assert (
            _counter_with(
                metrics,
                "oms.risk.check",
                {"check": "MaxOrderQuantity", "outcome": "approve"},
            )
            == 1
        )
        assert _counter_total(metrics, "oms.risk.approved") == 1
        assert _counter_total(metrics, "oms.risk.rejected") == 0


class TestFirstRejectShortCircuits:
    def test_quantity_reject_records_reject_and_skips_remaining(self, metrics):
        ks = KillSwitch()
        gate = RiskGate(
            checks=[
                MaxOrderQuantity(limit=Decimal("5")),  # rejects: order qty 10
                KillSwitchCheck(switch=ks),  # never reached
            ],
            metrics=metrics,
        )

        result = gate.evaluate(_market_buy())

        assert isinstance(result, Reject)
        # Only the first check ran: one approve/reject row total.
        assert _counter_total(metrics, "oms.risk.check") == 1
        assert (
            _counter_with(
                metrics,
                "oms.risk.check",
                {"check": "MaxOrderQuantity", "outcome": "reject"},
            )
            == 1
        )
        assert (
            _counter_with(
                metrics,
                "oms.risk.rejected",
                {"check": "MaxOrderQuantity", "symbol": "AAPL"},
            )
            == 1
        )
        assert _counter_total(metrics, "oms.risk.approved") == 0


class TestKillSwitchReject:
    def test_kill_switch_engaged_records_reject(self, metrics):
        ks = KillSwitch()
        ks.engage(reason="manual", actor="test")
        gate = RiskGate(
            checks=[KillSwitchCheck(switch=ks)],
            metrics=metrics,
        )

        result = gate.evaluate(_market_buy())

        assert isinstance(result, Reject)
        assert (
            _counter_with(
                metrics,
                "oms.risk.rejected",
                {"check": "KillSwitchCheck"},
            )
            == 1
        )


class TestNotionalCheck:
    def test_notional_reject_carries_check_name_tag(self, metrics):
        gate = RiskGate(
            checks=[MaxOrderNotional(limit=Decimal("500"))],
            metrics=metrics,
        )

        # 10 shares * $100 reference = $1000 notional > $500 limit
        result = gate.evaluate(_market_buy(), reference_price=Decimal("100"))

        assert isinstance(result, Reject)
        assert (
            _counter_with(
                metrics,
                "oms.risk.rejected",
                {"check": "MaxOrderNotional", "symbol": "AAPL"},
            )
            == 1
        )


class TestDefaultBackend:
    def test_resolves_get_metrics_when_not_injected(self):
        from engine.observability.metrics import NullBackend, set_metrics

        recording = RecordingBackend()
        set_metrics(recording)
        try:
            ks = KillSwitch()
            gate = RiskGate(checks=[KillSwitchCheck(switch=ks)])
            gate.evaluate(_market_buy())
            assert _counter_total(recording, "oms.risk.approved") == 1
        finally:
            set_metrics(NullBackend())


class TestExplicitBackendPrecedence:
    def test_injected_metrics_shadows_get_metrics_singleton(self):
        # Issue #300: an explicit ``metrics=`` backend must win over the
        # process singleton. Even when ``set_metrics()`` has installed a
        # live backend, every emission lands on the injected one and the
        # global backend records nothing.
        from engine.observability.metrics import NullBackend, set_metrics

        global_backend = RecordingBackend()
        set_metrics(global_backend)
        try:
            injected = RecordingBackend()
            ks = KillSwitch()
            gate = RiskGate(
                checks=[
                    KillSwitchCheck(switch=ks),
                    MaxOrderQuantity(limit=Decimal("100")),
                ],
                metrics=injected,
            )

            result = gate.evaluate(_market_buy())

            assert isinstance(result, Approve)
            # All emissions land on the injected backend only.
            assert _counter_total(injected, "oms.risk.check") == 2
            assert _counter_total(injected, "oms.risk.approved") == 1
            # The process singleton is never consulted.
            assert _counter_total(global_backend, "oms.risk.check") == 0
            assert _counter_total(global_backend, "oms.risk.approved") == 0
            assert _counter_total(global_backend, "oms.risk.rejected") == 0
        finally:
            set_metrics(NullBackend())

    def test_reject_path_still_respects_injected_backend(self):
        # First-reject-wins metrics must also respect precedence: the
        # rejecting rows land on the injected backend, never the singleton.
        from engine.observability.metrics import NullBackend, set_metrics

        global_backend = RecordingBackend()
        set_metrics(global_backend)
        try:
            injected = RecordingBackend()
            gate = RiskGate(
                checks=[MaxOrderQuantity(limit=Decimal("5"))],  # rejects qty 10
                metrics=injected,
            )

            result = gate.evaluate(_market_buy())

            assert isinstance(result, Reject)
            assert (
                _counter_with(
                    injected,
                    "oms.risk.check",
                    {"check": "MaxOrderQuantity", "outcome": "reject", "symbol": "AAPL"},
                )
                == 1
            )
            assert (
                _counter_with(
                    injected,
                    "oms.risk.rejected",
                    {"check": "MaxOrderQuantity", "symbol": "AAPL"},
                )
                == 1
            )
            assert _counter_total(injected, "oms.risk.approved") == 0
            assert _counter_total(global_backend, "oms.risk.check") == 0
            assert _counter_total(global_backend, "oms.risk.rejected") == 0
        finally:
            set_metrics(NullBackend())
