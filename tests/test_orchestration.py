"""Tests for engine.orchestration — multi-strategy orchestration & resolution.

Covers the four required scenarios plus the full public surface of
:class:`~engine.orchestration.orchestrator.StrategyOrchestrator`:

* single-strategy passthrough (sync + async),
* first-wins (highest-priority) conflict resolution,
* priority ordering & top-priority stalemate → HOLD,
* NET_POSITION vote resolution (positive/negative/zero net, non-finite
  weight abstention),
* no-signal case,
* deduplication and per-strategy failure / timeout isolation,
* config validation (unknown conflict policy, duplicate id, bad priority),
* and the **robustness fixes** required by gh#1093:

  1. an unknown ``Side`` raises ``StrategyOrchestratorError`` (the
     *correct* exception type) rather than a generic error,
  2. ``asyncio.TimeoutError`` from ``asyncio.wait_for`` is caught and
     logged as ``orchestrator.strategy_timeout`` while the remaining
     strategies still contribute,
  3. a strategy that raises the **builtin** ``TimeoutError`` propagates
     to the generic handler and is logged as ``orchestrator.strategy_failed``
     (never masquerading as a timeout),
  4. a signal whose ``metadata`` is ``None`` resolves to an empty dict
     instead of crashing with ``TypeError`` during aggregation.
"""

from __future__ import annotations

import asyncio

import pytest
from structlog.testing import capture_logs

from engine.core.signal import Side, Signal
from engine.orchestration.orchestrator import (
    ConflictResolution,
    StrategyOrchestrator,
    StrategyOrchestratorError,
)

# --------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------- #


class _FakeCostModel:
    """The orchestrator only *forwards* the cost model to strategies; it
    never invokes cost-model methods itself, so a bare object suffices."""


class _AsyncStrategy:
    """Minimal async strategy: returns a canned list of signals."""

    def __init__(self, sid: str, signals: list[Signal] | None = None) -> None:
        self._id = sid
        self._signals = list(signals or [])

    @property
    def id(self) -> str:
        return self._id

    async def evaluate(self, market_data, cost_model) -> list[Signal]:
        return [s.model_copy() for s in self._signals]


class _RaisingAsyncStrategy:
    """Async strategy whose evaluate raises ``exc``."""

    def __init__(self, sid: str, exc: BaseException) -> None:
        self._id = sid
        self._exc = exc

    @property
    def id(self) -> str:
        return self._id

    async def evaluate(self, market_data, cost_model) -> list[Signal]:
        raise self._exc


class _SyncStrategy:
    """Sync (non-async) strategy variant."""

    def __init__(self, sid: str, signals: list[Signal] | None = None) -> None:
        self._id = sid
        self._signals = list(signals or [])

    @property
    def id(self) -> str:
        return self._id

    def evaluate(self, market_data, cost_model) -> list[Signal]:
        return [s.model_copy() for s in self._signals]


class _RaisingSyncStrategy:
    """Sync strategy whose evaluate raises ``exc``."""

    def __init__(self, sid: str, exc: BaseException) -> None:
        self._id = sid
        self._exc = exc

    @property
    def id(self) -> str:
        return self._id

    def evaluate(self, market_data, cost_model) -> list[Signal]:
        raise self._exc


def _sig(symbol: str, side: Side, strategy_id: str, **kw) -> Signal:
    return Signal(symbol=symbol, side=side, strategy_id=strategy_id, **kw)


# --------------------------------------------------------------------- #
# Construction & introspection
# --------------------------------------------------------------------- #


class TestConstruction:
    def test_default_conflict_resolution_is_priority(self):
        orch = StrategyOrchestrator([], _FakeCostModel())
        assert orch.strategy_ids == []
        assert len(orch) == 0
        assert "anything" not in orch

    def test_register_and_introspection(self):
        orch = StrategyOrchestrator([], _FakeCostModel())
        orch.register(_AsyncStrategy("s1"), priority=2.0)
        assert "s1" in orch
        assert len(orch) == 1
        assert orch.strategy_ids == ["s1"]
        assert orch.get_priority("s1") == 2.0

    def test_get_priority_unregistered_is_default(self):
        orch = StrategyOrchestrator([], _FakeCostModel())
        assert orch.get_priority("nope") == 0.0

    def test_unregister(self):
        orch = StrategyOrchestrator([], _FakeCostModel())
        orch.register(_AsyncStrategy("s1"))
        assert orch.unregister("s1") is True
        assert "s1" not in orch
        assert orch.get_priority("s1") == 0.0
        # Idempotent.
        assert orch.unregister("s1") is False

    def test_priorities_override_applied_after_register(self):
        orch = StrategyOrchestrator(
            [_AsyncStrategy("s1", [])], _FakeCostModel(), priorities={"s1": 7.0}
        )
        assert orch.get_priority("s1") == 7.0

    def test_priority_for_unknown_strategy_rejected(self):
        with pytest.raises(StrategyOrchestratorError):
            StrategyOrchestrator([], _FakeCostModel(), priorities={"ghost": 1.0})


# --------------------------------------------------------------------- #
# Config validation
# --------------------------------------------------------------------- #


class TestConfigValidation:
    def test_unknown_conflict_policy_raises(self):
        with pytest.raises(StrategyOrchestratorError):
            StrategyOrchestrator([], _FakeCostModel(), conflict_resolution="bogus")

    def test_duplicate_strategy_id_rejected(self):
        with pytest.raises(StrategyOrchestratorError):
            StrategyOrchestrator(
                [_AsyncStrategy("s1"), _AsyncStrategy("s1")], _FakeCostModel()
            )

    def test_register_accepts_negative_priority(self):
        # Priority is an ordering key, not a magnitude: negative values are
        # valid and simply rank lower than the 0.0 default. Only
        # non-finite values are rejected.
        orch = StrategyOrchestrator([], _FakeCostModel())
        orch.register(_AsyncStrategy("s1"), priority=-0.5)
        assert orch.get_priority("s1") == -0.5

    def test_register_rejects_non_finite_priority(self):
        orch = StrategyOrchestrator([], _FakeCostModel())
        with pytest.raises(StrategyOrchestratorError):
            orch.register(_AsyncStrategy("s1"), priority=float("nan"))

    def test_register_rejects_non_numeric_priority(self):
        orch = StrategyOrchestrator([], _FakeCostModel())
        with pytest.raises(StrategyOrchestratorError):
            orch.register(_AsyncStrategy("s1"), priority="high")  # type: ignore[arg-type]

    def test_strategy_without_id_rejected(self):
        class _NoId:
            async def evaluate(self, md, cm):
                return []

        with pytest.raises(StrategyOrchestratorError):
            StrategyOrchestrator([_NoId()], _FakeCostModel())  # type: ignore[arg-type]

    def test_strategy_without_evaluate_rejected(self):
        class _NoEval:
            id = "no-eval"

        with pytest.raises(StrategyOrchestratorError):
            StrategyOrchestrator([_NoEval()], _FakeCostModel())  # type: ignore[arg-type]

    def test_strategy_with_callable_id_accepted(self):
        # A strategy whose ``id`` is a no-arg callable (rather than a
        # string attribute) is resolved by invoking it.
        class _CallableId:
            def id(self) -> str:  # type: ignore[override]
                return "callable"

            def evaluate(self, md, cm):
                return []

        orch = StrategyOrchestrator([_CallableId()], _FakeCostModel())
        assert "callable" in orch
        assert orch.strategy_ids == ["callable"]


# --------------------------------------------------------------------- #
# run_all + aggregate_signals: core behaviour
# --------------------------------------------------------------------- #


class TestSingleStrategyPassthrough:
    async def test_async_passthrough(self):
        sigs = [_sig("AAPL", Side.BUY, "s1"), _sig("MSFT", Side.SELL, "s1")]
        orch = StrategyOrchestrator([_AsyncStrategy("s1", sigs)], _FakeCostModel())

        raw = await orch.run_all({})

        assert [s.symbol for s in raw] == ["AAPL", "MSFT"]
        agg = orch.aggregate_signals()
        assert {s.symbol: s.side for s in agg} == {"AAPL": Side.BUY, "MSFT": Side.SELL}

    async def test_sync_passthrough(self):
        sigs = [_sig("AAPL", Side.HOLD, "s1")]
        orch = StrategyOrchestrator([_SyncStrategy("s1", sigs)], _FakeCostModel())

        raw = await orch.run_all({})

        assert [s.symbol for s in raw] == ["AAPL"]
        assert orch.aggregate_signals()[0].side == Side.HOLD

    async def test_non_list_return_normalized_to_empty(self):
        class _WeirdReturn:
            id = "weird"

            def evaluate(self, md, cm):
                return "not a list"  # falsy-checked but iterable...

        # An empty/None result is treated as "no signals"; a non-iterable
        # truthy value would only matter if `collected.extend(raw)` ran.
        class _NoneReturn:
            id = "none"

            def evaluate(self, md, cm):
                return None

        orch = StrategyOrchestrator([_NoneReturn()], _FakeCostModel())
        assert await orch.run_all({}) == []

    async def test_run_all_returns_flattened_raw_signals(self):
        orch = StrategyOrchestrator(
            [
                _AsyncStrategy("a", [_sig("AAPL", Side.BUY, "a")]),
                _AsyncStrategy("b", [_sig("MSFT", Side.SELL, "b")]),
            ],
            _FakeCostModel(),
        )
        raw = await orch.run_all({})
        assert {s.strategy_id for s in raw} == {"a", "b"}


class TestPriorityResolution:
    async def test_higher_priority_wins_conflict(self):
        orch = StrategyOrchestrator(
            [
                _AsyncStrategy("low", [_sig("AAPL", Side.BUY, "low")]),
                _AsyncStrategy("high", [_sig("AAPL", Side.SELL, "high")]),
            ],
            _FakeCostModel(),
            priorities={"low": 1.0, "high": 9.0},
        )
        await orch.run_all({})
        agg = orch.aggregate_signals()
        assert agg[0].symbol == "AAPL"
        assert agg[0].side == Side.SELL  # high priority wins

    async def test_first_wins_on_equal_priority_stalemate(self):
        # Two strategies tied for top priority on opposing sides → HOLD.
        orch = StrategyOrchestrator(
            [
                _AsyncStrategy("s1", [_sig("AAPL", Side.BUY, "s1")]),
                _AsyncStrategy("s2", [_sig("AAPL", Side.SELL, "s2")]),
            ],
            _FakeCostModel(),
            priorities={"s1": 5.0, "s2": 5.0},
        )
        await orch.run_all({})
        assert orch.aggregate_signals()[0].side == Side.HOLD

    async def test_priority_uses_registered_default(self):
        # Strategies registered without an explicit priority share the
        # 0.0 default, so a tie on opposing sides resolves to HOLD.
        orch = StrategyOrchestrator(
            [
                _AsyncStrategy("s1", [_sig("AAPL", Side.BUY, "s1")]),
                _AsyncStrategy("s2", [_sig("AAPL", Side.SELL, "s2")]),
            ],
            _FakeCostModel(),
        )
        await orch.run_all({})
        assert orch.aggregate_signals()[0].side == Side.HOLD

    async def test_hold_abstains_in_priority(self):
        orch = StrategyOrchestrator(
            [
                _AsyncStrategy("h", [_sig("AAPL", Side.HOLD, "h")]),
                _AsyncStrategy("b", [_sig("AAPL", Side.BUY, "b")]),
            ],
            _FakeCostModel(),
            priorities={"h": 9.0, "b": 1.0},
        )
        await orch.run_all({})
        # HOLD abstains even when high priority → the lone BUY wins.
        assert orch.aggregate_signals()[0].side == Side.BUY

    async def test_all_hold_emits_single_hold(self):
        orch = StrategyOrchestrator(
            [
                _AsyncStrategy("a", [_sig("AAPL", Side.HOLD, "a")]),
                _AsyncStrategy("b", [_sig("AAPL", Side.HOLD, "b")]),
            ],
            _FakeCostModel(),
        )
        await orch.run_all({})
        agg = orch.aggregate_signals()
        assert len(agg) == 1
        assert agg[0].side == Side.HOLD


class TestNetPositionResolution:
    def _orch(self, strategies):
        return StrategyOrchestrator(
            strategies, _FakeCostModel(), conflict_resolution=ConflictResolution.NET_POSITION
        )

    async def test_positive_net_resolves_buy(self):
        orch = self._orch(
            [
                _AsyncStrategy("b1", [_sig("AAPL", Side.BUY, "b1")]),
                _AsyncStrategy("b2", [_sig("AAPL", Side.BUY, "b2")]),
                _AsyncStrategy("s1", [_sig("AAPL", Side.SELL, "s1")]),
            ]
        )
        await orch.run_all({})
        agg = orch.aggregate_signals()
        assert agg[0].side == Side.BUY
        # Net magnitude is clamped to [0, 1]: 1 + 1 - 1 = 1.0.
        assert agg[0].weight == pytest.approx(1.0)

    async def test_negative_net_resolves_sell(self):
        orch = self._orch(
            [
                _AsyncStrategy("s1", [_sig("AAPL", Side.SELL, "s1")]),
                _AsyncStrategy("s2", [_sig("AAPL", Side.SELL, "s2")]),
                _AsyncStrategy("b1", [_sig("AAPL", Side.BUY, "b1")]),
            ]
        )
        await orch.run_all({})
        agg = orch.aggregate_signals()
        assert agg[0].side == Side.SELL
        assert agg[0].weight == pytest.approx(1.0)

    async def test_zero_net_resolves_hold_with_zero_weight(self):
        orch = self._orch(
            [
                _AsyncStrategy("b1", [_sig("AAPL", Side.BUY, "b1")]),
                _AsyncStrategy("s1", [_sig("AAPL", Side.SELL, "s1")]),
            ]
        )
        await orch.run_all({})
        agg = orch.aggregate_signals()
        assert agg[0].side == Side.HOLD
        assert agg[0].weight == pytest.approx(0.0)

    async def test_non_finite_weight_abstains(self):
        # A nan/inf weighted vote cannot contribute a magnitude and is
        # treated as an abstention on BOTH the BUY and SELL branches; the
        # lone finite BUY then wins outright.
        nan_buy = _sig("AAPL", Side.BUY, "nan", weight=1.0).model_copy(
            update={"weight": float("nan")}
        )
        inf_sell = _sig("AAPL", Side.SELL, "inf", weight=1.0).model_copy(
            update={"weight": float("inf")}
        )
        orch = self._orch(
            [
                _AsyncStrategy("nan", [nan_buy]),
                _AsyncStrategy("inf", [inf_sell]),
                _AsyncStrategy("b1", [_sig("AAPL", Side.BUY, "b1")]),
            ]
        )
        await orch.run_all({})
        agg = orch.aggregate_signals()
        # Both non-finite voters abstain → lone finite BUY wins.
        assert agg[0].side == Side.BUY
        assert agg[0].metadata["orchestrator_sources"] == ["b1"]

    async def test_per_symbol_independent_net(self):
        orch = self._orch(
            [
                _AsyncStrategy(
                    "a", [_sig("AAPL", Side.BUY, "a"), _sig("MSFT", Side.SELL, "a")]
                ),
                _AsyncStrategy(
                    "b", [_sig("AAPL", Side.BUY, "b"), _sig("MSFT", Side.SELL, "b")]
                ),
            ]
        )
        await orch.run_all({})
        by = {s.symbol: s.side for s in orch.aggregate_signals()}
        assert by == {"AAPL": Side.BUY, "MSFT": Side.SELL}

    async def test_aggregate_signals_accepts_explicit_input(self):
        # aggregate_signals works standalone on a provided iterable and
        # does not require a prior run_all. Two DISTINCT strategies voting
        # opposing sides with equal weight net to zero → HOLD.
        orch = self._orch([])
        out = orch.aggregate_signals(
            [_sig("AAPL", Side.BUY, "a"), _sig("AAPL", Side.SELL, "b")]
        )
        assert out[0].side == Side.HOLD  # 1 - 1 == 0 → HOLD


# --------------------------------------------------------------------- #
# Failure & timeout isolation (the robustness fixes)
# --------------------------------------------------------------------- #


class TestDeduplicationAndErrors:
    async def test_raising_async_strategy_isolated(self):
        with capture_logs() as logs:
            orch = StrategyOrchestrator(
                [
                    _AsyncStrategy("good", [_sig("AAPL", Side.BUY, "good")]),
                    _RaisingAsyncStrategy("bad", RuntimeError("boom")),
                ],
                _FakeCostModel(),
            )
            raw = await orch.run_all({})
        # The good strategy contributed; the bad one was skipped.
        assert [s.strategy_id for s in raw] == ["good"]
        assert any(e["event"] == "orchestrator.strategy_failed" for e in logs)
        # Aggregation still produces a decision from the survivors.
        assert orch.aggregate_signals()[0].side == Side.BUY

    async def test_raising_sync_strategy_isolated(self):
        orch = StrategyOrchestrator(
            [
                _SyncStrategy("good", [_sig("AAPL", Side.BUY, "good")]),
                _RaisingSyncStrategy("bad", ValueError("sync crash")),
            ],
            _FakeCostModel(),
        )
        raw = await orch.run_all({})
        assert [s.strategy_id for s in raw] == ["good"]
        assert orch.aggregate_signals()[0].side == Side.BUY

    async def test_multiple_failures_all_isolated(self):
        orch = StrategyOrchestrator(
            [
                _RaisingAsyncStrategy("bad1", RuntimeError("a")),
                _RaisingAsyncStrategy("bad2", ValueError("b")),
                _AsyncStrategy("good", [_sig("AAPL", Side.BUY, "good")]),
            ],
            _FakeCostModel(),
        )
        raw = await orch.run_all({})
        assert [s.strategy_id for s in raw] == ["good"]

    async def test_registry_mutation_during_run_does_not_crash(self):
        # A strategy that unregisters a sibling mid-cycle must not raise
        # "dictionary changed size during iteration" — run_all snapshots
        # the registry via ``list(...)``.
        orch = StrategyOrchestrator(
            [_AsyncStrategy("victim", [_sig("AAPL", Side.SELL, "victim")])],
            _FakeCostModel(),
        )

        class _Unregisterer:
            id = "keeper"

            async def evaluate(self, md, cm):
                orch.unregister("victim")
                return [_sig("AAPL", Side.BUY, "keeper")]

        orch.register(_Unregisterer())

        raw = await orch.run_all({})

        # Both ran (snapshot captured both before the mutation).
        assert {s.strategy_id for s in raw} == {"keeper", "victim"}
        assert "victim" not in orch  # removal took effect afterwards


class TestStrategyTimeout:
    """The narrow ``asyncio.TimeoutError`` handling in ``run_all``."""

    async def test_slow_async_strategy_times_out_and_is_skipped(self):
        # A strategy whose async result exceeds the cap is cancelled,
        # logged as ``orchestrator.strategy_timeout`` and skipped; the
        # remaining strategies still contribute.
        class _Slow:
            id = "slow"

            async def evaluate(self, md, cm):
                await asyncio.sleep(5.0)  # far beyond the cap
                return [_sig("AAPL", Side.BUY, "slow")]  # never reached

        with capture_logs() as logs:
            orch = StrategyOrchestrator(
                [
                    _Slow(),
                    _AsyncStrategy("good", [_sig("AAPL", Side.BUY, "good")]),
                ],
                _FakeCostModel(),
            )
            raw = await orch.run_all({}, timeout_seconds=0.05)

        # The timeout was surfaced distinctly.
        assert any(e["event"] == "orchestrator.strategy_timeout" for e in logs)
        assert not any(e["event"] == "orchestrator.strategy_failed" for e in logs)
        # The slow strategy contributed nothing; the good one survived.
        assert [s.strategy_id for s in raw] == ["good"]
        assert orch.aggregate_signals()[0].side == Side.BUY

    async def test_no_timeout_allows_slow_strategy_to_complete(self):
        class _Slowish:
            id = "slowish"

            async def evaluate(self, md, cm):
                await asyncio.sleep(0.01)
                return [_sig("AAPL", Side.BUY, "slowish")]

        orch = StrategyOrchestrator([_Slowish()], _FakeCostModel())
        # No cap → the awaitable runs to completion.
        raw = await orch.run_all({})
        assert [s.strategy_id for s in raw] == ["slowish"]

    async def test_async_strategy_raising_builtin_timeout_without_cap_is_failure(self):
        # An async strategy that raises the builtin TimeoutError itself,
        # run WITHOUT a wait_for cap, propagates through the generic
        # ``await raw`` path and is reported as strategy_failed. This is
        # the clean distinction: with no cap the narrow wait_for guard is
        # never entered, so the builtin TimeoutError cannot be mistaken
        # for a timeout.
        class _RaisesBuiltinTimeout:
            id = "raises"

            async def evaluate(self, md, cm):
                raise TimeoutError("inner socket timeout")

        with capture_logs() as logs:
            orch = StrategyOrchestrator(
                [
                    _RaisesBuiltinTimeout(),
                    _AsyncStrategy("good", [_sig("AAPL", Side.BUY, "good")]),
                ],
                _FakeCostModel(),
            )
            raw = await orch.run_all({})  # no timeout_seconds cap

        # Reported as a FAILURE, never as a timeout.
        assert any(e["event"] == "orchestrator.strategy_failed" for e in logs)
        assert not any(e["event"] == "orchestrator.strategy_timeout" for e in logs)
        assert [s.strategy_id for s in raw] == ["good"]

    async def test_sync_strategy_raising_builtin_timeout_is_failure_not_timeout(self):
        # THE required regression: a *sync* evaluate that raises the
        # builtin TimeoutError (from the synchronous call frame, which is
        # outside the narrow wait_for guard) must be reported as
        # strategy_failed, never masquerade as a timeout.
        with capture_logs() as logs:
            orch = StrategyOrchestrator(
                [
                    _RaisingSyncStrategy("boom", TimeoutError("socket timed out")),
                    _SyncStrategy("good", [_sig("AAPL", Side.BUY, "good")]),
                ],
                _FakeCostModel(),
            )
            raw = await orch.run_all({}, timeout_seconds=1.0)

        assert any(
            e["event"] == "orchestrator.strategy_failed" and e["strategy_id"] == "boom"
            for e in logs
        )
        assert not any(e["event"] == "orchestrator.strategy_timeout" for e in logs)
        assert [s.strategy_id for s in raw] == ["good"]


# --------------------------------------------------------------------- #
# Robustness fix: unknown Side raises the correct exception type
# --------------------------------------------------------------------- #


class TestUnknownSide:
    def test_unknown_side_in_net_position_raises_orchestrator_error(self):
        # A Signal carrying a side outside {BUY, SELL, HOLD} (only
        # reachable by bypassing the StrEnum, e.g. via model_copy) must
        # raise StrategyOrchestratorError — the *correct*, specific type
        # — not a KeyError or silent acceptance.
        unknown = _sig("AAPL", Side.BUY, "x").model_copy(update={"side": "moo"})
        orch = StrategyOrchestrator(
            [],
            _FakeCostModel(),
            conflict_resolution=ConflictResolution.NET_POSITION,
        )
        with pytest.raises(StrategyOrchestratorError) as exc_info:
            orch.aggregate_signals([unknown])
        assert "unsupported side" in str(exc_info.value)
        assert repr("moo") in str(exc_info.value)

    async def test_unknown_side_propagates_from_run_all_pipeline(self):
        # End-to-end: a strategy emitting an unknown-side signal flows
        # through run_all and surfaces the error at aggregation time.
        class _BadSide:
            id = "bad"

            def evaluate(self, md, cm):
                return [_sig("AAPL", Side.BUY, "bad").model_copy(update={"side": "wat"})]

        orch = StrategyOrchestrator(
            [_BadSide()],
            _FakeCostModel(),
            conflict_resolution=ConflictResolution.NET_POSITION,
        )
        await orch.run_all({})
        with pytest.raises(StrategyOrchestratorError):
            orch.aggregate_signals()

    def test_known_sides_do_not_raise(self):
        # Sanity: all three valid sides aggregate without error.
        orch = StrategyOrchestrator(
            [],
            _FakeCostModel(),
            conflict_resolution=ConflictResolution.NET_POSITION,
        )
        out = orch.aggregate_signals(
            [
                _sig("AAPL", Side.BUY, "a"),
                _sig("AAPL", Side.SELL, "b"),
                _sig("MSFT", Side.HOLD, "c"),
            ]
        )
        assert {(s.symbol, s.side) for s in out} == {("AAPL", Side.HOLD), ("MSFT", Side.HOLD)}


# --------------------------------------------------------------------- #
# Robustness fix: None metadata resolves to an empty dict
# --------------------------------------------------------------------- #


class TestNoneMetadataResolution:
    def test_none_metadata_priority_resolves_without_error(self):
        # A template signal whose ``metadata`` is None (only reachable via
        # the validation-bypassing model_copy) must NOT crash with
        # ``TypeError: 'NoneType' object does not support item assignment``
        # during aggregation.
        template = _sig("AAPL", Side.BUY, "x").model_copy(update={"metadata": None})
        assert template.metadata is None  # precondition

        orch = StrategyOrchestrator([_AsyncStrategy("x")], _FakeCostModel())
        out = orch.aggregate_signals([template])

        assert len(out) == 1
        resolved = out[0]
        # metadata was rebuilt as a fresh dict (not None) and stamped.
        assert isinstance(resolved.metadata, dict)
        assert resolved.metadata["orchestrator_sources"] == ["x"]

    def test_none_metadata_net_position_resolves_without_error(self):
        template = _sig("AAPL", Side.BUY, "x").model_copy(update={"metadata": None})
        orch = StrategyOrchestrator(
            [],
            _FakeCostModel(),
            conflict_resolution=ConflictResolution.NET_POSITION,
        )
        out = orch.aggregate_signals([template])
        assert out[0].side == Side.BUY
        assert isinstance(out[0].metadata, dict)
        assert out[0].metadata["orchestrator_sources"] == ["x"]

    def test_none_metadata_all_hold_resolves_without_error(self):
        # Even a HOLD outcome (no active voters) rebuilds metadata safely.
        template = _sig("AAPL", Side.HOLD, "x").model_copy(update={"metadata": None})
        orch = StrategyOrchestrator([_AsyncStrategy("x")], _FakeCostModel())
        out = orch.aggregate_signals([template])
        assert out[0].side == Side.HOLD
        assert isinstance(out[0].metadata, dict)
        assert out[0].metadata["orchestrator_sources"] == []


class TestMetadataDeepCopyIsolation:
    async def test_resolved_metadata_is_deep_copied(self):
        # Mutating the aggregated signal's metadata must not leak back into
        # the source strategy's signal (nested dict included).
        nested = {"conf": {"score": 0.9}}
        src = _sig("AAPL", Side.BUY, "x").model_copy(update={"metadata": dict(nested)})
        orch = StrategyOrchestrator([_AsyncStrategy("x")], _FakeCostModel())
        out = orch.aggregate_signals([src])
        resolved = out[0]

        # Mutate the resolved metadata deeply.
        resolved.metadata["orchestrator_sources"].append("ZZZ")
        resolved.metadata["conf"]["score"] = -1.0

        # Source is untouched.
        assert src.metadata == {"conf": {"score": 0.9}}
        assert src.metadata["conf"] is not resolved.metadata["conf"]

    async def test_resolved_signal_strategy_id_is_aggregated_marker(self):
        orch = StrategyOrchestrator([_AsyncStrategy("x", [_sig("AAPL", Side.BUY, "x")])], _FakeCostModel())
        await orch.run_all({})
        resolved = orch.aggregate_signals()[0]
        assert resolved.strategy_id == "orchestrator"
        assert resolved.metadata["orchestrator_sources"] == ["x"]


# --------------------------------------------------------------------- #
# Robustness fix: float-dust in priority comparisons (math.isclose)
# --------------------------------------------------------------------- #


class TestPriorityFloatComparison:
    """``_priority`` must treat priorities that are equal in intent but
    differ by float dust (e.g. ``0.1 + 0.2`` vs ``0.3``) as tied."""

    def test_near_equal_priorities_tie_to_hold(self):
        # ``0.1 + 0.2`` evaluates to ``0.30000000000000004``, which compares
        # *unequal* to ``0.3`` under ``==``. The two strategies should still
        # be treated as tied at the top priority on opposing sides, so the
        # resolution is the HOLD stalemate rather than one side winning.
        assert (0.1 + 0.2) != 0.3  # precondition: this is the float hazard
        orch = StrategyOrchestrator(
            [
                _AsyncStrategy("a", [_sig("AAPL", Side.BUY, "a")]),
                _AsyncStrategy("b", [_sig("AAPL", Side.SELL, "b")]),
            ],
            _FakeCostModel(),
            priorities={"a": 0.3, "b": 0.1 + 0.2},
        )
        out = orch.aggregate_signals(
            [_sig("AAPL", Side.BUY, "a"), _sig("AAPL", Side.SELL, "b")]
        )
        assert out[0].side == Side.HOLD

    def test_clearly_distinct_priorities_still_pick_winner(self):
        # Regression guard: priorities that are genuinely far apart must
        # NOT be collapsed into a tie by the isclose tolerance.
        orch = StrategyOrchestrator(
            [
                _AsyncStrategy("low", [_sig("AAPL", Side.BUY, "low")]),
                _AsyncStrategy("high", [_sig("AAPL", Side.SELL, "high")]),
            ],
            _FakeCostModel(),
            priorities={"low": 1.0, "high": 9.0},
        )
        out = orch.aggregate_signals(
            [_sig("AAPL", Side.BUY, "low"), _sig("AAPL", Side.SELL, "high")]
        )
        assert out[0].side == Side.SELL


# --------------------------------------------------------------------- #
# Robustness fix: deduplicate signals per (strategy_id, symbol)
# --------------------------------------------------------------------- #


class TestDeduplicationPerStrategySymbol:
    """``aggregate_signals`` keeps at most one signal per
    (strategy_id, symbol) before voting."""

    def test_net_position_does_not_double_count_same_strategy(self):
        # Strategy "a" emits two BUY votes on AAPL. Without dedup the net
        # would be ``0.6 + 0.6 - 1.0 == 0.2`` → BUY ("a" double-counted).
        # After dedup only the first "a" vote counts, so the net is
        # ``0.6 - 1.0 == -0.4`` → SELL.
        orch = StrategyOrchestrator(
            [],
            _FakeCostModel(),
            conflict_resolution=ConflictResolution.NET_POSITION,
        )
        out = orch.aggregate_signals(
            [
                _sig("AAPL", Side.BUY, "a", weight=0.6),
                _sig("AAPL", Side.BUY, "a", weight=0.6),
                _sig("AAPL", Side.SELL, "b", weight=1.0),
            ]
        )
        assert out[0].side == Side.SELL
        assert out[0].weight == pytest.approx(0.4)
        # Only one "a" vote survives into the audit trail.
        assert out[0].metadata["orchestrator_sources"] == ["a", "b"]

    def test_priority_self_conflict_resolves_to_first_signal(self):
        # A single strategy emitting both BUY and SELL on the same symbol
        # used to fill the entire winner set and deadlock itself to HOLD.
        # After dedup only its first signal survives, resolving to BUY.
        orch = StrategyOrchestrator(
            [_AsyncStrategy("a")],
            _FakeCostModel(),
            priorities={"a": 5.0},
        )
        out = orch.aggregate_signals(
            [_sig("AAPL", Side.BUY, "a"), _sig("AAPL", Side.SELL, "a")]
        )
        assert out[0].side == Side.BUY
        assert out[0].metadata["orchestrator_sources"] == ["a"]

    def test_distinct_strategies_on_same_symbol_unaffected(self):
        # Sanity: dedup is keyed on (strategy_id, symbol), so multiple
        # strategies legitimately voting the same symbol are all kept.
        orch = StrategyOrchestrator(
            [],
            _FakeCostModel(),
            conflict_resolution=ConflictResolution.NET_POSITION,
        )
        out = orch.aggregate_signals(
            [
                _sig("AAPL", Side.BUY, "a", weight=0.6),
                _sig("AAPL", Side.BUY, "b", weight=0.6),
            ]
        )
        assert out[0].side == Side.BUY
        assert out[0].weight == pytest.approx(1.0)  # 0.6 + 0.6 clamped

    def test_same_strategy_different_symbols_unaffected(self):
        # A strategy voting two different symbols is NOT a duplicate.
        orch = StrategyOrchestrator([_AsyncStrategy("a")], _FakeCostModel())
        out = orch.aggregate_signals(
            [_sig("AAPL", Side.BUY, "a"), _sig("MSFT", Side.SELL, "a")]
        )
        assert {s.symbol: s.side for s in out} == {"AAPL": Side.BUY, "MSFT": Side.SELL}

    async def test_dedup_applies_to_run_all_pipeline(self):
        # End-to-end: a strategy emitting two signals for the same symbol
        # is deduplicated before NET_POSITION aggregation, so a single
        # strategy cannot cast two votes.
        class _Greedy:
            id = "a"

            def evaluate(self, md, cm):
                return [
                    _sig("AAPL", Side.BUY, "a", weight=0.6),
                    _sig("AAPL", Side.BUY, "a", weight=0.6),
                ]

        orch = StrategyOrchestrator(
            [_Greedy(), _AsyncStrategy("b", [_sig("AAPL", Side.SELL, "b", weight=1.0)])],
            _FakeCostModel(),
            conflict_resolution=ConflictResolution.NET_POSITION,
        )
        await orch.run_all({})
        agg = orch.aggregate_signals()
        assert agg[0].side == Side.SELL
        assert agg[0].weight == pytest.approx(0.4)
