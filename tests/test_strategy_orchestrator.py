"""Tests for engine.core.strategy_orchestrator.

Covers: empty-registry no-op, single-strategy pass-through (sync and
async), 3-strategy conflicting signals resolved by majority, weighted
override of a numerical majority, registration validation, failure
isolation, and aggregation-mode aliasing.
"""

from __future__ import annotations

import asyncio
import math

import pytest

from engine.core.signal import Side, Signal
from engine.core.signal_aggregator import AGGREGATED_STRATEGY_ID
from engine.core.strategy_orchestrator import (
    AggregationMode,
    OrchestrationResult,
    StrategyOrchestrator,
    StrategyOrchestratorError,
)

# --------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------- #


def _sig(symbol: str, side: Side, strategy_id: str, **kw) -> Signal:
    return Signal(symbol=symbol, side=side, strategy_id=strategy_id, **kw)


class _RecordingStrategy:
    """Strategy that returns a fixed signal list and records the exact
    market_data / cost_model objects it received (so tests can assert
    every strategy saw the *same* inputs)."""

    def __init__(self, sid: str, signals: list[Signal]) -> None:
        self._id = sid
        self._signals = signals
        self.received: list[tuple[object, object]] = []

    @property
    def id(self) -> str:
        return self._id

    async def evaluate(self, market_data, cost_model) -> list[Signal]:
        # Record object identity, not value, to prove same-input sharing.
        self.received.append((market_data, cost_model))
        return [s.model_copy() for s in self._signals]


class _SyncStrategy:
    """Sync (non-async) strategy variant to prove evaluate_all handles
    plain callables too."""

    def __init__(self, sid: str, signals: list[Signal]) -> None:
        self._id = sid
        self._signals = signals

    @property
    def id(self) -> str:
        return self._id

    def evaluate(self, market_data, cost_model) -> list[Signal]:
        return [s.model_copy() for s in self._signals]


class _CallableIdStrategy:
    """Strategy whose ``id`` is a method rather than a property."""

    def __init__(self, sid: str, signals: list[Signal]) -> None:
        self._id = sid
        self._signals = signals

    def id(self) -> str:  # type: ignore[override]
        return self._id

    async def evaluate(self, market_data, cost_model) -> list[Signal]:
        return [s.model_copy() for s in self._signals]


class _RaisingStrategy:
    def __init__(self, sid: str, exc: Exception) -> None:
        self._id = sid
        self._exc = exc

    @property
    def id(self) -> str:
        return self._id

    async def evaluate(self, market_data, cost_model) -> list[Signal]:
        raise self._exc


class _NoIdStrategy:
    async def evaluate(self, market_data, cost_model) -> list[Signal]:
        return []


class _NoEvaluateStrategy:
    id = "has-id-but-no-evaluate"


# Sentinel values passed as market_data / cost_model. These are
# deep-copyable, value-comparable dicts (not bare objects) so tests can
# assert that every strategy received an *equal* copy of the same source
# data while the orchestrator still guarantees each gets its own
# independent deep copy (mutation isolation).
_MARKET = {"prices": {"AAPL": 150.0, "MSFT": 300.0}}
_COSTS = {"fee_bps": 5.0, "spread_bps": 1.0}


# --------------------------------------------------------------------- #
# Registration & introspection
# --------------------------------------------------------------------- #


class TestRegistration:
    def test_register_default_weight_is_one(self):
        orch = StrategyOrchestrator()
        orch.register(_RecordingStrategy("s1", []))
        assert "s1" in orch
        assert len(orch) == 1
        assert orch.get_weight("s1") == 1.0
        assert orch.strategy_ids == ["s1"]

    def test_register_custom_weight(self):
        orch = StrategyOrchestrator()
        orch.register(_RecordingStrategy("s1", []), weight=3.5)
        assert orch.get_weight("s1") == 3.5
        assert orch.weights == {"s1": 3.5}

    def test_weights_property_returns_snapshot(self):
        orch = StrategyOrchestrator()
        orch.register(_RecordingStrategy("s1", []), weight=2.0)
        snap = orch.weights
        snap["s1"] = 999.0  # mutating the snapshot must not leak
        assert orch.get_weight("s1") == 2.0

    def test_reregister_updates_weight_and_strategy(self):
        orch = StrategyOrchestrator()
        first = _RecordingStrategy("s1", [])
        second = _RecordingStrategy("s1", [])
        orch.register(first, weight=1.0)
        orch.register(second, weight=5.0)
        assert len(orch) == 1
        assert orch.get_weight("s1") == 5.0

    def test_register_callable_id(self):
        orch = StrategyOrchestrator()
        orch.register(_CallableIdStrategy("s9", []))
        assert "s9" in orch

    def test_unregister(self):
        orch = StrategyOrchestrator()
        orch.register(_RecordingStrategy("s1", []))
        orch.register(_RecordingStrategy("s2", []))
        assert orch.unregister("s1") is True
        assert "s1" not in orch
        assert len(orch) == 1
        # Idempotent: removing again is a no-op returning False.
        assert orch.unregister("s1") is False
        assert orch.get_weight("s1") is None

    def test_get_weight_unregistered_is_none(self):
        orch = StrategyOrchestrator()
        assert orch.get_weight("nope") is None

    @pytest.mark.parametrize("bad", [-0.01, -1.0, float("nan"), float("inf"), float("-inf")])
    async def test_register_rejects_invalid_weight(self, bad):
        orch = StrategyOrchestrator()
        with pytest.raises(StrategyOrchestratorError):
            orch.register(_RecordingStrategy("s1", []), weight=bad)

    def test_register_rejects_non_numeric_weight(self):
        orch = StrategyOrchestrator()
        with pytest.raises(StrategyOrchestratorError):
            orch.register(_RecordingStrategy("s1", []), weight="heavy")  # type: ignore[arg-type]

    def test_register_rejects_strategy_without_id(self):
        orch = StrategyOrchestrator()
        with pytest.raises(StrategyOrchestratorError):
            orch.register(_NoIdStrategy())  # type: ignore[arg-type]

    def test_register_rejects_strategy_without_evaluate(self):
        orch = StrategyOrchestrator()
        with pytest.raises(StrategyOrchestratorError):
            orch.register(_NoEvaluateStrategy)  # type: ignore[arg-type]


# --------------------------------------------------------------------- #
# evaluate_all — core behaviour
# --------------------------------------------------------------------- #


class TestEvaluateAll:
    async def test_empty_registry_is_noop(self):
        orch = StrategyOrchestrator()
        result = await orch.evaluate_all(_MARKET, _COSTS)
        assert isinstance(result, OrchestrationResult)
        assert result.is_noop
        assert result.signals == []
        assert result.batches == []
        assert result.strategy_count == 0
        assert result.errors == {}
        assert result.aggregation == "majority"

    async def test_single_strategy_passthrough_async(self):
        sigs = [
            _sig("AAPL", Side.BUY, "s1"),
            _sig("MSFT", Side.SELL, "s1"),
        ]
        s1 = _RecordingStrategy("s1", sigs)
        orch = StrategyOrchestrator()
        orch.register(s1)

        result = await orch.evaluate_all(_MARKET, _COSTS)

        assert not result.is_noop
        assert {sig.symbol: sig.side for sig in result.signals} == {
            "AAPL": Side.BUY,
            "MSFT": Side.SELL,
        }
        # The lone strategy received the exact shared objects.
        assert s1.received == [(_MARKET, _COSTS)]
        # Aggregated signals are tagged as aggregated for the audit trail.
        assert all(sig.strategy_id == AGGREGATED_STRATEGY_ID for sig in result.signals)

    async def test_single_strategy_passthrough_sync(self):
        # A sync (non-async) evaluate must be handled identically.
        sigs = [_sig("AAPL", Side.HOLD, "s1")]
        orch = StrategyOrchestrator()
        orch.register(_SyncStrategy("s1", sigs))

        result = await orch.evaluate_all(_MARKET, _COSTS)

        assert len(result.signals) == 1
        assert result.signals[0].symbol == "AAPL"

    async def test_all_strategies_receive_same_inputs(self):
        # The whole point of the orchestrator: equal copies of the same
        # source market_data and cost_model reach every strategy. Each
        # strategy gets its own deep copy (see test_mutation_isolation),
        # so we assert value equality rather than object identity.
        a = _RecordingStrategy("a", [_sig("AAPL", Side.BUY, "a")])
        b = _RecordingStrategy("b", [_sig("AAPL", Side.BUY, "b")])
        c = _RecordingStrategy("c", [_sig("AAPL", Side.BUY, "c")])
        orch = StrategyOrchestrator()
        for s in (a, b, c):
            orch.register(s)

        await orch.evaluate_all(_MARKET, _COSTS)

        assert a.received == [(_MARKET, _COSTS)]
        assert b.received == [(_MARKET, _COSTS)]
        assert c.received == [(_MARKET, _COSTS)]

    async def test_majority_resolves_conflict_buy_wins(self):
        # 2 BUY vs 1 SELL -> BUY takes 2/3 > 50%.
        orch = StrategyOrchestrator()
        orch.register(_RecordingStrategy("b1", [_sig("AAPL", Side.BUY, "b1")]))
        orch.register(_RecordingStrategy("b2", [_sig("AAPL", Side.BUY, "b2")]))
        orch.register(_RecordingStrategy("s1", [_sig("AAPL", Side.SELL, "s1")]))

        result = await orch.evaluate_all(_MARKET, _COSTS, aggregation="majority")

        assert len(result.signals) == 1
        assert result.signals[0].symbol == "AAPL"
        assert result.signals[0].side == Side.BUY

    async def test_majority_sell_wins(self):
        orch = StrategyOrchestrator()
        orch.register(_RecordingStrategy("s1", [_sig("TSLA", Side.SELL, "s1")]))
        orch.register(_RecordingStrategy("s2", [_sig("TSLA", Side.SELL, "s2")]))
        orch.register(_RecordingStrategy("b1", [_sig("TSLA", Side.BUY, "b1")]))

        result = await orch.evaluate_all(_MARKET, _COSTS)

        assert result.signals[0].side == Side.SELL

    async def test_majority_tie_emits_hold(self):
        # 1 BUY vs 1 SELL -> neither > 50% -> HOLD.
        orch = StrategyOrchestrator()
        orch.register(_RecordingStrategy("b1", [_sig("AAPL", Side.BUY, "b1")]))
        orch.register(_RecordingStrategy("s1", [_sig("AAPL", Side.SELL, "s1")]))

        result = await orch.evaluate_all(_MARKET, _COSTS)

        assert len(result.signals) == 1
        assert result.signals[0].side == Side.HOLD

    async def test_hold_abstains_from_majority_denominator(self):
        # 1 BUY, 1 SELL, 1 HOLD: HOLD abstains -> 1 vs 1 tie -> HOLD.
        orch = StrategyOrchestrator()
        orch.register(_RecordingStrategy("b1", [_sig("AAPL", Side.BUY, "b1")]))
        orch.register(_RecordingStrategy("s1", [_sig("AAPL", Side.SELL, "s1")]))
        orch.register(_RecordingStrategy("h1", [_sig("AAPL", Side.HOLD, "h1")]))

        result = await orch.evaluate_all(_MARKET, _COSTS)

        assert result.signals[0].side == Side.HOLD

    async def test_per_symbol_independent_resolution(self):
        # Different symbols resolve independently in one pass.
        orch = StrategyOrchestrator()
        orch.register(
            _RecordingStrategy(
                "a", [_sig("AAPL", Side.BUY, "a"), _sig("MSFT", Side.SELL, "a")]
            )
        )
        orch.register(
            _RecordingStrategy(
                "b", [_sig("AAPL", Side.BUY, "b"), _sig("MSFT", Side.BUY, "b")]
            )
        )

        result = await orch.evaluate_all(_MARKET, _COSTS)

        by_symbol = {sig.symbol: sig.side for sig in result.signals}
        assert by_symbol == {"AAPL": Side.BUY, "MSFT": Side.HOLD}


class TestWeightedOverride:
    async def test_weighted_overrides_numerical_majority(self):
        # Numerical majority is BUY (2 vs 1), but the lone SELL strategy
        # carries enough weight to win under `weighted`.
        orch = StrategyOrchestrator()
        orch.register(_RecordingStrategy("b1", [_sig("AAPL", Side.BUY, "b1")]), weight=1.0)
        orch.register(_RecordingStrategy("b2", [_sig("AAPL", Side.BUY, "b2")]), weight=1.0)
        orch.register(_RecordingStrategy("s1", [_sig("AAPL", Side.SELL, "s1")]), weight=5.0)

        majority = await orch.evaluate_all(_MARKET, _COSTS, aggregation="majority")
        weighted = await orch.evaluate_all(_MARKET, _COSTS, aggregation="weighted")

        # Majority mode ignores weights -> BUY wins on a 2-vs-1 count.
        assert majority.signals[0].side == Side.BUY
        # Weighted mode -> SELL total weight 5.0 beats BUY total 2.0.
        assert weighted.signals[0].side == Side.SELL
        assert weighted.aggregation == "weighted"
        assert weighted.weights["s1"] == 5.0

    async def test_weighted_tie_emits_hold(self):
        orch = StrategyOrchestrator()
        orch.register(_RecordingStrategy("b1", [_sig("AAPL", Side.BUY, "b1")]), weight=2.0)
        orch.register(_RecordingStrategy("s1", [_sig("AAPL", Side.SELL, "s1")]), weight=2.0)

        result = await orch.evaluate_all(_MARKET, _COSTS, aggregation="weighted")

        assert result.signals[0].side == Side.HOLD

    async def test_weighted_default_weight_for_unset_is_one(self):
        # Unregistered-weight strategies still vote with weight 1.0 in
        # weighted mode, so two equal BUYs beat one SELL.
        orch = StrategyOrchestrator()
        orch.register(_RecordingStrategy("b1", [_sig("AAPL", Side.BUY, "b1")]))
        orch.register(_RecordingStrategy("b2", [_sig("AAPL", Side.BUY, "b2")]))
        orch.register(
            _RecordingStrategy("s1", [_sig("AAPL", Side.SELL, "s1")]), weight=1.5
        )

        result = await orch.evaluate_all(_MARKET, _COSTS, aggregation="weighted")

        # BUY 1.0 + 1.0 = 2.0 vs SELL 1.5 -> BUY.
        assert result.signals[0].side == Side.BUY


class TestAggregationModeHandling:
    async def test_majority_and_majority_vote_are_aliases(self):
        orch = StrategyOrchestrator()
        orch.register(_RecordingStrategy("b1", [_sig("AAPL", Side.BUY, "b1")]))
        orch.register(_RecordingStrategy("b2", [_sig("AAPL", Side.BUY, "b2")]))
        orch.register(_RecordingStrategy("s1", [_sig("AAPL", Side.SELL, "s1")]))

        a = await orch.evaluate_all(_MARKET, _COSTS, aggregation="majority")
        b = await orch.evaluate_all(_MARKET, _COSTS, aggregation="majority_vote")

        assert [s.side for s in a.signals] == [s.side for s in b.signals]
        assert b.aggregation == "majority_vote"

    async def test_default_aggregation_is_majority(self):
        orch = StrategyOrchestrator()
        orch.register(_RecordingStrategy("b1", [_sig("AAPL", Side.BUY, "b1")]))
        result = await orch.evaluate_all(_MARKET, _COSTS)
        assert result.aggregation == AggregationMode.MAJORITY.value

    async def test_enum_accepted_as_mode(self):
        orch = StrategyOrchestrator()
        orch.register(_RecordingStrategy("b1", [_sig("AAPL", Side.BUY, "b1")]))
        result = await orch.evaluate_all(_MARKET, _COSTS, aggregation=AggregationMode.WEIGHTED)
        assert result.aggregation == "weighted"

    async def test_unknown_aggregation_mode_raises(self):
        orch = StrategyOrchestrator()
        orch.register(_RecordingStrategy("b1", []))
        with pytest.raises(StrategyOrchestratorError):
            await orch.evaluate_all(_MARKET, _COSTS, aggregation="plurality")


# --------------------------------------------------------------------- #
# Robustness
# --------------------------------------------------------------------- #


class TestFailureIsolation:
    async def test_one_strategy_raising_does_not_kill_orchestration(self):
        orch = StrategyOrchestrator()
        good = _RecordingStrategy("good", [_sig("AAPL", Side.BUY, "good")])
        bad = _RaisingStrategy("bad", RuntimeError("boom"))
        orch.register(good)
        orch.register(bad)

        result = await orch.evaluate_all(_MARKET, _COSTS)

        # The healthy strategy still contributed and the symbol resolves.
        assert any(s.symbol == "AAPL" for s in result.signals)
        # The failure is recorded, not swallowed.
        assert "bad" in result.errors
        assert "RuntimeError" in result.errors["bad"]
        # Only the good strategy produced a batch.
        assert [b.strategy_id for b in result.batches] == ["good"]
        assert result.strategy_count == 2  # both were registered

    async def test_strategy_returning_none_treated_as_empty(self):
        class _NoneStrategy:
            id = "none"

            async def evaluate(self, market_data, cost_model):
                return None

        orch = StrategyOrchestrator()
        orch.register(_RecordingStrategy("a", [_sig("AAPL", Side.BUY, "a")]))
        orch.register(_NoneStrategy())  # type: ignore[arg-type]

        result = await orch.evaluate_all(_MARKET, _COSTS)

        assert [b.strategy_id for b in result.batches] == ["a", "none"]
        assert result.batches[1].signals == []
        assert result.signals[0].side == Side.BUY


class TestResultShape:
    async def test_result_records_batches_and_metadata(self):
        orch = StrategyOrchestrator()
        orch.register(_RecordingStrategy("a", [_sig("AAPL", Side.BUY, "a")]), weight=2.0)
        orch.register(_RecordingStrategy("b", [_sig("AAPL", Side.BUY, "b")]), weight=3.0)

        result = await orch.evaluate_all(_MARKET, _COSTS, aggregation="weighted")

        assert result.strategy_count == 2
        assert {b.strategy_id for b in result.batches} == {"a", "b"}
        assert math.isclose(result.weights["a"], 2.0)
        assert math.isclose(result.weights["b"], 3.0)
        # trade_signals excludes any HOLD outcome.
        assert all(s.side != Side.HOLD for s in result.trade_signals)
        assert len(result.trade_signals) == 1

    async def test_all_hold_still_emits_hold_signal(self):
        # When every strategy abstains, the aggregator emits a single
        # HOLD so downstream code has a record the symbol was considered.
        orch = StrategyOrchestrator()
        orch.register(_RecordingStrategy("a", [_sig("AAPL", Side.HOLD, "a")]))
        orch.register(_RecordingStrategy("b", [_sig("AAPL", Side.HOLD, "b")]))

        result = await orch.evaluate_all(_MARKET, _COSTS)

        assert len(result.signals) == 1
        assert result.signals[0].side == Side.HOLD
        # HOLD-only is not a no-op (no-op == empty result).
        assert not result.is_noop
        assert result.trade_signals == []


# --------------------------------------------------------------------- #
# Robustness fixes: registry mutation during evaluation, per-strategy
# timeouts, and mutation isolation between strategies.
# --------------------------------------------------------------------- #


class TestRegistryMutationDuringEvaluate:
    async def test_register_during_evaluate_does_not_crash(self):
        # Without a snapshot, a strategy that registers a sibling while
        # the orchestrator is iterating raises "dictionary changed size
        # during iteration". The snapshot keeps the cycle stable; the
        # late sibling is picked up on the *next* cycle.
        orch = StrategyOrchestrator()

        class _RegisteringStrategy:
            id = "early"

            async def evaluate(self, market_data, cost_model):
                # Mutate the registry mid-cycle.
                orch.register(
                    _RecordingStrategy(
                        "late", [_sig("AAPL", Side.SELL, "late")]
                    )
                )
                return [_sig("AAPL", Side.BUY, "early")]

        orch.register(_RegisteringStrategy())

        result = await orch.evaluate_all(_MARKET, _COSTS)

        # The cycle completed instead of raising.
        assert [b.strategy_id for b in result.batches] == ["early"]
        # The registration took effect for the registry...
        assert "late" in orch
        # ...but the latecomer did not run this cycle.
        assert "late" not in {b.strategy_id for b in result.batches}

    async def test_unregister_during_evaluate_does_not_crash(self):
        # Symmetric: unregistering a sibling mid-cycle must not raise.
        orch = StrategyOrchestrator()

        class _UnregisteringStrategy:
            id = "keeper"

            async def evaluate(self, market_data, cost_model):
                orch.unregister("victim")
                return [_sig("AAPL", Side.BUY, "keeper")]

        orch.register(_UnregisteringStrategy())
        orch.register(_RecordingStrategy("victim", [_sig("AAPL", Side.SELL, "victim")]))

        result = await orch.evaluate_all(_MARKET, _COSTS)

        # Both ran (snapshot captured both before the mutation).
        assert {b.strategy_id for b in result.batches} == {"keeper", "victim"}
        assert "victim" not in orch  # removal took effect afterwards


class TestStrategyTimeout:
    async def test_slow_async_strategy_times_out_and_is_recorded(self):
        # A strategy whose evaluate() exceeds the configured timeout is
        # cancelled and recorded as a TimeoutError error result; the
        # remaining strategies still contribute.
        orch = StrategyOrchestrator(eval_timeout=0.05)
        good = _RecordingStrategy("good", [_sig("AAPL", Side.BUY, "good")])

        class _SlowStrategy:
            id = "slow"

            async def evaluate(self, market_data, cost_model):
                # Far beyond the 0.05s cap; wait_for cancels us.
                await asyncio.sleep(5.0)
                return [_sig("AAPL", Side.BUY, "slow")]  # never reached

        orch.register(good)
        orch.register(_SlowStrategy())

        result = await orch.evaluate_all(_MARKET, _COSTS)

        # Timeout surfaced as a distinct error entry.
        assert "slow" in result.errors
        assert "TimeoutError" in result.errors["slow"]
        assert "0.05" in result.errors["slow"]
        # The healthy strategy still produced a batch and a decision.
        assert [b.strategy_id for b in result.batches] == ["good"]
        assert result.signals[0].side == Side.BUY
        assert result.strategy_count == 2  # both registered

    async def test_default_timeout_is_thirty_seconds(self):
        orch = StrategyOrchestrator()
        assert math.isclose(orch._eval_timeout, 30.0)

    @pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), float("-inf")])
    def test_constructor_rejects_invalid_timeout(self, bad):
        with pytest.raises(StrategyOrchestratorError):
            StrategyOrchestrator(eval_timeout=bad)

    def test_constructor_rejects_non_numeric_timeout(self):
        with pytest.raises(StrategyOrchestratorError):
            StrategyOrchestrator(eval_timeout="soon")  # type: ignore[arg-type]


class TestMutationIsolation:
    async def test_each_strategy_receives_its_own_deep_copy(self):
        # A strategy that mutates the market_data / cost_model handed to
        # it must not poison its siblings or the caller's originals.
        market = {"prices": {"AAPL": 150.0}}
        costs = {"fee_bps": 5.0}

        class _MutatingStrategy:
            id = "mutator"

            async def evaluate(self, market_data, cost_model):
                # Mutate the copy we received in place.
                market_data["prices"]["AAPL"] = 999.0
                cost_model["fee_bps"] = 999.0
                return [_sig("AAPL", Side.BUY, "mutator")]

        observer = _RecordingStrategy(
            "observer", [_sig("AAPL", Side.BUY, "observer")]
        )

        orch = StrategyOrchestrator()
        orch.register(_MutatingStrategy())
        orch.register(observer)

        await orch.evaluate_all(market, costs)

        # The caller's originals are untouched.
        assert market == {"prices": {"AAPL": 150.0}}
        assert costs == {"fee_bps": 5.0}
        # The sibling observed the original, unmutated data.
        observed_md, observed_cm = observer.received[0]
        assert observed_md == {"prices": {"AAPL": 150.0}}
        assert observed_cm == {"fee_bps": 5.0}

    async def test_strategies_receive_distinct_copies(self):
        # Even when no mutation happens, the objects handed to two
        # strategies are distinct (proving the copy is per-strategy, not
        # a single shared deepcopy).
        a = _RecordingStrategy("a", [_sig("AAPL", Side.BUY, "a")])
        b = _RecordingStrategy("b", [_sig("AAPL", Side.BUY, "b")])
        orch = StrategyOrchestrator()
        orch.register(a)
        orch.register(b)

        await orch.evaluate_all(_MARKET, _COSTS)

        a_md = a.received[0][0]
        b_md = b.received[0][0]
        assert a_md == b_md  # equal by value ...
        assert a_md is not b_md  # ... but independent objects
