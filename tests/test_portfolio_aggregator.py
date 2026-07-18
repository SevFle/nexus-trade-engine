"""Static-allocation tests for :class:`MultiStrategyPortfolio`.

The companion suite ``tests/test_multi_strategy_portfolio.py`` already
exercises signal merging, failure/timeout isolation and the no-op fixes.
This module narrows in on the *portfolio-as-aggregator* concern that the
task brief calls out, and is organised around its three guarantees:

1. **Capital distributed by weight** — each strategy's dollar allocation
   is its normalised weight x ``total_capital``, and the deployed dollars
   always sum back to ``total_capital`` (so capital is never created or
   destroyed by the split).
2. **Adding / removing a strategy recalculates allocations** — the
   registry is the source of truth, so every mutation (register /
   unregister / set_capital_weight) propagates into the allocation
   snapshot on the very next lookup. There is no stale cache.
3. **Proportional share edge cases** — relative weights need not sum to
   1.0; an equal-weight registry splits capital evenly, a single strategy
   owns 100%, and a zero-weight strategy owns nothing while its siblings
   absorb the freed share.

A final group drives the one previously-uncovered branch
(``_merge_symbol`` raising on an unsupported side) to 100% line coverage
of the module.

All tests are deterministic, synchronous, and independent — no I/O, no
global state, no shared fixtures across classes.
"""

from __future__ import annotations

import math

import pytest

from engine.core.signal import Side, Signal
from engine.portfolio.multi_strategy import (
    CombinedPosition,
    MultiStrategyPortfolio,
    MultiStrategyPortfolioError,
    PortfolioEvaluation,
)

# --------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------- #


def _signal(symbol: str, side: Side, strategy_id: str, *, weight: float = 1.0) -> Signal:
    return Signal(symbol=symbol, side=side, strategy_id=strategy_id, weight=weight)


class _DummyStrategy:
    """Minimal registered strategy. ``evaluate`` is never reached by the
    static-allocation tests (they never call ``evaluate_all``); it exists
    only to satisfy the ``_strategy_id`` contract."""

    def __init__(self, sid: str) -> None:
        self._id = sid

    @property
    def id(self) -> str:
        return self._id

    async def evaluate(self, market_data, cost_model) -> list[Signal]:  # pragma: no cover
        return [_signal("AAPL", Side.BUY, self._id)]


class _CostModel:
    """Trivial mutable stand-in for ``ICostModel``."""


CAPITAL = 1_000_000.0


def _build(*weights: float) -> MultiStrategyPortfolio:
    """Portfolio whose strategies ``s0..sN`` carry the given relative
    weights, in registration order."""
    pf = MultiStrategyPortfolio(total_capital=CAPITAL, cost_model=_CostModel())
    for i, w in enumerate(weights):
        pf.register(_DummyStrategy(f"s{i}"), capital_weight=w)
    return pf


# --------------------------------------------------------------------- #
# 1. Capital distributed by weight
# --------------------------------------------------------------------- #


class TestCapitalDistributedByWeight:
    """Guarantee (1): dollar allocations track weights exactly, sum to the
    book, and are never negative."""

    def test_weights_summing_to_one_map_directly_to_capital(self) -> None:
        # Weights that already sum to 1.0 -> allocations are weight x capital.
        pf = _build(0.6, 0.4)
        allocs = pf.allocations()
        assert allocs["s0"] == pytest.approx(CAPITAL * 0.6)
        assert allocs["s1"] == pytest.approx(CAPITAL * 0.4)

    def test_allocations_sum_back_to_total_capital(self) -> None:
        # No capital is created or destroyed by the split, regardless of
        # how weird the relative weights are.
        for weights in [(1.0, 1.0), (2.0, 1.0), (7.0, 3.0, 5.0), (1.0,), (0.5, 0.25, 0.25)]:
            pf = _build(*weights)
            assert sum(pf.allocations().values()) == pytest.approx(CAPITAL)

    def test_no_allocation_is_ever_negative(self) -> None:
        # The brief's validation requirement: never produce a negative
        # allocation, even with lopsided weights or zero-weight members.
        pf = _build(0.0, 9.0, 1.0)
        for dollars in pf.allocations().values():
            assert dollars >= 0.0
        # And the per-id lookup never goes negative either.
        assert all(pf.allocation(sid) >= 0.0 for sid in pf.strategy_ids)

    def test_allocation_respects_individual_weight_ratio(self) -> None:
        # a:b = 3:1 → a owns 75%, b owns 25%, exact to the cent.
        pf = _build(3.0, 1.0)
        assert pf.allocation("s0") == pytest.approx(CAPITAL * 0.75)
        assert pf.allocation("s1") == pytest.approx(CAPITAL * 0.25)
        assert pf.capital_weight_normalized("s0") == pytest.approx(0.75)
        assert pf.capital_weight_normalized("s1") == pytest.approx(0.25)

    def test_allocation_for_unknown_strategy_is_zero(self) -> None:
        pf = _build(1.0)
        # Total lookups: an unknown id resolves to 0.0, never a KeyError.
        assert pf.allocation("does-not-exist") == 0.0
        assert pf.capital_weight_normalized("does-not-exist") == 0.0


# --------------------------------------------------------------------- #
# 2. Adding / removing a strategy recalculates allocations
# --------------------------------------------------------------------- #


class TestAllocationRecalculatesOnMutation:
    """Guarantee (2): the registry is the single source of truth, so every
    mutation is reflected in the *next* allocation snapshot — there is no
    cached/stale split to refresh by hand."""

    def test_adding_strategy_reallocates_proportionally(self) -> None:
        pf = _build(1.0)  # s0 owns 100%.
        assert pf.allocation("s0") == pytest.approx(CAPITAL)

        pf.register(_DummyStrategy("s1"), capital_weight=1.0)  # now 50/50.
        allocs = pf.allocations()
        assert allocs["s0"] == pytest.approx(CAPITAL / 2)
        assert allocs["s1"] == pytest.approx(CAPITAL / 2)
        # s0's explicit dollar share dropped because the book was re-split.
        assert pf.allocation("s0") < CAPITAL

    def test_adding_strategy_with_unequal_weight_rebalances_book(self) -> None:
        pf = _build(1.0, 1.0)  # s0, s1 each 500_000.
        assert pf.allocation("s0") == pytest.approx(CAPITAL / 2)

        # Add s2 with weight 2 → new weights {1, 1, 2} → s2 owns half.
        pf.register(_DummyStrategy("s2"), capital_weight=2.0)
        allocs = pf.allocations()
        assert allocs["s0"] == pytest.approx(CAPITAL / 4)
        assert allocs["s1"] == pytest.approx(CAPITAL / 4)
        assert allocs["s2"] == pytest.approx(CAPITAL / 2)
        assert sum(allocs.values()) == pytest.approx(CAPITAL)

    def test_removing_strategy_returns_freed_capital_to_peers(self) -> None:
        pf = _build(1.0, 1.0, 2.0)  # {s0,s1,s2} = {250k, 250k, 500k}.
        assert pf.allocation("s2") == pytest.approx(CAPITAL / 2)

        assert pf.unregister("s2") is True
        # Remaining two equal-weight strategies split the whole book evenly.
        allocs = pf.allocations()
        assert allocs["s0"] == pytest.approx(CAPITAL / 2)
        assert allocs["s1"] == pytest.approx(CAPITAL / 2)
        # s2 is gone entirely.
        assert "s2" not in allocs
        assert pf.allocation("s2") == 0.0

    def test_removing_strategy_preserves_total_deployed_capital(self) -> None:
        pf = _build(3.0, 2.0, 1.0)
        before = sum(pf.allocations().values())
        assert before == pytest.approx(CAPITAL)

        pf.unregister("s1")
        after = sum(pf.allocations().values())
        # The survivors absorb the freed capital — total never leaks.
        assert after == pytest.approx(CAPITAL)
        # And the survivors' *relative* ratio (3:1) is unchanged.
        allocs = pf.allocations()
        assert allocs["s0"] / allocs["s2"] == pytest.approx(3.0)

    def test_unregistered_strategy_drops_to_zero_before_next_lookup(self) -> None:
        pf = _build(1.0, 1.0)
        assert pf.allocation("s1") == pytest.approx(CAPITAL / 2)

        pf.unregister("s1")
        # No recompute call needed — the next lookup reflects the removal.
        assert pf.allocation("s1") == 0.0
        assert pf.capital_weight_normalized("s1") == 0.0
        # The lone survivor now owns the whole book.
        assert pf.allocation("s0") == pytest.approx(CAPITAL)

    def test_set_capital_weight_reallocates_immediately(self) -> None:
        pf = _build(1.0, 1.0)
        assert pf.allocation("s0") == pytest.approx(CAPITAL / 2)

        pf.set_capital_weight("s0", 3.0)  # now {3, 1} → s0 owns 75%.
        assert pf.allocation("s0") == pytest.approx(CAPITAL * 0.75)
        assert pf.allocation("s1") == pytest.approx(CAPITAL * 0.25)

    def test_add_remove_add_cycle_restores_original_allocation(self) -> None:
        # A reversible mutation must leave allocations unchanged: proves
        # there is no drifting rounding error or stale residual.
        pf = _build(1.0, 1.0, 1.0)
        original = pf.allocations()
        assert original["s1"] == pytest.approx(CAPITAL / 3)

        pf.unregister("s1")
        pf.register(_DummyStrategy("s1"), capital_weight=1.0)
        restored = pf.allocations()
        assert restored["s0"] == pytest.approx(original["s0"])
        assert restored["s1"] == pytest.approx(original["s1"])
        assert restored["s2"] == pytest.approx(original["s2"])

    def test_unregister_unknown_strategy_leaves_allocations_intact(self) -> None:
        pf = _build(1.0, 1.0)
        before = pf.allocations()
        assert pf.unregister("ghost") is False
        assert pf.allocations() == before

    def test_unregister_all_leaves_empty_allocation(self) -> None:
        pf = _build(1.0, 1.0)
        pf.unregister("s0")
        pf.unregister("s1")
        assert len(pf) == 0
        assert pf.allocations() == {}
        assert pf.capital_weight_normalized("s0") == 0.0

    def test_registering_after_reaching_capacity_does_not_reallocate(self) -> None:
        pf = MultiStrategyPortfolio(
            total_capital=CAPITAL, cost_model=_CostModel(), max_strategies=2
        )
        pf.register(_DummyStrategy("s0"), capital_weight=1.0)
        pf.register(_DummyStrategy("s1"), capital_weight=1.0)
        snapshot = pf.allocations()
        # Over-capacity registration is rejected, so the split is untouched.
        with pytest.raises(MultiStrategyPortfolioError, match="max_strategies"):
            pf.register(_DummyStrategy("s2"), capital_weight=1.0)
        assert pf.allocations() == snapshot


# --------------------------------------------------------------------- #
# 3. Proportional-share edge cases
# --------------------------------------------------------------------- #


class TestProportionalShare:
    """Guarantee (3): the split is always *proportional* to relative
    weight, with sensible degenerate cases at the boundaries."""

    def test_single_strategy_owns_entire_capital(self) -> None:
        pf = _build(1.0)
        assert pf.allocation("s0") == pytest.approx(CAPITAL)
        assert pf.capital_weight_normalized("s0") == pytest.approx(1.0)

    def test_equal_weights_split_evenly_regardless_of_count(self) -> None:
        for n in (2, 3, 5, 10):
            pf = _build(*(1.0 for _ in range(n)))
            allocs = pf.allocations()
            share = CAPITAL / n
            assert set(allocs) == {f"s{i}" for i in range(n)}
            for dollars in allocs.values():
                assert dollars == pytest.approx(share)
            assert pf.capital_weight_normalized("s0") == pytest.approx(1 / n)

    def test_scaled_weights_give_identical_proportions(self) -> None:
        # {2, 1} and {200, 100} describe the same portfolio: relative
        # proportions, not absolute weights, drive the split.
        a = _build(2.0, 1.0).allocations()
        b = _build(200.0, 100.0).allocations()
        assert a["s0"] == pytest.approx(b["s0"])
        assert a["s1"] == pytest.approx(b["s1"])

    def test_zero_weight_strategy_gets_nothing_peers_absorb_rest(self) -> None:
        pf = _build(1.0, 0.0, 3.0)  # effective weights {1, 0, 3} → s2=75%.
        allocs = pf.allocations()
        assert allocs["s1"] == 0.0
        assert allocs["s0"] == pytest.approx(CAPITAL * 0.25)
        assert allocs["s2"] == pytest.approx(CAPITAL * 0.75)
        # The freed 0-weight share is reclaimed by the survivors.
        assert sum(allocs.values()) == pytest.approx(CAPITAL)

    def test_all_zero_weights_yields_zero_for_everyone(self) -> None:
        pf = _build(0.0, 0.0, 0.0)
        assert pf.allocations() == {"s0": 0.0, "s1": 0.0, "s2": 0.0}
        for sid in pf.strategy_ids:
            assert pf.capital_weight_normalized(sid) == 0.0

    def test_proportions_preserved_across_capital_levels(self) -> None:
        # Doubling the book doubles every dollar allocation, ratios fixed.
        small = _build(3.0, 1.0)
        big = MultiStrategyPortfolio(total_capital=CAPITAL * 2, cost_model=_CostModel())
        big.register(_DummyStrategy("s0"), capital_weight=3.0)
        big.register(_DummyStrategy("s1"), capital_weight=1.0)
        assert small.allocation("s0") / big.allocation("s0") == pytest.approx(0.5)
        assert small.allocation("s1") / big.allocation("s1") == pytest.approx(0.5)

    def test_weight_snapshot_is_a_defensive_copy(self) -> None:
        # Mutating the returned dict must not corrupt the registry.
        pf = _build(1.0, 2.0)
        weights = pf.capital_weights
        weights["s0"] = 999.0
        assert pf.get_capital_weight("s0") == 1.0
        assert pf.capital_weights["s0"] == 1.0

    def test_allocation_snapshot_is_a_defensive_copy(self) -> None:
        pf = _build(1.0, 2.0)
        allocs = pf.allocations()
        allocs["s0"] = -1.0
        # The live lookup is unaffected by external mutation.
        assert pf.allocation("s0") == pytest.approx(CAPITAL / 3)


# --------------------------------------------------------------------- #
# Allocation feeds the merged evaluation correctly
# --------------------------------------------------------------------- #


class _FixedSignals:
    """Strategy that returns a fixed signal list (used to confirm the
    static allocation actually drives ``evaluate_all``'s capital math)."""

    def __init__(self, sid: str, signals: list[Signal]) -> None:
        self._id = sid
        self._signals = signals

    @property
    def id(self) -> str:
        return self._id

    async def evaluate(self, market_data, cost_model) -> list[Signal]:
        return [s.model_copy() for s in self._signals]


class TestAllocationFeedsEvaluation:
    async def test_capital_deployed_equals_allocated_when_fully_invested(self) -> None:
        # Each strategy fully invests its allocation (weight=1.0 signal) on
        # disjoint symbols, so deployed capital must equal the allocation sum.
        pf = MultiStrategyPortfolio(total_capital=CAPITAL, cost_model=_CostModel())
        pf.register(_FixedSignals("s0", [_signal("AAA", Side.BUY, "s0")]), capital_weight=0.7)
        pf.register(_FixedSignals("s1", [_signal("BBB", Side.BUY, "s1")]), capital_weight=0.3)
        result = await pf.evaluate_all({})
        assert result.capital_deployed == pytest.approx(CAPITAL)
        assert result.capital_utilization == pytest.approx(1.0)
        # Each position's net exposure matches that strategy's allocation.
        assert result.positions["AAA"].net_exposure == pytest.approx(CAPITAL * 0.7)
        assert result.positions["BBB"].net_exposure == pytest.approx(CAPITAL * 0.3)

    async def test_reallocation_changes_next_evaluation_capital(self) -> None:
        # Removing a strategy mid-life recalculates allocations, so the
        # next evaluate_all deploys the new split (not the old one).
        pf = MultiStrategyPortfolio(total_capital=CAPITAL, cost_model=_CostModel())
        pf.register(_FixedSignals("s0", [_signal("AAA", Side.BUY, "s0")]), capital_weight=1.0)
        pf.register(_FixedSignals("s1", [_signal("BBB", Side.BUY, "s1")]), capital_weight=1.0)
        first = await pf.evaluate_all({})
        assert first.positions["AAA"].net_exposure == pytest.approx(CAPITAL / 2)

        pf.unregister("s1")
        second = await pf.evaluate_all({})
        # s0 now owns the whole book.
        assert second.positions["AAA"].net_exposure == pytest.approx(CAPITAL)
        assert "BBB" not in second.positions


# --------------------------------------------------------------------- #
# Coverage gap: unsupported side in the merge core
# --------------------------------------------------------------------- #


class TestUnsupportedSideGuard:
    """Lines 565-566: a signal whose ``side`` is not BUY/SELL/HOLD is
    rejected by the merge core rather than silently treated as an
    abstention. ``Signal`` validates ``side`` at construction, so the
    invalid value is injected via ``model_copy`` (which bypasses
    re-validation) — the same pattern used elsewhere for NaN weights."""

    async def test_unsupported_side_raises_portfolio_error(self) -> None:
        bad = _signal("AAPL", Side.BUY, "s0").model_copy(update={"side": "limit"})
        pf = MultiStrategyPortfolio(total_capital=CAPITAL, cost_model=_CostModel())
        pf.register(_FixedSignals("s0", [bad]))
        with pytest.raises(MultiStrategyPortfolioError, match="unsupported side"):
            await pf.evaluate_all({})


# --------------------------------------------------------------------- #
# Model defaults & dataclass invariants
# --------------------------------------------------------------------- #


class TestDataclassInvariants:
    def test_combined_position_is_frozen(self) -> None:
        pos = CombinedPosition(symbol="X", side=Side.HOLD, net_weight=0.0, net_exposure=0.0)
        with pytest.raises((AttributeError, Exception)):
            pos.symbol = "Y"  # type: ignore[misc]

    def test_portfolio_evaluation_is_frozen(self) -> None:
        ev = PortfolioEvaluation()
        with pytest.raises((AttributeError, Exception)):
            ev.total_capital = 1.0  # type: ignore[misc]

    def test_combined_position_contributors_default_empty(self) -> None:
        pos = CombinedPosition(symbol="X", side=Side.HOLD, net_weight=0.0, net_exposure=0.0)
        assert pos.contributors == []
        assert pos.signals == []

    def test_portfolio_evaluation_defaults_are_clean_noop(self) -> None:
        ev = PortfolioEvaluation()
        assert ev.signals == []
        assert ev.positions == {}
        assert ev.per_strategy_signals == {}
        assert ev.errors == {}
        assert ev.merge_mode == ""
        assert math.isclose(ev.net_exposure, 0.0)
        assert ev.is_noop is True
