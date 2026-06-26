"""Tests for engine.core.strategy_orchestrator.

Covers: empty-registry no-op, single-strategy pass-through (sync and
async), 3-strategy conflicting signals resolved by majority, weighted
override of a numerical majority, registration validation, failure
isolation, and aggregation-mode aliasing.
"""

from __future__ import annotations

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


# Sentinel objects passed as market_data / cost_model so tests can verify
# every strategy received the *same* object by identity.
_MARKET = object()
_COSTS = object()


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
        # The whole point of the orchestrator: identical market_data and
        # cost_model reach every strategy by object identity.
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
