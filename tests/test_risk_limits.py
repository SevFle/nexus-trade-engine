"""Tests for engine.core.risk_limits — pre-trade risk gate."""

from __future__ import annotations

import pytest

from engine.core.risk_limits import (
    AccountState,
    OrderIntent,
    RiskGate,
    RiskLimits,
    RiskLimitsError,
)


def _intent(
    *,
    symbol: str = "AAPL",
    notional: float = 10_000.0,
    side: str = "buy",
    sector: str = "tech",
    asset_class: str = "equity",
) -> OrderIntent:
    return OrderIntent(
        symbol=symbol,
        side=side,
        notional=notional,
        sector=sector,
        asset_class=asset_class,
    )


def _state(
    *,
    cash: float = 100_000.0,
    total_value: float = 100_000.0,
    daily_pnl: float = 0.0,
    exposures: dict[str, float] | None = None,
    sector_exposures: dict[str, float] | None = None,
    asset_class_exposures: dict[str, float] | None = None,
) -> AccountState:
    return AccountState(
        cash=cash,
        total_value=total_value,
        daily_pnl=daily_pnl,
        exposures=exposures or {},
        sector_exposures=sector_exposures or {},
        asset_class_exposures=asset_class_exposures or {},
    )


class TestSingleOrderCap:
    def test_below_cap_approved(self):
        gate = RiskGate(RiskLimits(max_single_order_notional=50_000.0))
        out = gate.check(_intent(notional=10_000.0), _state())
        assert out.approved is True

    def test_above_cap_rejected(self):
        gate = RiskGate(RiskLimits(max_single_order_notional=5_000.0))
        out = gate.check(_intent(notional=10_000.0), _state())
        assert out.approved is False
        assert "single_order_notional" in out.breached_limits


class TestPerSymbolNotional:
    def test_existing_position_room_remaining(self):
        gate = RiskGate(RiskLimits(max_position_notional={"AAPL": 50_000.0}))
        out = gate.check(
            _intent(symbol="AAPL", notional=10_000.0),
            _state(exposures={"AAPL": 30_000.0}),
        )
        assert out.approved is True

    def test_existing_position_breached(self):
        gate = RiskGate(RiskLimits(max_position_notional={"AAPL": 50_000.0}))
        out = gate.check(
            _intent(symbol="AAPL", notional=10_000.0),
            _state(exposures={"AAPL": 45_000.0}),
        )
        assert out.approved is False
        assert "position_notional[AAPL]" in out.breached_limits

    def test_unconfigured_symbol_unbounded(self):
        gate = RiskGate(RiskLimits(max_position_notional={"AAPL": 50_000.0}))
        out = gate.check(
            _intent(symbol="MSFT", notional=1_000_000.0),
            _state(),
        )
        assert out.approved is True

    def test_sell_does_not_breach_long_cap(self):
        gate = RiskGate(RiskLimits(max_position_notional={"AAPL": 50_000.0}))
        out = gate.check(
            _intent(symbol="AAPL", side="sell", notional=10_000.0),
            _state(exposures={"AAPL": 60_000.0}),
        )
        assert out.approved is True


class TestSectorConcentration:
    def test_below_cap(self):
        gate = RiskGate(RiskLimits(max_sector_concentration_pct={"tech": 0.40}))
        out = gate.check(
            _intent(sector="tech", notional=10_000.0),
            _state(sector_exposures={"tech": 20_000.0}, total_value=100_000.0),
        )
        assert out.approved is True

    def test_above_cap(self):
        gate = RiskGate(RiskLimits(max_sector_concentration_pct={"tech": 0.40}))
        out = gate.check(
            _intent(sector="tech", notional=20_000.0),
            _state(sector_exposures={"tech": 30_000.0}, total_value=100_000.0),
        )
        assert out.approved is False
        assert "sector_concentration[tech]" in out.breached_limits


class TestAssetClassConcentration:
    def test_above_cap(self):
        gate = RiskGate(
            RiskLimits(max_asset_class_concentration_pct={"crypto": 0.10})
        )
        out = gate.check(
            _intent(asset_class="crypto", notional=8_000.0),
            _state(
                asset_class_exposures={"crypto": 5_000.0},
                total_value=100_000.0,
            ),
        )
        assert out.approved is False
        assert "asset_class_concentration[crypto]" in out.breached_limits


class TestVelocity:
    def test_within_window(self):
        gate = RiskGate(
            RiskLimits(max_orders_per_window=3, velocity_window_seconds=60)
        )
        for _ in range(3):
            out = gate.check(_intent(), _state())
            assert out.approved is True

    def test_exceeds_window(self):
        gate = RiskGate(
            RiskLimits(max_orders_per_window=2, velocity_window_seconds=60)
        )
        gate.check(_intent(), _state())
        gate.check(_intent(), _state())
        out = gate.check(_intent(), _state())
        assert out.approved is False
        assert "velocity" in out.breached_limits

    def test_window_rolls_off(self):
        clock = [1000.0]
        gate = RiskGate(
            RiskLimits(max_orders_per_window=2, velocity_window_seconds=60),
            clock=lambda: clock[0],
        )
        gate.check(_intent(), _state())
        gate.check(_intent(), _state())
        clock[0] = 1100.0
        out = gate.check(_intent(), _state())
        assert out.approved is True


class TestDailyLossBreaker:
    def test_below_loss_threshold(self):
        gate = RiskGate(RiskLimits(max_daily_loss=5_000.0))
        out = gate.check(_intent(), _state(daily_pnl=-1_000.0))
        assert out.approved is True

    def test_at_loss_threshold_trips(self):
        gate = RiskGate(RiskLimits(max_daily_loss=5_000.0))
        out = gate.check(_intent(), _state(daily_pnl=-5_000.0))
        assert out.approved is False
        assert "daily_loss" in out.breached_limits

    def test_breaker_stays_tripped_after_pnl_recovers(self):
        gate = RiskGate(RiskLimits(max_daily_loss=5_000.0))
        gate.check(_intent(), _state(daily_pnl=-6_000.0))
        out = gate.check(_intent(), _state(daily_pnl=-1_000.0))
        assert out.approved is False
        assert "circuit_breaker" in out.breached_limits

    def test_manual_reset(self):
        gate = RiskGate(RiskLimits(max_daily_loss=5_000.0))
        gate.check(_intent(), _state(daily_pnl=-6_000.0))
        gate.reset_circuit_breaker()
        out = gate.check(_intent(), _state(daily_pnl=-1_000.0))
        assert out.approved is True


class TestMultipleBreaches:
    def test_all_breaches_reported(self):
        gate = RiskGate(
            RiskLimits(
                max_single_order_notional=1_000.0,
                max_position_notional={"AAPL": 1_000.0},
            )
        )
        out = gate.check(
            _intent(symbol="AAPL", notional=10_000.0),
            _state(exposures={"AAPL": 5_000.0}),
        )
        assert out.approved is False
        assert "single_order_notional" in out.breached_limits
        assert "position_notional[AAPL]" in out.breached_limits


class TestValidation:
    def test_negative_notional_rejected_at_construction(self):
        with pytest.raises(RiskLimitsError):
            OrderIntent(
                symbol="AAPL",
                side="buy",
                notional=-1.0,
                sector="tech",
                asset_class="equity",
            )

    def test_unknown_side_rejected(self):
        with pytest.raises(RiskLimitsError):
            OrderIntent(
                symbol="AAPL",
                side="floof",
                notional=1.0,
                sector="tech",
                asset_class="equity",
            )

    def test_negative_total_value_rejected(self):
        with pytest.raises(RiskLimitsError):
            AccountState(
                cash=0.0,
                total_value=-1.0,
                daily_pnl=0.0,
                exposures={},
                sector_exposures={},
                asset_class_exposures={},
            )

    def test_zero_total_value_skips_concentration_checks(self):
        gate = RiskGate(
            RiskLimits(max_sector_concentration_pct={"tech": 0.10})
        )
        out = gate.check(_intent(), _state(total_value=0.0))
        assert out.approved is True


class TestRiskDecisionShape:
    def test_approved_decision_has_no_breaches(self):
        gate = RiskGate(RiskLimits())
        out = gate.check(_intent(), _state())
        assert out.approved is True
        assert out.breached_limits == ()
        assert out.warnings == ()


class TestImmutability:
    def test_account_state_dict_mutation_does_not_leak(self):
        # Caller mutating their source dict after constructing AccountState
        # must NOT change what the gate sees — defends against silent state
        # corruption in long-running gates.
        exposures = {"AAPL": 30_000.0}
        state = AccountState(
            cash=100_000.0,
            total_value=100_000.0,
            daily_pnl=0.0,
            exposures=exposures,
            sector_exposures={},
            asset_class_exposures={},
        )
        exposures["AAPL"] = 999_999_999.0  # caller mutates AFTER construction
        gate = RiskGate(RiskLimits(max_position_notional={"AAPL": 50_000.0}))
        out = gate.check(_intent(symbol="AAPL", notional=10_000.0), state)
        assert out.approved is True  # gate saw the snapshot, not the mutation

    def test_risk_decision_breaches_are_immutable(self):
        gate = RiskGate(RiskLimits(max_single_order_notional=1.0))
        out = gate.check(_intent(notional=100.0), _state())
        with pytest.raises((AttributeError, TypeError)):
            out.breached_limits.append("forged")  # type: ignore[attr-defined]


class TestNumericValidation:
    def test_nan_notional_rejected(self):
        with pytest.raises(RiskLimitsError):
            OrderIntent(
                symbol="AAPL",
                side="buy",
                notional=float("nan"),
                sector="tech",
                asset_class="equity",
            )

    def test_inf_notional_rejected(self):
        with pytest.raises(RiskLimitsError):
            OrderIntent(
                symbol="AAPL",
                side="buy",
                notional=float("inf"),
                sector="tech",
                asset_class="equity",
            )

    def test_nan_daily_pnl_rejected(self):
        with pytest.raises(RiskLimitsError):
            AccountState(
                cash=0.0,
                total_value=1.0,
                daily_pnl=float("nan"),
                exposures={},
                sector_exposures={},
                asset_class_exposures={},
            )


class TestThreadSafety:
    def test_concurrent_checks_do_not_corrupt_velocity_buffer(self):
        # Drives 8 threads * 100 checks each at a 500-cap. With a lock the
        # rolling buffer stays internally consistent (no IndexError, no
        # list mutation during iteration).
        import threading as _t

        gate = RiskGate(
            RiskLimits(max_orders_per_window=500, velocity_window_seconds=60)
        )

        errors: list[BaseException] = []

        def worker():
            try:
                for _ in range(100):
                    gate.check(_intent(), _state())
            except BaseException as exc:
                errors.append(exc)

        threads = [_t.Thread(target=worker) for _ in range(8)]
        for thr in threads:
            thr.start()
        for thr in threads:
            thr.join()

        assert errors == []
