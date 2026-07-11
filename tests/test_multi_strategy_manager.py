"""Tests for engine.strategies.multi_manager.MultiStrategyManager.

Covers: registration (with explicit ids + allocation_pct), duplicate-
registration rejection, allocation-budget math, signal aggregation with
provenance tagging, per-strategy capital-cap enforcement, sync/async/
awaitable dispatch, failure & timeout isolation, registry-mutation
safety during a cycle, and input-copy isolation between strategies.
"""

from __future__ import annotations

import asyncio
import math
import threading

import pytest

from engine.core.signal import Side, Signal, SignalStrength
from engine.strategies.multi_manager import (
    MultiStrategyEvaluation,
    MultiStrategyManager,
    MultiStrategyManagerError,
    StrategyRegistration,
)

# --------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------- #


def _sig(symbol: str, side: Side, strategy_id: str = "", *, weight: float = 1.0, **kw) -> Signal:
    return Signal(
        symbol=symbol,
        side=side,
        strategy_id=strategy_id,
        weight=weight,
        **kw,
    )


class _RecordingStrategy:
    """Strategy that returns a fixed signal list and records the exact
    market_data / cost_model objects it received (so tests can assert
    every strategy saw the *same* inputs). The signals are returned as
    fresh copies so the manager's re-tagging cannot mutate the fixture."""

    def __init__(self, signals: list[Signal]) -> None:
        self._signals = signals
        self.received: list[tuple[object, object]] = []
        self.call_count = 0

    async def evaluate(self, market_data, cost_model) -> list[Signal]:
        self.received.append((market_data, cost_model))
        self.call_count += 1
        return [s.model_copy() for s in self._signals]


class _SyncStrategy:
    """Sync (non-async) strategy variant to prove evaluate_all handles
    plain callables too."""

    def __init__(self, signals: list[Signal]) -> None:
        self._signals = signals

    def evaluate(self, market_data, cost_model) -> list[Signal]:
        return [s.model_copy() for s in self._signals]


class _RaisingStrategy:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def evaluate(self, market_data, cost_model) -> list[Signal]:
        raise self._exc


class _NoneStrategy:
    async def evaluate(self, market_data, cost_model):
        return None


class _NoEvaluate:
    id = "has-id-but-no-evaluate"


_MARKET = {"prices": {"AAPL": 150.0, "MSFT": 300.0, "TSLA": 200.0}}
_COSTS = {"fee_bps": 5.0, "spread_bps": 1.0}


# --------------------------------------------------------------------- #
# Registration & introspection
# --------------------------------------------------------------------- #


class TestRegistration:
    def test_register_returns_registration_record(self):
        mgr = MultiStrategyManager(total_capital=100_000.0)
        reg = mgr.register("momentum", _RecordingStrategy([]), allocation_pct=30.0)

        assert isinstance(reg, StrategyRegistration)
        assert reg.strategy_id == "momentum"
        assert reg.allocation_pct == 30.0
        # Dollar cap = total_capital * pct / 100.
        assert reg.allocation_cap == pytest.approx(30_000.0)
        assert "momentum" in mgr
        assert len(mgr) == 1
        assert mgr.strategy_ids == ["momentum"]

    def test_register_explicit_id_overrides_strategy_label(self):
        # The registered id is the source of truth, NOT strategy.id.
        class _Mislabel:
            id = "i-lie-about-my-id"

            async def evaluate(self, market_data, cost_model):
                return [_sig("AAPL", Side.BUY, "also-lie")]

        mgr = MultiStrategyManager()
        mgr.register("truthful", _Mislabel(), allocation_pct=10.0)

        result = asyncio.run(mgr.evaluate_all(_MARKET, _COSTS))

        # The emitted signal is tagged with the *registered* id.
        assert result.signals[0].strategy_id == "truthful"
        assert result.signals[0].symbol == "AAPL"

    def test_register_rejects_non_string_id(self):
        mgr = MultiStrategyManager()
        with pytest.raises(MultiStrategyManagerError, match="strategy_id must be a string"):
            mgr.register(123, _RecordingStrategy([]), allocation_pct=10.0)  # type: ignore[arg-type]

    def test_register_rejects_empty_id(self):
        mgr = MultiStrategyManager()
        with pytest.raises(MultiStrategyManagerError, match="non-empty string"):
            mgr.register("   ", _RecordingStrategy([]), allocation_pct=10.0)

    def test_register_rejects_duplicate(self):
        mgr = MultiStrategyManager()
        mgr.register("dup", _RecordingStrategy([]), allocation_pct=10.0)
        with pytest.raises(MultiStrategyManagerError, match="already registered"):
            mgr.register("dup", _RecordingStrategy([]), allocation_pct=20.0)

    def test_register_rejects_strategy_without_evaluate(self):
        mgr = MultiStrategyManager()
        with pytest.raises(MultiStrategyManagerError, match="callable `evaluate`"):
            mgr.register("bad", _NoEvaluate(), allocation_pct=10.0)  # type: ignore[arg-type]

    def test_register_rejects_none_strategy(self):
        mgr = MultiStrategyManager()
        with pytest.raises(MultiStrategyManagerError, match="must not be None"):
            mgr.register("bad", None, allocation_pct=10.0)  # type: ignore[arg-type]

    @pytest.mark.parametrize("bad", [-0.01, -1.0, 100.01, 150.0, float("nan"), float("inf")])
    def test_register_rejects_out_of_range_allocation_pct(self, bad):
        mgr = MultiStrategyManager()
        with pytest.raises(MultiStrategyManagerError):
            mgr.register("s1", _RecordingStrategy([]), allocation_pct=bad)

    def test_register_rejects_non_numeric_allocation_pct(self):
        mgr = MultiStrategyManager()
        with pytest.raises(MultiStrategyManagerError, match="must be a number"):
            mgr.register("s1", _RecordingStrategy([]), allocation_pct="half")  # type: ignore[arg-type]

    def test_register_rejects_none_allocation_pct(self):
        # ``None`` is neither bool nor str, so it falls through to
        # ``float(None)`` which raises TypeError -> caught and re-raised.
        mgr = MultiStrategyManager()
        with pytest.raises(MultiStrategyManagerError, match="must be a number"):
            mgr.register("s1", _RecordingStrategy([]), allocation_pct=None)  # type: ignore[arg-type]

    def test_register_rejects_bool_allocation_pct(self):
        # bool subclasses int and would otherwise sneak through as 1/0.
        mgr = MultiStrategyManager()
        with pytest.raises(MultiStrategyManagerError, match="must be a number"):
            mgr.register("s1", _RecordingStrategy([]), allocation_pct=True)  # type: ignore[arg-type]

    def test_register_allows_zero_allocation_pct(self):
        # 0% registers a capital-starved but active strategy.
        mgr = MultiStrategyManager(total_capital=1000.0)
        reg = mgr.register("paused", _RecordingStrategy([]), allocation_pct=0.0)
        assert reg.allocation_pct == 0.0
        assert reg.allocation_cap == 0.0

    def test_register_rejects_total_allocation_over_100(self):
        mgr = MultiStrategyManager()
        mgr.register("a", _RecordingStrategy([]), allocation_pct=60.0)
        mgr.register("b", _RecordingStrategy([]), allocation_pct=40.0)
        # a + b == 100; adding c at any positive pct must be rejected.
        with pytest.raises(MultiStrategyManagerError, match="would raise total allocation"):
            mgr.register("c", _RecordingStrategy([]), allocation_pct=1.0)

    def test_register_allows_total_allocation_exactly_100(self):
        mgr = MultiStrategyManager()
        mgr.register("a", _RecordingStrategy([]), allocation_pct=60.0)
        # Landing exactly on 100 is fine.
        mgr.register("b", _RecordingStrategy([]), allocation_pct=40.0)
        assert mgr.total_allocation_pct == pytest.approx(100.0)

    def test_unregister(self):
        mgr = MultiStrategyManager()
        mgr.register("s1", _RecordingStrategy([]), allocation_pct=10.0)
        mgr.register("s2", _RecordingStrategy([]), allocation_pct=20.0)

        assert mgr.unregister("s1") is True
        assert "s1" not in mgr
        assert len(mgr) == 1
        # Idempotent.
        assert mgr.unregister("s1") is False
        # Unregistering frees budget for re-allocation.
        mgr.register("s1", _RecordingStrategy([]), allocation_pct=10.0)
        assert "s1" in mgr

    def test_max_strategies_enforced(self):
        mgr = MultiStrategyManager(max_strategies=2)
        mgr.register("a", _RecordingStrategy([]), allocation_pct=10.0)
        mgr.register("b", _RecordingStrategy([]), allocation_pct=10.0)
        with pytest.raises(MultiStrategyManagerError, match="max_strategies"):
            mgr.register("c", _RecordingStrategy([]), allocation_pct=10.0)


# --------------------------------------------------------------------- #
# Constructor validation
# --------------------------------------------------------------------- #


class TestConstructor:
    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_rejects_non_finite_total_capital(self, bad):
        with pytest.raises(MultiStrategyManagerError):
            MultiStrategyManager(total_capital=bad)

    def test_rejects_negative_total_capital(self):
        with pytest.raises(MultiStrategyManagerError, match="non-negative"):
            MultiStrategyManager(total_capital=-1.0)

    @pytest.mark.parametrize("bad", [0, -1.0, float("nan"), float("inf")])
    def test_rejects_invalid_eval_timeout(self, bad):
        with pytest.raises(MultiStrategyManagerError):
            MultiStrategyManager(eval_timeout=bad)

    def test_rejects_zero_max_strategies(self):
        with pytest.raises(MultiStrategyManagerError, match="max_strategies"):
            MultiStrategyManager(max_strategies=0)

    def test_int_max_strategies_accepted(self):
        # A plain ``int`` (the documented type) must not crash.
        mgr = MultiStrategyManager(max_strategies=5)
        assert mgr._max_strategies == 5
        # The ceiling is enforced against the int value.
        for sid in ("a", "b", "c", "d", "e"):
            mgr.register(sid, _RecordingStrategy([]), allocation_pct=10.0)
        with pytest.raises(MultiStrategyManagerError, match="max_strategies"):
            mgr.register("f", _RecordingStrategy([]), allocation_pct=10.0)

    def test_integer_valued_float_max_strategies_accepted(self):
        # ``5.0`` is an integer value -> coerced to 5.
        mgr = MultiStrategyManager(max_strategies=5.0)
        assert mgr._max_strategies == 5

    def test_fractional_max_strategies_rejected(self):
        # ``5.5`` must NOT be silently truncated to 5 by ``int()``.
        with pytest.raises(MultiStrategyManagerError, match="must be an integer"):
            MultiStrategyManager(max_strategies=5.5)

    def test_string_max_strategies_rejected(self):
        # Consistent with ``_finite``: numeric strings are rejected.
        with pytest.raises(MultiStrategyManagerError, match="must be a number"):
            MultiStrategyManager(max_strategies="5")  # type: ignore[arg-type]

    def test_bool_max_strategies_rejected(self):
        # bool subclasses int and must not sneak through as 1.
        with pytest.raises(MultiStrategyManagerError, match="must be a number"):
            MultiStrategyManager(max_strategies=True)  # type: ignore[arg-type]


# --------------------------------------------------------------------- #
# Capital allocation math
# --------------------------------------------------------------------- #


class TestCapitalAllocation:
    def test_allocation_cap_is_pct_of_total(self):
        mgr = MultiStrategyManager(total_capital=1_000_000.0)
        mgr.register("big", _RecordingStrategy([]), allocation_pct=45.0)
        mgr.register("small", _RecordingStrategy([]), allocation_pct=5.0)

        assert mgr.allocation_cap("big") == pytest.approx(450_000.0)
        assert mgr.allocation_cap("small") == pytest.approx(50_000.0)

    def test_allocations_returns_all_strategies(self):
        mgr = MultiStrategyManager(total_capital=1000.0)
        mgr.register("a", _RecordingStrategy([]), allocation_pct=25.0)
        mgr.register("b", _RecordingStrategy([]), allocation_pct=15.0)

        allocs = mgr.allocations()
        assert allocs == {"a": pytest.approx(250.0), "b": pytest.approx(150.0)}

    def test_allocation_cap_unknown_strategy_is_zero(self):
        mgr = MultiStrategyManager(total_capital=1000.0)
        assert mgr.allocation_cap("ghost") == 0.0

    def test_zero_total_capital_gives_zero_dollar_caps(self):
        mgr = MultiStrategyManager(total_capital=0.0)
        mgr.register("a", _RecordingStrategy([]), allocation_pct=50.0)
        # Dollar cap is 0, but the fraction cap still applies.
        assert mgr.allocation_cap("a") == 0.0
        assert mgr.get_allocation_pct("a") == 50.0

    def test_allocation_pcts_snapshot_is_independent(self):
        mgr = MultiStrategyManager()
        mgr.register("a", _RecordingStrategy([]), allocation_pct=10.0)
        snap = mgr.allocation_pcts
        snap["a"] = 999.0
        assert mgr.get_allocation_pct("a") == 10.0

    def test_trivial_properties(self):
        # Coverage for the one-line property accessors.
        mgr = MultiStrategyManager(total_capital=5000.0)
        mgr.register("a", _RecordingStrategy([]), allocation_pct=10.0)
        assert mgr.total_capital == 5000.0
        assert mgr.strategy_ids == ["a"]
        regs = mgr.registrations
        assert isinstance(regs["a"], StrategyRegistration)
        assert mgr.get_allocation_pct("ghost") is None


# --------------------------------------------------------------------- #
# evaluate_all — signal aggregation & provenance
# --------------------------------------------------------------------- #


class TestEvaluateAggregation:
    async def test_empty_registry_is_noop(self):
        mgr = MultiStrategyManager()
        result = await mgr.evaluate_all(_MARKET, _COSTS)

        assert isinstance(result, MultiStrategyEvaluation)
        assert result.is_noop
        assert result.signals == []
        assert result.per_strategy_signals == {}
        assert result.errors == {}
        assert result.allocation_caps == {}

    async def test_aggregates_signals_from_all_strategies(self):
        mgr = MultiStrategyManager()
        mgr.register(
            "momentum",
            _RecordingStrategy([_sig("AAPL", Side.BUY, "momentum", weight=0.2)]),
            allocation_pct=40.0,
        )
        mgr.register(
            "mean_rev",
            _RecordingStrategy(
                [
                    _sig("MSFT", Side.SELL, "mean_rev", weight=0.1),
                    _sig("TSLA", Side.BUY, "mean_rev"),
                ]
            ),
            allocation_pct=40.0,
        )

        result = await mgr.evaluate_all(_MARKET, _COSTS)

        # Every signal is forwarded (no per-symbol merging here).
        assert len(result.signals) == 3
        by_source = result.per_strategy_signals
        assert set(by_source) == {"momentum", "mean_rev"}
        assert {s.symbol for s in by_source["momentum"]} == {"AAPL"}
        assert {s.symbol for s in by_source["mean_rev"]} == {"MSFT", "TSLA"}

    async def test_each_signal_tagged_with_registered_strategy_id(self):
        # Strategies emit signals with their own (possibly empty) id; the
        # manager re-tags every signal with the registered id.
        mgr = MultiStrategyManager()
        mgr.register(
            "alpha",
            _RecordingStrategy(
                [
                    _sig("AAPL", Side.BUY, "wrong", weight=0.1),
                    _sig("MSFT", Side.HOLD, "", weight=0.1),
                ]
            ),
            allocation_pct=20.0,
        )

        result = await mgr.evaluate_all(_MARKET, _COSTS)

        assert all(s.strategy_id == "alpha" for s in result.signals)
        # Original side/strength metadata is preserved.
        assert {s.symbol: s.side for s in result.signals} == {"AAPL": Side.BUY, "MSFT": Side.HOLD}

    async def test_signals_annotated_with_capital_budget(self):
        mgr = MultiStrategyManager(total_capital=10_000.0)
        mgr.register(
            "s1",
            _RecordingStrategy([_sig("AAPL", Side.BUY, "s1", weight=0.05)]),
            allocation_pct=25.0,
        )

        result = await mgr.evaluate_all(_MARKET, _COSTS)

        meta = result.signals[0].metadata
        assert meta["allocation_cap_pct"] == 25.0
        assert meta["allocation_cap_dollars"] == pytest.approx(2500.0)

    async def test_original_strategy_signals_not_mutated(self):
        original = [_sig("AAPL", Side.BUY, "src", weight=0.1)]
        strategy = _RecordingStrategy(original)
        mgr = MultiStrategyManager()
        mgr.register("s1", strategy, allocation_pct=20.0)

        await mgr.evaluate_all(_MARKET, _COSTS)

        # The fixture signals are untouched by the manager's re-tagging.
        assert original[0].strategy_id == "src"
        assert original[0].weight == 0.1

    async def test_result_shape(self):
        mgr = MultiStrategyManager(total_capital=100_000.0)
        mgr.register(
            "a",
            _RecordingStrategy([_sig("AAPL", Side.BUY, "a", weight=0.1)]),
            allocation_pct=30.0,
        )
        mgr.register(
            "b",
            _RecordingStrategy([_sig("AAPL", Side.HOLD, "b", weight=0.1)]),
            allocation_pct=30.0,
        )

        result = await mgr.evaluate_all(_MARKET, _COSTS)

        assert result.total_capital == 100_000.0
        assert result.strategy_count == 2
        assert set(result.allocation_caps) == {"a", "b"}
        # trade_signals excludes HOLDs.
        assert all(s.side != Side.HOLD for s in result.trade_signals)
        assert len(result.trade_signals) == 1
        assert not result.is_noop

    async def test_strategy_returning_none_is_empty_batch(self):
        mgr = MultiStrategyManager()
        mgr.register(
            "real",
            _RecordingStrategy([_sig("AAPL", Side.BUY, "real", weight=0.1)]),
            allocation_pct=20.0,
        )
        mgr.register("none", _NoneStrategy(), allocation_pct=20.0)

        result = await mgr.evaluate_all(_MARKET, _COSTS)

        assert result.per_strategy_signals["none"] == []
        assert len(result.signals) == 1
        assert result.errors == {}


# --------------------------------------------------------------------- #
# Allocation-cap enforcement
# --------------------------------------------------------------------- #


class TestAllocationEnforcement:
    async def test_within_cap_is_not_scaled(self):
        mgr = MultiStrategyManager()
        # Allocation fraction = 20%. Two BUYs at 0.05 each sum to 0.10 < 0.20.
        mgr.register(
            "s1",
            _RecordingStrategy(
                [
                    _sig("AAPL", Side.BUY, "s1", weight=0.05),
                    _sig("MSFT", Side.BUY, "s1", weight=0.05),
                ]
            ),
            allocation_pct=20.0,
        )

        result = await mgr.evaluate_all(_MARKET, _COSTS)

        assert result.allocation_adjustments == {}
        weights = {s.symbol: s.weight for s in result.signals}
        assert weights["AAPL"] == pytest.approx(0.05)
        assert weights["MSFT"] == pytest.approx(0.05)

    async def test_over_cap_weights_scaled_proportionally(self):
        mgr = MultiStrategyManager()
        # Allocation fraction = 20% (0.20). Strategy commits 0.5 + 0.5 = 1.0.
        # Scale factor = 0.20 / 1.0 = 0.20 -> each becomes 0.10.
        mgr.register(
            "s1",
            _RecordingStrategy(
                [
                    _sig("AAPL", Side.BUY, "s1", weight=0.5),
                    _sig("MSFT", Side.BUY, "s1", weight=0.5),
                ]
            ),
            allocation_pct=20.0,
        )

        result = await mgr.evaluate_all(_MARKET, _COSTS)

        # Recorded adjustment.
        assert "s1" in result.allocation_adjustments
        assert result.allocation_adjustments["s1"] == pytest.approx(0.20)
        # Weights scaled so their sum == allocation fraction (0.20).
        active = [s for s in result.signals if s.side != Side.HOLD]
        assert math.isclose(sum(s.weight for s in active), 0.20, abs_tol=1e-9)
        # Proportional: equal inputs stay equal.
        assert active[0].weight == pytest.approx(active[1].weight)
        # Metadata records the original weight + capped flag.
        assert all(s.metadata.get("allocation_capped") is True for s in active)
        assert all(s.metadata.get("allocation_original_weight") == 0.5 for s in active)

    async def test_cap_enforced_per_strategy_independently(self):
        # Two strategies with different caps: each is enforced against its
        # own fraction, not the book total.
        mgr = MultiStrategyManager()
        # big: 50% cap -> fraction 0.5; commits 0.8 -> scaled to 0.5.
        mgr.register(
            "big",
            _RecordingStrategy([_sig("AAPL", Side.BUY, "big", weight=0.8)]),
            allocation_pct=50.0,
        )
        # small: 10% cap -> fraction 0.10; commits 0.4 -> scaled to 0.10.
        mgr.register(
            "small",
            _RecordingStrategy([_sig("MSFT", Side.BUY, "small", weight=0.4)]),
            allocation_pct=10.0,
        )

        result = await mgr.evaluate_all(_MARKET, _COSTS)

        by_source = {sid: sigs[0] for sid, sigs in result.per_strategy_signals.items()}
        assert by_source["big"].weight == pytest.approx(0.5)
        assert by_source["small"].weight == pytest.approx(0.10)
        assert set(result.allocation_adjustments) == {"big", "small"}

    async def test_hold_signals_abstain_from_cap_sum(self):
        mgr = MultiStrategyManager()
        # Allocation fraction = 20%. BUY at 0.20 + HOLD at 0.9 -> active
        # sum is 0.20 (HOLD abstains), so no scaling is applied even
        # though the HOLD's raw weight exceeds the fraction.
        mgr.register(
            "s1",
            _RecordingStrategy(
                [
                    _sig("AAPL", Side.BUY, "s1", weight=0.20),
                    _sig("MSFT", Side.HOLD, "s1", weight=0.9),
                ]
            ),
            allocation_pct=20.0,
        )

        result = await mgr.evaluate_all(_MARKET, _COSTS)

        assert result.allocation_adjustments == {}
        buy = next(s for s in result.signals if s.symbol == "AAPL")
        hold = next(s for s in result.signals if s.symbol == "MSFT")
        assert buy.weight == pytest.approx(0.20)
        assert hold.weight == pytest.approx(0.9)

    async def test_hold_passes_through_unchanged_when_active_scaled(self):
        # When scaling IS triggered, HOLD signals in the same batch must
        # pass through untouched (they abstain from both the sum and the
        # rescale). Active weights are scaled down to the fraction.
        mgr = MultiStrategyManager()
        # Fraction = 20%. Active sum = 0.5 + 0.5 = 1.0 > 0.20 -> scaled
        # by 0.20 to 0.10 each; the HOLD at 0.9 is untouched.
        mgr.register(
            "s1",
            _RecordingStrategy(
                [
                    _sig("AAPL", Side.BUY, "s1", weight=0.5),
                    _sig("MSFT", Side.BUY, "s1", weight=0.5),
                    _sig("TSLA", Side.HOLD, "s1", weight=0.9),
                ]
            ),
            allocation_pct=20.0,
        )

        result = await mgr.evaluate_all(_MARKET, _COSTS)

        assert "s1" in result.allocation_adjustments
        aapl = next(s for s in result.signals if s.symbol == "AAPL")
        msft = next(s for s in result.signals if s.symbol == "MSFT")
        tsla = next(s for s in result.signals if s.symbol == "TSLA")
        assert aapl.weight == pytest.approx(0.10)
        assert msft.weight == pytest.approx(0.10)
        # HOLD untouched and not marked as capped.
        assert tsla.weight == pytest.approx(0.9)
        assert not tsla.metadata.get("allocation_capped")

    async def test_zero_allocation_pct_zeroes_active_weights(self):
        mgr = MultiStrategyManager(total_capital=1000.0)
        mgr.register(
            "paused",
            _RecordingStrategy(
                [
                    _sig("AAPL", Side.BUY, "paused", weight=0.5),
                    _sig("MSFT", Side.HOLD, "paused", weight=0.3),
                ]
            ),
            allocation_pct=0.0,
        )

        result = await mgr.evaluate_all(_MARKET, _COSTS)

        buy = next(s for s in result.signals if s.symbol == "AAPL")
        hold = next(s for s in result.signals if s.symbol == "MSFT")
        # Capital-starved strategy deploys nothing but keeps its intent.
        assert buy.weight == 0.0
        assert buy.side == Side.BUY
        assert buy.metadata.get("allocation_capped") is True
        assert buy.metadata.get("allocation_original_weight") == 0.5
        # HOLD passes through untouched.
        assert hold.weight == pytest.approx(0.3)

    async def test_cap_records_adjustment_even_when_total_is_zero(self):
        # total_capital == 0 => dollar caps are 0, but fraction caps still
        # drive the scaling so a strategy cannot over-commit the book.
        mgr = MultiStrategyManager(total_capital=0.0)
        mgr.register(
            "s1",
            _RecordingStrategy([_sig("AAPL", Side.BUY, "s1", weight=0.4)]),
            allocation_pct=10.0,
        )

        result = await mgr.evaluate_all(_MARKET, _COSTS)

        assert result.allocation_caps["s1"] == 0.0
        assert result.signals[0].weight == pytest.approx(0.10)
        assert "s1" in result.allocation_adjustments


# --------------------------------------------------------------------- #
# Dispatch: sync / async / awaitable
# --------------------------------------------------------------------- #


class TestDispatch:
    async def test_sync_strategy_evaluated(self):
        mgr = MultiStrategyManager()
        mgr.register(
            "sync",
            _SyncStrategy([_sig("AAPL", Side.BUY, "sync", weight=0.1)]),
            allocation_pct=20.0,
        )

        result = await mgr.evaluate_all(_MARKET, _COSTS)

        assert result.errors == {}
        assert result.signals[0].strategy_id == "sync"
        assert result.signals[0].side == Side.BUY

    async def test_async_strategy_evaluated(self):
        mgr = MultiStrategyManager()
        mgr.register(
            "async",
            _RecordingStrategy([_sig("AAPL", Side.SELL, "async", weight=0.1)]),
            allocation_pct=20.0,
        )

        result = await mgr.evaluate_all(_MARKET, _COSTS)

        assert result.errors == {}
        assert result.signals[0].side == Side.SELL

    async def test_sync_returning_awaitable_is_awaited(self):
        # A plain ``def evaluate`` returning a coroutine must be awaited,
        # not treated as a bare signal list.
        class _AwaitableStrategy:
            def evaluate(self, market_data, cost_model):  # not async def
                async def _impl():
                    return [_sig("AAPL", Side.BUY, "awaits", weight=0.1)]

                return _impl()

        mgr = MultiStrategyManager()
        mgr.register("awaits", _AwaitableStrategy(), allocation_pct=20.0)

        result = await mgr.evaluate_all(_MARKET, _COSTS)

        assert result.errors == {}
        assert result.signals[0].side == Side.BUY

    async def test_generator_returning_evaluate_materialised(self):
        class _GenStrategy:
            def evaluate(self, market_data, cost_model):
                yield _sig("AAPL", Side.BUY, "gen", weight=0.05)
                yield _sig("MSFT", Side.SELL, "gen", weight=0.05)

        mgr = MultiStrategyManager()
        mgr.register("gen", _GenStrategy(), allocation_pct=20.0)

        result = await mgr.evaluate_all(_MARKET, _COSTS)

        assert result.errors == {}
        assert {s.symbol: s.side for s in result.signals} == {"AAPL": Side.BUY, "MSFT": Side.SELL}


# --------------------------------------------------------------------- #
# Robustness: failure isolation, timeout, registry mutation, copy isolation
# --------------------------------------------------------------------- #


class TestFailureIsolation:
    async def test_one_strategy_raising_does_not_kill_cycle(self):
        mgr = MultiStrategyManager()
        good = _RecordingStrategy([_sig("AAPL", Side.BUY, "good", weight=0.1)])
        mgr.register("good", good, allocation_pct=20.0)
        mgr.register("bad", _RaisingStrategy(RuntimeError("boom")), allocation_pct=20.0)

        result = await mgr.evaluate_all(_MARKET, _COSTS)

        assert "bad" in result.errors
        assert "RuntimeError" in result.errors["bad"]
        # The healthy strategy still contributed.
        assert "good" in result.per_strategy_signals
        assert result.signals[0].side == Side.BUY

    async def test_strategy_raising_timeout_is_failure_not_timeout(self):
        # A strategy that raises the builtin TimeoutError itself (rather
        # than exceeding the deadline) is a crash, not a timeout.
        mgr = MultiStrategyManager()
        mgr.register("crash", _RaisingStrategy(TimeoutError("manual")), allocation_pct=20.0)
        mgr.register(
            "good",
            _RecordingStrategy([_sig("AAPL", Side.BUY, "good", weight=0.1)]),
            allocation_pct=20.0,
        )

        result = await mgr.evaluate_all(_MARKET, _COSTS)

        # Reported under errors (not as a deadline timeout), and the
        # message reflects the strategy's own exception.
        assert "crash" in result.errors
        assert "TimeoutError" in result.errors["crash"]
        assert "good" in result.per_strategy_signals

    async def test_sync_strategy_that_raises_is_isolated(self):
        # A *sync* evaluate that raises is recorded as an error and does
        # not abort the cycle (same isolation contract as async raises).
        class _CrashingSync:
            def evaluate(self, market_data, cost_model):  # sync, raises
                raise ValueError("sync crash")

        mgr = MultiStrategyManager()
        mgr.register("crash", _CrashingSync(), allocation_pct=20.0)
        mgr.register(
            "good",
            _RecordingStrategy([_sig("AAPL", Side.BUY, "good", weight=0.1)]),
            allocation_pct=20.0,
        )

        result = await mgr.evaluate_all(_MARKET, _COSTS)

        assert "crash" in result.errors
        assert "ValueError" in result.errors["crash"]
        assert "good" in result.per_strategy_signals
        assert result.signals[0].side == Side.BUY


class TestTimeout:
    async def test_slow_async_strategy_times_out(self):
        mgr = MultiStrategyManager(eval_timeout=0.05)

        class _Slow:
            async def evaluate(self, market_data, cost_model):
                await asyncio.sleep(5.0)
                return [_sig("AAPL", Side.BUY, "slow", weight=0.1)]

        mgr.register("slow", _Slow(), allocation_pct=20.0)
        mgr.register(
            "good",
            _RecordingStrategy([_sig("AAPL", Side.BUY, "good", weight=0.1)]),
            allocation_pct=20.0,
        )

        result = await mgr.evaluate_all(_MARKET, _COSTS)

        assert "slow" in result.errors
        assert "TimeoutError" in result.errors["slow"]
        assert "0.05" in result.errors["slow"]
        assert "good" in result.per_strategy_signals
        assert result.signals[0].side == Side.BUY

    async def test_default_timeout_is_thirty_seconds(self):
        mgr = MultiStrategyManager()
        assert math.isclose(mgr._eval_timeout, 30.0)


class TestRegistryMutationDuringEvaluate:
    async def test_register_during_evaluate_does_not_crash(self):
        mgr = MultiStrategyManager()

        class _Registering:
            async def evaluate(self, market_data, cost_model):
                # Mutate the registry mid-cycle.
                mgr.register(
                    "late",
                    _RecordingStrategy([_sig("MSFT", Side.SELL, "late", weight=0.1)]),
                    allocation_pct=20.0,
                )
                return [_sig("AAPL", Side.BUY, "early", weight=0.1)]

        mgr.register("early", _Registering(), allocation_pct=20.0)

        result = await mgr.evaluate_all(_MARKET, _COSTS)

        # Cycle completed; latecomer deferred to the next cycle.
        assert set(result.per_strategy_signals) == {"early"}
        assert "late" in mgr  # registration took effect afterwards

    async def test_unregister_during_evaluate_does_not_crash(self):
        mgr = MultiStrategyManager()

        class _Unregistering:
            async def evaluate(self, market_data, cost_model):
                mgr.unregister("victim")
                return [_sig("AAPL", Side.BUY, "keeper", weight=0.1)]

        mgr.register("keeper", _Unregistering(), allocation_pct=20.0)
        mgr.register(
            "victim",
            _RecordingStrategy([_sig("TSLA", Side.SELL, "victim", weight=0.1)]),
            allocation_pct=20.0,
        )

        result = await mgr.evaluate_all(_MARKET, _COSTS)

        # Both ran (snapshot captured both before the mutation).
        assert set(result.per_strategy_signals) == {"keeper", "victim"}
        assert "victim" not in mgr


class TestInputIsolation:
    async def test_each_strategy_receives_its_own_deep_copy(self):
        market = {"prices": {"AAPL": 150.0}}
        costs = {"fee_bps": 5.0}

        class _Mutating:
            async def evaluate(self, market_data, cost_model):
                market_data["prices"]["AAPL"] = 999.0
                cost_model["fee_bps"] = 999.0
                return [_sig("AAPL", Side.BUY, "mutator", weight=0.1)]

        observer = _RecordingStrategy([_sig("AAPL", Side.BUY, "observer", weight=0.1)])
        mgr = MultiStrategyManager()
        mgr.register("mutator", _Mutating(), allocation_pct=20.0)
        mgr.register("observer", observer, allocation_pct=20.0)

        await mgr.evaluate_all(market, costs)

        # Caller's originals untouched.
        assert market == {"prices": {"AAPL": 150.0}}
        assert costs == {"fee_bps": 5.0}
        # Sibling saw the original, unmutated data.
        observed_md, _observed_cm = observer.received[0]
        assert observed_md == {"prices": {"AAPL": 150.0}}

    async def test_strategies_receive_distinct_copies(self):
        a = _RecordingStrategy([_sig("AAPL", Side.BUY, "a", weight=0.1)])
        b = _RecordingStrategy([_sig("AAPL", Side.BUY, "b", weight=0.1)])
        mgr = MultiStrategyManager()
        mgr.register("a", a, allocation_pct=20.0)
        mgr.register("b", b, allocation_pct=20.0)

        await mgr.evaluate_all(_MARKET, _COSTS)

        a_md = a.received[0][0]
        b_md = b.received[0][0]
        assert a_md == b_md  # equal by value ...
        assert a_md is not b_md  # ... but independent objects

    async def test_sync_strategy_runs_in_event_loop_thread(self):
        # The sync path does NOT offload to a thread pool: it runs inline
        # in the event loop's thread.
        main_thread = threading.get_ident()
        seen: list[int] = []

        class _Probe:
            def evaluate(self, market_data, cost_model):
                seen.append(threading.get_ident())
                return [_sig("AAPL", Side.BUY, "probe", weight=0.1)]

        mgr = MultiStrategyManager()
        mgr.register("probe", _Probe(), allocation_pct=20.0)

        await mgr.evaluate_all(_MARKET, _COSTS)

        assert seen == [main_thread]


# --------------------------------------------------------------------- #
# Edge cases
# --------------------------------------------------------------------- #


class TestEdgeCases:
    async def test_non_finite_signal_weight_abstains_from_cap(self):
        # A NaN/Inf weight cannot scale an exposure, so it abstains from
        # the cap sum (matching the rest of the engine's guards).
        #
        # ``Signal.weight`` is constrained to [0, 1] by pydantic, so a
        # non-finite weight can only reach the manager via a strategy that
        # bypassed validation (``model_construct``). We simulate that here
        # to exercise the defensive guard in ``_enforce_cap``.
        inf_signal = Signal.model_construct(
            symbol="AAPL", side=Side.BUY, strategy_id="s1", weight=float("inf")
        )
        mgr = MultiStrategyManager()
        mgr.register(
            "s1",
            _RecordingStrategy(
                [
                    inf_signal,
                    _sig("MSFT", Side.BUY, "s1", weight=0.1),
                ]
            ),
            allocation_pct=20.0,
        )

        result = await mgr.evaluate_all(_MARKET, _COSTS)

        # The Inf-weight signal is not summed, so only 0.1 is active ->
        # under the 0.20 cap -> no scaling.
        assert result.allocation_adjustments == {}
        aapl = next(s for s in result.signals if s.symbol == "AAPL")
        msft = next(s for s in result.signals if s.symbol == "MSFT")
        # Inf is preserved (not mutated) since it abstained.
        assert math.isinf(aapl.weight)
        assert msft.weight == pytest.approx(0.1)

    async def test_strength_and_metadata_preserved_on_retag(self):
        mgr = MultiStrategyManager()
        original = Signal(
            symbol="AAPL",
            side=Side.BUY,
            strategy_id="orig",
            weight=0.1,
            strength=SignalStrength.STRONG,
            reason="earnings beat",
            metadata={"confidence": 0.9},
        )
        mgr.register("s1", _RecordingStrategy([original]), allocation_pct=20.0)

        result = await mgr.evaluate_all(_MARKET, _COSTS)

        sig = result.signals[0]
        assert sig.strength == SignalStrength.STRONG
        assert sig.reason == "earnings beat"
        # Original metadata merged with the allocation annotations.
        assert sig.metadata["confidence"] == 0.9
        assert "allocation_cap_pct" in sig.metadata

    async def test_unregistered_strategy_excluded_from_next_cycle(self):
        mgr = MultiStrategyManager()
        mgr.register(
            "keep",
            _RecordingStrategy([_sig("AAPL", Side.BUY, "keep", weight=0.1)]),
            allocation_pct=20.0,
        )
        mgr.register(
            "drop",
            _RecordingStrategy([_sig("MSFT", Side.SELL, "drop", weight=0.1)]),
            allocation_pct=20.0,
        )

        first = await mgr.evaluate_all(_MARKET, _COSTS)
        assert set(first.per_strategy_signals) == {"keep", "drop"}

        mgr.unregister("drop")
        second = await mgr.evaluate_all(_MARKET, _COSTS)
        assert set(second.per_strategy_signals) == {"keep"}
