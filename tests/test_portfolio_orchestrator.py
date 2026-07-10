"""Unit tests for :mod:`engine.portfolio.orchestrator`.

Covers happy-path aggregation, conflict resolution (net weighted vote),
zero-weight handling, empty-strategy-list edge case, async strategy
support, error isolation, and the shared-context contract.

NOTE: this targets the *portfolio* orchestrator. The sibling file
``tests/test_strategy_orchestrator.py`` covers the distinct
``engine.core.strategy_orchestrator`` and ``engine.orchestration``
orchestrators and is intentionally left untouched.
"""

from __future__ import annotations

from typing import Any

import pytest

from engine.core.signal import Side, Signal
from engine.portfolio.orchestrator import (
    ORCHESTRATED_STRATEGY_ID,
    IStrategy,
    SignalSet,
    StrategyOrchestrator,
    StrategyOrchestratorError,
)


# --------------------------------------------------------------------------- #
# Stub strategies
# --------------------------------------------------------------------------- #
class _Stub:
    """Minimal sync IStrategy that emits a fixed list of signals."""

    def __init__(self, sid: str, signals: list[Signal] | None = None) -> None:
        self.id = sid
        self.signals = list(signals or [])
        self.seen_contexts: list[int] = []

    def evaluate(self, market_context: Any) -> list[Signal]:
        self.seen_contexts.append(id(market_context))
        return list(self.signals)


class _AsyncStub(_Stub):
    async def evaluate(self, market_context: Any) -> list[Signal]:  # type: ignore[override]
        self.seen_contexts.append(id(market_context))
        return list(self.signals)


class _RaisingStub(_Stub):
    def evaluate(self, market_context: Any) -> list[Signal]:  # type: ignore[override]
        raise RuntimeError("boom")


class _CallableIdStub:
    """``id`` exposed as a no-arg method — exercises the callable branch."""

    def id(self) -> str:
        return "callable-id"

    def evaluate(self, market_context: Any) -> list[Signal]:
        return []


def _sig(symbol: str, side: Side, sid: str = "s") -> Signal:
    return Signal(symbol=symbol, side=side, strategy_id=sid)


def _by_symbol(result: SignalSet) -> dict[str, Signal]:
    return {s.symbol: s for s in result.signals}


# --------------------------------------------------------------------------- #
# Construction / registration
# --------------------------------------------------------------------------- #
class TestConstruction:
    def test_registers_with_weights(self):
        orch = StrategyOrchestrator([(_Stub("a"), 0.6), (_Stub("b"), 0.4)])
        assert len(orch) == 2
        assert orch.strategy_ids == ["a", "b"]
        assert orch.weights == {"a": 0.6, "b": 0.4}
        assert "a" in orch and "b" in orch

    def test_zero_weight_allowed(self):
        orch = StrategyOrchestrator([(_Stub("a"), 0.0)])
        assert orch.weights == {"a": 0.0}

    def test_callable_id_is_tolerated(self):
        orch = StrategyOrchestrator([(_CallableIdStub(), 1.0)])
        assert orch.strategy_ids == ["callable-id"]

    @pytest.mark.parametrize("bad_weight", [-0.1, float("nan"), float("inf"), "x", None])
    async def test_invalid_weight_rejected(self, bad_weight: Any):
        with pytest.raises(StrategyOrchestratorError):
            StrategyOrchestrator([(_Stub("a"), bad_weight)])  # type: ignore[arg-type]

    def test_missing_id_rejected(self):
        class _NoId:
            def evaluate(self, ctx: Any) -> list[Signal]:
                return []

        with pytest.raises(StrategyOrchestratorError):
            StrategyOrchestrator([(_NoId(), 1.0)])  # type: ignore[list-item]

    def test_missing_evaluate_rejected(self):
        class _NoEval:
            id = "x"

        with pytest.raises(StrategyOrchestratorError):
            StrategyOrchestrator([(_NoEval(), 1.0)])  # type: ignore[list-item]

    def test_duplicate_id_rejected(self):
        with pytest.raises(StrategyOrchestratorError, match="duplicate"):
            StrategyOrchestrator([(_Stub("a"), 0.5), (_Stub("a"), 0.5)])

    def test_malformed_entry_rejected(self):
        with pytest.raises(StrategyOrchestratorError):
            StrategyOrchestrator([(_Stub("a"),)])  # type: ignore[list-item]

    def test_non_list_rejected(self):
        with pytest.raises(StrategyOrchestratorError):
            StrategyOrchestrator((_Stub("a"), 1.0))  # type: ignore[arg-type]

    def test_istrategy_protocol_matches_stub(self):
        # runtime_checkable Protocol: structural isinstance check passes.
        assert isinstance(_Stub("a"), IStrategy)


# --------------------------------------------------------------------------- #
# Happy-path aggregation
# --------------------------------------------------------------------------- #
class TestHappyPathAggregation:
    async def test_unanimous_buy(self):
        orch = StrategyOrchestrator(
            [(_Stub("a", [_sig("AAPL", Side.BUY)]), 0.6),
             (_Stub("b", [_sig("AAPL", Side.BUY)]), 0.4)]
        )
        result = await orch.evaluate(object())
        aapl = _by_symbol(result)["AAPL"]
        assert aapl.side == Side.BUY
        assert aapl.strategy_id == ORCHESTRATED_STRATEGY_ID
        assert aapl.weight == pytest.approx(1.0)  # 0.6 + 0.4 clamped to 1.0
        assert result.strategy_count == 2
        assert result.breakdown["AAPL"]["side"] == "buy"

    async def test_multiple_symbols_resolved_independently(self):
        orch = StrategyOrchestrator(
            [(_Stub("a", [_sig("AAPL", Side.BUY), _sig("MSFT", Side.SELL)]), 1.0)]
        )
        result = await orch.evaluate(object())
        sym = _by_symbol(result)
        assert sym["AAPL"].side == Side.BUY
        assert sym["MSFT"].side == Side.SELL
        # deterministic (sorted) ordering of emitted signals
        assert [s.symbol for s in result.signals] == ["AAPL", "MSFT"]

    async def test_hold_abstains(self):
        # A votes BUY, B votes HOLD (abstain) -> BUY survives.
        orch = StrategyOrchestrator(
            [(_Stub("a", [_sig("AAPL", Side.BUY)]), 0.5),
             (_Stub("b", [_sig("AAPL", Side.HOLD)]), 0.5)]
        )
        result = await orch.evaluate(object())
        assert _by_symbol(result)["AAPL"].side == Side.BUY

    async def test_all_hold_emits_hold_record(self):
        orch = StrategyOrchestrator(
            [(_Stub("a", [_sig("AAPL", Side.HOLD)]), 1.0)]
        )
        result = await orch.evaluate(object())
        aapl = _by_symbol(result)["AAPL"]
        assert aapl.side == Side.HOLD
        assert result.trade_signals == []

    async def test_trade_signals_property(self):
        orch = StrategyOrchestrator(
            [(_Stub("a", [_sig("AAPL", Side.BUY), _sig("CASH", Side.HOLD)]), 1.0)]
        )
        result = await orch.evaluate(object())
        assert [s.symbol for s in result.trade_signals] == ["AAPL"]


# --------------------------------------------------------------------------- #
# Conflict resolution — strongest net weight wins
# --------------------------------------------------------------------------- #
class TestConflictResolution:
    async def test_strongest_net_weight_wins(self):
        # Heavy SELL (0.7) vs light BUY (0.2) -> net -0.5 -> SELL.
        orch = StrategyOrchestrator(
            [(_Stub("a", [_sig("AAPL", Side.SELL)]), 0.7),
             (_Stub("b", [_sig("AAPL", Side.BUY)]), 0.2)]
        )
        result = await orch.evaluate(object())
        aapl = _by_symbol(result)["AAPL"]
        assert aapl.side == Side.SELL
        assert aapl.weight == pytest.approx(0.5)  # abs(net)
        assert result.breakdown["AAPL"]["net"] == pytest.approx(-0.5)

    async def test_exact_tie_resolves_to_hold(self):
        orch = StrategyOrchestrator(
            [(_Stub("a", [_sig("AAPL", Side.BUY)]), 0.5),
             (_Stub("b", [_sig("AAPL", Side.SELL)]), 0.5)]
        )
        result = await orch.evaluate(object())
        assert _by_symbol(result)["AAPL"].side == Side.HOLD

    async def test_three_way_net(self):
        # BUY 0.5 + BUY 0.3 - SELL 0.4 = +0.4 -> BUY.
        orch = StrategyOrchestrator(
            [(_Stub("a", [_sig("X", Side.BUY)]), 0.5),
             (_Stub("b", [_sig("X", Side.BUY)]), 0.3),
             (_Stub("c", [_sig("X", Side.SELL)]), 0.4)]
        )
        result = await orch.evaluate(object())
        x = _by_symbol(result)["X"]
        assert x.side == Side.BUY
        assert x.weight == pytest.approx(0.4)

    async def test_weight_clamped_to_one(self):
        orch = StrategyOrchestrator(
            [(_Stub("a", [_sig("AAPL", Side.BUY)]), 0.9),
             (_Stub("b", [_sig("AAPL", Side.BUY)]), 0.9)]
        )
        result = await orch.evaluate(object())
        assert _by_symbol(result)["AAPL"].weight == 1.0  # min(1.8, 1.0)


# --------------------------------------------------------------------------- #
# Zero-weight handling
# --------------------------------------------------------------------------- #
class TestZeroWeight:
    async def test_zero_weight_cannot_flip_majority(self):
        # SELL (0.5) vs zero-weight BUY -> net -0.5 -> SELL.
        orch = StrategyOrchestrator(
            [(_Stub("a", [_sig("AAPL", Side.SELL)]), 0.5),
             (_Stub("b", [_sig("AAPL", Side.BUY)]), 0.0)]
        )
        result = await orch.evaluate(object())
        assert _by_symbol(result)["AAPL"].side == Side.SELL

    async def test_zero_weight_alone_resolves_to_hold(self):
        orch = StrategyOrchestrator([(_Stub("a", [_sig("AAPL", Side.BUY)]), 0.0)])
        result = await orch.evaluate(object())
        assert _by_symbol(result)["AAPL"].side == Side.HOLD  # net 0

    async def test_all_zero_weights(self):
        orch = StrategyOrchestrator(
            [(_Stub("a", [_sig("AAPL", Side.BUY)]), 0.0),
             (_Stub("b", [_sig("AAPL", Side.SELL)]), 0.0)]
        )
        result = await orch.evaluate(object())
        assert _by_symbol(result)["AAPL"].side == Side.HOLD


# --------------------------------------------------------------------------- #
# Empty list + error isolation
# --------------------------------------------------------------------------- #
class TestEmptyAndErrors:
    async def test_empty_strategy_list(self):
        orch = StrategyOrchestrator([])
        result = await orch.evaluate(object())
        assert result.is_empty
        assert result.signals == []
        assert result.strategy_count == 0
        assert result.errors == {}

    async def test_raising_strategy_isolated(self):
        orch = StrategyOrchestrator(
            [(_RaisingStub("bad"), 1.0),
             (_Stub("good", [_sig("AAPL", Side.BUY)]), 1.0)]
        )
        result = await orch.evaluate(object())
        assert _by_symbol(result)["AAPL"].side == Side.BUY
        assert "bad" in result.errors
        assert "RuntimeError" in result.errors["bad"]

    async def test_none_return_treated_as_no_signals(self):
        class _NoneStub(_Stub):
            def evaluate(self, ctx: Any) -> Any:  # type: ignore[override]
                return None

        orch = StrategyOrchestrator(
            [(_NoneStub("n"), 1.0),
             (_Stub("g", [_sig("AAPL", Side.BUY)]), 1.0)]
        )
        result = await orch.evaluate(object())
        assert _by_symbol(result)["AAPL"].side == Side.BUY
        assert result.errors == {}

    async def test_shared_market_context(self):
        # The task requires every strategy to see the SAME context object.
        a, b = _Stub("a", [_sig("AAPL", Side.BUY)]), _Stub("b", [_sig("AAPL", Side.BUY)])
        orch = StrategyOrchestrator([(a, 0.5), (b, 0.5)])
        ctx = {"market": "data"}
        await orch.evaluate(ctx)
        assert a.seen_contexts == [id(ctx)]
        assert b.seen_contexts == [id(ctx)]


# --------------------------------------------------------------------------- #
# Async strategy support
# --------------------------------------------------------------------------- #
class TestAsyncStrategies:
    async def test_async_evaluate_awaited(self):
        orch = StrategyOrchestrator(
            [(_AsyncStub("a", [_sig("AAPL", Side.BUY)]), 0.5),
             (_Stub("b", [_sig("AAPL", Side.BUY)]), 0.5)]
        )
        result = await orch.evaluate(object())
        assert _by_symbol(result)["AAPL"].side == Side.BUY
        assert result.strategy_count == 2

    async def test_mixed_sync_async_share_context(self):
        a, b = _AsyncStub("a", [_sig("AAPL", Side.BUY)]), _Stub("b", [_sig("AAPL", Side.BUY)])
        orch = StrategyOrchestrator([(a, 0.5), (b, 0.5)])
        ctx = object()
        await orch.evaluate(ctx)
        assert a.seen_contexts == [id(ctx)]
        assert b.seen_contexts == [id(ctx)]


def test_module_exports():
    # guard the public surface declared in __all__
    import engine.portfolio.orchestrator as mod

    for name in mod.__all__:
        assert hasattr(mod, name), f"{name} missing from module"
