"""Tests for engine.portfolio.multi_strategy.

Covers construction/validation, registration, capital allocation math,
the risk-adjusted signal merge (netting, capital weighting, ties, HOLD
abstention), failure + timeout isolation, deep-copy input isolation, and
the two high-severity fixes:

* zero total capital is a valid *no-op* state that short-circuits
  :meth:`evaluate_all` before any strategy runs, and
* :attr:`PortfolioEvaluation.is_noop` honours its docstring contract —
  it is ``True`` whenever there are no signals **or** total capital is
  zero.
"""

from __future__ import annotations

import asyncio
import math

import pytest

from engine.core.signal import Side, Signal
from engine.portfolio.multi_strategy import (
    CombinedPosition,
    MultiStrategyPortfolio,
    MultiStrategyPortfolioError,
    PortfolioEvaluation,
    SignalMergeMode,
)

# --------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------- #


def _sig(symbol: str, side: Side, strategy_id: str, *, weight: float = 1.0) -> Signal:
    return Signal(symbol=symbol, side=side, strategy_id=strategy_id, weight=weight)


class _Strategy:
    """Async strategy returning a fixed signal list and recording the
    exact (market_data, cost_model) objects it received so tests can
    assert deep-copy isolation."""

    def __init__(self, sid: str, signals: list[Signal]) -> None:
        self._id = sid
        self._signals = signals
        self.received: list[tuple[object, object]] = []
        self.call_count = 0

    @property
    def id(self) -> str:
        return self._id

    async def evaluate(self, market_data, cost_model) -> list[Signal]:
        self.received.append((market_data, cost_model))
        self.call_count += 1
        return [s.model_copy() for s in self._signals]


class _SyncStrategy:
    """Sync (non-async) strategy to prove ``evaluate_all`` handles plain
    callables as well as coroutines."""

    def __init__(self, sid: str, signals: list[Signal]) -> None:
        self._id = sid
        self._signals = signals
        self.call_count = 0

    @property
    def id(self) -> str:
        return self._id

    def evaluate(self, market_data, cost_model) -> list[Signal]:
        self.call_count += 1
        return [s.model_copy() for s in self._signals]


class _RaisingStrategy:
    def __init__(self, sid: str, exc: BaseException) -> None:
        self._id = sid
        self._exc = exc
        self.call_count = 0

    @property
    def id(self) -> str:
        return self._id

    async def evaluate(self, market_data, cost_model) -> list[Signal]:
        self.call_count += 1
        raise self._exc


class _SlowStrategy:
    """Async strategy that sleeps past the configured eval_timeout."""

    def __init__(self, sid: str, delay: float) -> None:
        self._id = sid
        self._delay = delay
        self.call_count = 0

    @property
    def id(self) -> str:
        return self._id

    async def evaluate(self, market_data, cost_model) -> list[Signal]:
        self.call_count += 1
        await asyncio.sleep(self._delay)
        return [_sig("AAPL", Side.BUY, self._id)]


class _MutatingStrategy:
    """Mutates the market_data it receives, to prove every strategy (and
    the caller) gets an independent deep copy."""

    def __init__(self, sid: str) -> None:
        self._id = sid

    @property
    def id(self) -> str:
        return self._id

    async def evaluate(self, market_data, cost_model) -> list[Signal]:
        market_data["poisoned"] = True  # type: ignore[index]
        return []


class _NoIdStrategy:
    async def evaluate(self, market_data, cost_model) -> list[Signal]:
        return []


class _NoEvaluateStrategy:
    id = "has-id-but-no-evaluate"


class _CallableIdStrategy:
    """Strategy whose ``id`` is a method rather than a property."""

    def __init__(self, sid: str, signals: list[Signal]) -> None:
        self._id = sid
        self._signals = signals

    def id(self) -> str:  # type: ignore[override]
        return self._id

    async def evaluate(self, market_data, cost_model) -> list[Signal]:
        return [s.model_copy() for s in self._signals]


class _CostModel:
    """Minimal mutable cost-model stand-in (deep-copied per strategy)."""

    def __init__(self, tag: str = "default") -> None:
        self.tag = tag


_MARKET = {"symbol": "AAPL"}


# --------------------------------------------------------------------- #
# Construction & validation
# --------------------------------------------------------------------- #


class TestConstruction:
    def test_valid_construction_records_capital_and_defaults(self) -> None:
        pf = MultiStrategyPortfolio(total_capital=1000.0, cost_model=_CostModel())
        assert pf.total_capital == 1000.0
        assert len(pf) == 0
        assert pf.strategy_ids == []
        assert pf.capital_weights == {}

    @pytest.mark.parametrize("bad", [-1.0, -0.01, -1e9])
    def test_negative_capital_rejected(self, bad: float) -> None:
        with pytest.raises(MultiStrategyPortfolioError, match="total_capital must be non-negative"):
            MultiStrategyPortfolio(total_capital=bad, cost_model=_CostModel())

    def test_zero_capital_is_valid_noop_state(self) -> None:
        # Zero capital is a well-formed no-op (mirrors CapitalAllocation /
        # allocate_capital); it must NOT be rejected at construction.
        pf = MultiStrategyPortfolio(total_capital=0.0, cost_model=_CostModel())
        assert pf.total_capital == 0.0

    @pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
    def test_non_finite_capital_rejected(self, bad: float) -> None:
        with pytest.raises(MultiStrategyPortfolioError, match="total_capital must be finite"):
            MultiStrategyPortfolio(total_capital=bad, cost_model=_CostModel())

    @pytest.mark.parametrize("bad", ["1000", None])
    def test_non_numeric_capital_rejected(self, bad: object) -> None:
        with pytest.raises(MultiStrategyPortfolioError, match="total_capital must be a number"):
            MultiStrategyPortfolio(total_capital=bad, cost_model=_CostModel())  # type: ignore[arg-type]

    @pytest.mark.parametrize("bad", [0, -0.5, -1])
    def test_eval_timeout_must_be_positive(self, bad: float) -> None:
        with pytest.raises(MultiStrategyPortfolioError, match="eval_timeout"):
            MultiStrategyPortfolio(total_capital=100.0, cost_model=_CostModel(), eval_timeout=bad)

    def test_non_finite_eval_timeout_rejected(self) -> None:
        with pytest.raises(MultiStrategyPortfolioError, match="eval_timeout must be finite"):
            MultiStrategyPortfolio(total_capital=100.0, cost_model=_CostModel(), eval_timeout=math.inf)

    def test_max_strategies_must_be_at_least_one(self) -> None:
        with pytest.raises(MultiStrategyPortfolioError, match="max_strategies must be >= 1"):
            MultiStrategyPortfolio(total_capital=100.0, cost_model=_CostModel(), max_strategies=0)


# --------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------- #


class TestRegistration:
    def test_register_default_and_custom_weights(self) -> None:
        pf = MultiStrategyPortfolio(total_capital=1000.0, cost_model=_CostModel())
        pf.register(_Strategy("a", []))
        pf.register(_Strategy("b", []), capital_weight=2.0)
        assert pf.strategy_ids == ["a", "b"]
        assert pf.capital_weights == {"a": 1.0, "b": 2.0}
        assert pf.get_capital_weight("a") == 1.0
        assert pf.get_capital_weight("missing") is None
        assert "a" in pf
        assert "z" not in pf

    def test_register_callable_id(self) -> None:
        pf = MultiStrategyPortfolio(total_capital=1000.0, cost_model=_CostModel())
        pf.register(_CallableIdStrategy("c", []))
        assert pf.strategy_ids == ["c"]

    def test_register_rejects_strategy_without_id(self) -> None:
        pf = MultiStrategyPortfolio(total_capital=1000.0, cost_model=_CostModel())
        with pytest.raises(MultiStrategyPortfolioError, match="non-empty string `id`"):
            pf.register(_NoIdStrategy())

    def test_register_rejects_strategy_without_evaluate(self) -> None:
        pf = MultiStrategyPortfolio(total_capital=1000.0, cost_model=_CostModel())
        with pytest.raises(MultiStrategyPortfolioError, match="callable `evaluate`"):
            pf.register(_NoEvaluateStrategy())

    def test_register_rejects_duplicate_id(self) -> None:
        pf = MultiStrategyPortfolio(total_capital=1000.0, cost_model=_CostModel())
        pf.register(_Strategy("a", []))
        with pytest.raises(MultiStrategyPortfolioError, match="already registered"):
            pf.register(_Strategy("a", []))

    def test_register_rejects_negative_weight(self) -> None:
        pf = MultiStrategyPortfolio(total_capital=1000.0, cost_model=_CostModel())
        with pytest.raises(MultiStrategyPortfolioError, match="must be non-negative"):
            pf.register(_Strategy("a", []), capital_weight=-0.1)

    def test_register_rejects_non_finite_weight(self) -> None:
        pf = MultiStrategyPortfolio(total_capital=1000.0, cost_model=_CostModel())
        with pytest.raises(MultiStrategyPortfolioError, match="must be finite"):
            pf.register(_Strategy("a", []), capital_weight=math.nan)

    def test_register_enforces_max_strategies(self) -> None:
        pf = MultiStrategyPortfolio(total_capital=1000.0, cost_model=_CostModel(), max_strategies=2)
        pf.register(_Strategy("a", []))
        pf.register(_Strategy("b", []))
        with pytest.raises(MultiStrategyPortfolioError, match="max_strategies"):
            pf.register(_Strategy("c", []))

    def test_unregister_returns_presence_and_drops_weight(self) -> None:
        pf = MultiStrategyPortfolio(total_capital=1000.0, cost_model=_CostModel())
        pf.register(_Strategy("a", []), capital_weight=3.0)
        assert pf.unregister("a") is True
        assert pf.unregister("a") is False
        assert pf.strategy_ids == []
        assert pf.capital_weights == {}

    def test_set_capital_weight_updates_known_strategy(self) -> None:
        pf = MultiStrategyPortfolio(total_capital=1000.0, cost_model=_CostModel())
        pf.register(_Strategy("a", []), capital_weight=1.0)
        pf.set_capital_weight("a", 5.0)
        assert pf.get_capital_weight("a") == 5.0

    def test_set_capital_weight_rejects_unknown(self) -> None:
        pf = MultiStrategyPortfolio(total_capital=1000.0, cost_model=_CostModel())
        with pytest.raises(MultiStrategyPortfolioError, match="unknown strategy"):
            pf.set_capital_weight("ghost", 1.0)

    def test_set_capital_weight_rejects_negative(self) -> None:
        pf = MultiStrategyPortfolio(total_capital=1000.0, cost_model=_CostModel())
        pf.register(_Strategy("a", []))
        with pytest.raises(MultiStrategyPortfolioError, match="must be non-negative"):
            pf.set_capital_weight("a", -1.0)


# --------------------------------------------------------------------- #
# Capital allocation math
# --------------------------------------------------------------------- #


class TestAllocation:
    def test_normalized_weights_and_allocations(self) -> None:
        pf = MultiStrategyPortfolio(total_capital=1000.0, cost_model=_CostModel())
        pf.register(_Strategy("a", []), capital_weight=2.0)
        pf.register(_Strategy("b", []), capital_weight=1.0)
        # Relative weights need not sum to 1: {a:2, b:1} -> a=2/3, b=1/3.
        assert pf.capital_weight_normalized("a") == pytest.approx(2 / 3)
        assert pf.capital_weight_normalized("b") == pytest.approx(1 / 3)
        assert pf.allocation("a") == pytest.approx(1000.0 * 2 / 3)
        assert pf.allocation("b") == pytest.approx(1000.0 / 3)
        allocs = pf.allocations()
        assert allocs["a"] == pytest.approx(1000.0 * 2 / 3)
        assert allocs["b"] == pytest.approx(1000.0 / 3)

    def test_unknown_strategy_resolves_to_zero(self) -> None:
        pf = MultiStrategyPortfolio(total_capital=1000.0, cost_model=_CostModel())
        pf.register(_Strategy("a", []))
        assert pf.capital_weight_normalized("ghost") == 0.0
        assert pf.allocation("ghost") == 0.0

    def test_zero_weight_strategy_gets_nothing(self) -> None:
        pf = MultiStrategyPortfolio(total_capital=1000.0, cost_model=_CostModel())
        pf.register(_Strategy("a", []), capital_weight=1.0)
        pf.register(_Strategy("b", []), capital_weight=0.0)
        assert pf.allocation("b") == 0.0
        assert pf.allocation("a") == pytest.approx(1000.0)

    def test_all_zero_weights_yields_zero_allocations(self) -> None:
        pf = MultiStrategyPortfolio(total_capital=1000.0, cost_model=_CostModel())
        pf.register(_Strategy("a", []), capital_weight=0.0)
        pf.register(_Strategy("b", []), capital_weight=0.0)
        assert pf.capital_weight_normalized("a") == 0.0
        assert pf.allocations() == {"a": 0.0, "b": 0.0}

    def test_zero_capital_yields_zero_allocations(self) -> None:
        pf = MultiStrategyPortfolio(total_capital=0.0, cost_model=_CostModel())
        pf.register(_Strategy("a", []), capital_weight=1.0)
        assert pf.allocation("a") == 0.0
        assert pf.allocations() == {"a": 0.0}


# --------------------------------------------------------------------- #
# evaluate_all — core behaviour
# --------------------------------------------------------------------- #


class TestEvaluateAll:
    async def test_empty_registry_is_noop(self) -> None:
        pf = MultiStrategyPortfolio(total_capital=1000.0, cost_model=_CostModel())
        result = await pf.evaluate_all(_MARKET)
        assert result.is_noop
        assert result.signals == []
        assert result.positions == {}
        assert result.merge_mode == SignalMergeMode.RISK_ADJUSTED.value
        assert result.total_capital == 1000.0
        assert result.capital_utilization == 0.0

    async def test_single_strategy_passes_through(self) -> None:
        pf = MultiStrategyPortfolio(total_capital=1000.0, cost_model=_CostModel())
        pf.register(_Strategy("a", [_sig("AAPL", Side.BUY, "a")]))
        result = await pf.evaluate_all(_MARKET)
        assert not result.is_noop
        assert len(result.signals) == 1
        out = result.signals[0]
        assert out.symbol == "AAPL"
        assert out.side == Side.BUY
        # Whole-book weight = 1000/1000 deployed = 1.0.
        assert out.weight == pytest.approx(1.0)
        assert out.strategy_id == "portfolio"
        assert result.per_strategy_signals["a"]
        assert result.capital_deployed == pytest.approx(1000.0)
        assert result.capital_utilization == pytest.approx(1.0)

    async def test_sync_and_async_strategies_both_supported(self) -> None:
        pf = MultiStrategyPortfolio(total_capital=1000.0, cost_model=_CostModel())
        pf.register(_SyncStrategy("s", [_sig("MSFT", Side.BUY, "s")]))
        result = await pf.evaluate_all(_MARKET)
        assert len(result.signals) == 1
        assert result.signals[0].symbol == "MSFT"

    async def test_opposing_equal_signals_net_to_hold(self) -> None:
        # Two equal-capital strategies opposing on the same symbol cancel.
        pf = MultiStrategyPortfolio(total_capital=1000.0, cost_model=_CostModel())
        pf.register(_Strategy("a", [_sig("AAPL", Side.BUY, "a")]))
        pf.register(_Strategy("b", [_sig("AAPL", Side.SELL, "b")]))
        result = await pf.evaluate_all(_MARKET)
        # Net exposure 0 -> HOLD, no tradeable signal emitted...
        assert result.signals == []
        # ...but the symbol still appears in positions (it was considered).
        assert "AAPL" in result.positions
        assert result.positions["AAPL"].side == Side.HOLD
        assert result.positions["AAPL"].net_weight == 0.0
        assert set(result.positions["AAPL"].contributors) == {"a", "b"}

    async def test_capital_weight_drives_merge_decision(self) -> None:
        # a has 2x the capital of b, so on a conflict a's dollar exposure wins.
        pf = MultiStrategyPortfolio(total_capital=1000.0, cost_model=_CostModel())
        pf.register(_Strategy("a", [_sig("AAPL", Side.BUY, "a")]), capital_weight=2.0)
        pf.register(_Strategy("b", [_sig("AAPL", Side.SELL, "b")]), capital_weight=1.0)
        result = await pf.evaluate_all(_MARKET)
        # net = +666.67 - 333.33 = +333.33 -> BUY, weight ~0.3333.
        assert len(result.signals) == 1
        out = result.signals[0]
        assert out.side == Side.BUY
        assert out.weight == pytest.approx(1 / 3, abs=1e-3)
        assert result.net_exposure == pytest.approx(1000.0 / 3, abs=1e-2)

    async def test_signals_grouped_per_symbol_independently(self) -> None:
        pf = MultiStrategyPortfolio(total_capital=1000.0, cost_model=_CostModel())
        pf.register(
            _Strategy(
                "a",
                [_sig("AAPL", Side.BUY, "a"), _sig("MSFT", Side.SELL, "a")],
            )
        )
        result = await pf.evaluate_all(_MARKET)
        symbols = {s.symbol for s in result.signals}
        assert symbols == {"AAPL", "MSFT"}
        assert {p: pos.side for p, pos in result.positions.items()} == {
            "AAPL": Side.BUY,
            "MSFT": Side.SELL,
        }

    async def test_hold_abstains_and_contributes_nothing(self) -> None:
        pf = MultiStrategyPortfolio(total_capital=1000.0, cost_model=_CostModel())
        pf.register(
            _Strategy(
                "a",
                [_sig("AAPL", Side.HOLD, "a"), _sig("AAPL", Side.BUY, "a")],
            )
        )
        result = await pf.evaluate_all(_MARKET)
        # HOLD abstains; the BUY still deploys full capital.
        assert len(result.signals) == 1
        assert result.signals[0].side == Side.BUY
        assert result.signals[0].weight == pytest.approx(1.0)

    async def test_signal_weight_scales_exposure(self) -> None:
        # A 0.25-weight BUY deploys only a quarter of the strategy's capital.
        pf = MultiStrategyPortfolio(total_capital=1000.0, cost_model=_CostModel())
        pf.register(_Strategy("a", [_sig("AAPL", Side.BUY, "a", weight=0.25)]))
        result = await pf.evaluate_all(_MARKET)
        assert result.signals[0].weight == pytest.approx(0.25)
        assert result.capital_deployed == pytest.approx(250.0)

    async def test_merged_signal_carries_portfolio_provenance(self) -> None:
        pf = MultiStrategyPortfolio(total_capital=1000.0, cost_model=_CostModel())
        pf.register(_Strategy("a", [_sig("AAPL", Side.BUY, "a", weight=0.4)]))
        result = await pf.evaluate_all(_MARKET)
        out = result.signals[0]
        assert out.strategy_id == "portfolio"
        assert out.metadata["portfolio_contributors"] == ["a"]

    async def test_merged_metadata_is_deep_copied(self) -> None:
        pf = MultiStrategyPortfolio(total_capital=1000.0, cost_model=_CostModel())
        src = _sig("AAPL", Side.BUY, "a")
        src.metadata["nested"] = {"k": [1, 2]}
        pf.register(_Strategy("a", [src]))
        result = await pf.evaluate_all(_MARKET)
        out = result.signals[0]
        out.metadata["nested"]["k"].append(3)
        # Mutating the merged metadata must not touch the source signal.
        assert src.metadata["nested"]["k"] == [1, 2]

    async def test_non_finite_signal_weight_abstains(self) -> None:
        # ``Signal`` validates ``weight ∈ [0,1]`` at construction, so a
        # non-finite weight is injected via ``model_copy`` — the same
        # pattern used by ``test_orchestration.py``. The portfolio must
        # treat a non-finite weight as an abstention (it cannot scale an
        # exposure) and resolve the symbol to HOLD with no emitted signal.
        bad = _sig("AAPL", Side.BUY, "a", weight=1.0).model_copy(
            update={"weight": math.nan}
        )
        pf = MultiStrategyPortfolio(total_capital=1000.0, cost_model=_CostModel())
        pf.register(_Strategy("a", [bad]))
        result = await pf.evaluate_all(_MARKET)
        # NaN weight cannot scale an exposure -> abstention -> HOLD, no signal.
        assert result.signals == []
        assert result.positions["AAPL"].side == Side.HOLD

    async def test_unknown_merge_mode_raises(self) -> None:
        pf = MultiStrategyPortfolio(total_capital=1000.0, cost_model=_CostModel())
        pf.register(_Strategy("a", []))
        with pytest.raises(MultiStrategyPortfolioError, match="unknown merge mode"):
            await pf.evaluate_all(_MARKET, merge_mode="bogus")

    async def test_merge_mode_accepts_enum(self) -> None:
        pf = MultiStrategyPortfolio(total_capital=1000.0, cost_model=_CostModel())
        pf.register(_Strategy("a", [_sig("AAPL", Side.BUY, "a")]))
        result = await pf.evaluate_all(_MARKET, merge_mode=SignalMergeMode.RISK_ADJUSTED)
        assert result.merge_mode == SignalMergeMode.RISK_ADJUSTED.value
        assert len(result.signals) == 1

    async def test_strategy_returning_none_contributes_nothing(self) -> None:
        class _NoneStrategy:
            id = "none"

            async def evaluate(self, md, cm) -> None:
                return None

        pf = MultiStrategyPortfolio(total_capital=1000.0, cost_model=_CostModel())
        pf.register(_NoneStrategy())
        result = await pf.evaluate_all(_MARKET)
        assert result.signals == []
        assert result.per_strategy_signals == {"none": []}


# --------------------------------------------------------------------- #
# evaluate_all — isolation of failures & timeouts
# --------------------------------------------------------------------- #


class TestFailureAndTimeoutIsolation:
    async def test_failing_strategy_recorded_others_continue(self) -> None:
        pf = MultiStrategyPortfolio(total_capital=1000.0, cost_model=_CostModel())
        pf.register(_RaisingStrategy("bad", RuntimeError("boom")))
        pf.register(_Strategy("good", [_sig("AAPL", Side.BUY, "good")]))
        result = await pf.evaluate_all(_MARKET)
        assert "bad" in result.errors
        assert "RuntimeError" in result.errors["bad"]
        assert "good" not in result.errors
        assert len(result.signals) == 1
        assert result.signals[0].side == Side.BUY

    async def test_timeout_recorded_as_timeouterror(self) -> None:
        pf = MultiStrategyPortfolio(
            total_capital=1000.0, cost_model=_CostModel(), eval_timeout=0.02
        )
        pf.register(_SlowStrategy("slow", delay=1.0))
        result = await pf.evaluate_all(_MARKET)
        assert "slow" in result.errors
        assert result.errors["slow"].startswith("TimeoutError")
        assert result.is_noop

    async def test_sync_builtin_timeouterror_treated_as_failure(self) -> None:
        # A sync strategy raising the builtin TimeoutError must be reported
        # as a strategy failure, NOT misclassified as a deadline expiry.
        class _RaisesTimeout:
            id = "rt"

            def evaluate(self, md, cm):
                raise TimeoutError("sync boom")

        pf = MultiStrategyPortfolio(total_capital=1000.0, cost_model=_CostModel())
        pf.register(_RaisesTimeout())
        result = await pf.evaluate_all(_MARKET)
        assert "rt" in result.errors
        # Sync path reports "<ExcType>: <msg>" (no "evaluate exceeded" phrasing).
        assert "evaluate exceeded" not in result.errors["rt"]


class TestDeepCopyIsolation:
    async def test_each_strategy_gets_independent_market_copy(self) -> None:
        pf = MultiStrategyPortfolio(total_capital=1000.0, cost_model=_CostModel())
        mutator = _MutatingStrategy("mut")
        observer = _Strategy("obs", [])
        pf.register(mutator)
        pf.register(observer)
        market = {"symbol": "AAPL"}
        await pf.evaluate_all(market)
        # The observer must not see the mutator's change, and the caller's
        # original must be untouched as well.
        assert "poisoned" not in market
        assert all("poisoned" not in md for md, _ in observer.received)

    async def test_each_strategy_gets_independent_cost_model_copy(self) -> None:
        pf = MultiStrategyPortfolio(total_capital=1000.0, cost_model=_CostModel("orig"))
        a = _Strategy("a", [])
        pf.register(a)
        await pf.evaluate_all(_MARKET)
        received_cm = a.received[0][1]
        assert received_cm.tag == "orig"


# --------------------------------------------------------------------- #
# The two high-severity fixes
# --------------------------------------------------------------------- #


class TestZeroCapitalNoopFix:
    """Fix 1: zero capital short-circuits evaluate_all (strategies are
    never invoked) and yields a clean no-op result."""

    def test_zero_capital_is_accepted_at_construction(self) -> None:
        pf = MultiStrategyPortfolio(total_capital=0.0, cost_model=_CostModel())
        assert pf.total_capital == 0.0

    async def test_zero_capital_short_circuits_before_strategy_eval(self) -> None:
        spy = _Strategy("a", [_sig("AAPL", Side.BUY, "a")])
        pf = MultiStrategyPortfolio(total_capital=0.0, cost_model=_CostModel())
        pf.register(spy)
        result = await pf.evaluate_all(_MARKET)
        # The strategy must NOT have been evaluated.
        assert spy.call_count == 0
        assert spy.received == []
        # Result is a clean no-op with safe bookkeeping.
        assert result.signals == []
        assert result.positions == {}
        assert result.capital_deployed == 0.0
        assert result.net_exposure == 0.0
        assert result.capital_utilization == 0.0
        assert result.total_capital == 0.0
        assert result.is_noop

    async def test_zero_capital_with_valid_mode_short_circuits(self) -> None:
        # A valid mode with zero capital short-circuits cleanly.
        pf = MultiStrategyPortfolio(total_capital=0.0, cost_model=_CostModel())
        pf.register(_Strategy("a", []))
        result = await pf.evaluate_all(_MARKET, merge_mode="risk_adjusted")
        assert result.is_noop

    async def test_positive_capital_still_evaluates_strategies(self) -> None:
        # Regression guard: the short-circuit must not swallow real capital.
        spy = _Strategy("a", [_sig("AAPL", Side.BUY, "a")])
        pf = MultiStrategyPortfolio(total_capital=10.0, cost_model=_CostModel())
        pf.register(spy)
        result = await pf.evaluate_all(_MARKET)
        assert spy.call_count == 1
        assert not result.is_noop


class TestIsNoopContract:
    """Fix 2: is_noop is True iff there are no signals OR total capital
    is zero — matching its docstring."""

    def test_empty_signals_is_noop(self) -> None:
        assert PortfolioEvaluation(signals=[], total_capital=1000.0).is_noop is True

    def test_signals_with_positive_capital_is_not_noop(self) -> None:
        ev = PortfolioEvaluation(signals=[_sig("AAPL", Side.BUY, "a")], total_capital=1000.0)
        assert ev.is_noop is False

    def test_signals_with_zero_capital_is_noop(self) -> None:
        # The bug: previously this returned False because is_noop only
        # checked len(signals). With zero capital nothing is deployable, so
        # the docstring (and now the code) treat it as a no-op.
        ev = PortfolioEvaluation(
            signals=[_sig("AAPL", Side.BUY, "a")],
            total_capital=0.0,
        )
        assert ev.is_noop is True

    def test_trade_signals_filters_hold(self) -> None:
        ev = PortfolioEvaluation(
            signals=[
                _sig("AAPL", Side.BUY, "a"),
                _sig("MSFT", Side.HOLD, "a"),
                _sig("GOOG", Side.SELL, "a"),
            ],
            total_capital=1000.0,
        )
        assert len(ev.trade_signals) == 2
        assert {s.symbol for s in ev.trade_signals} == {"AAPL", "GOOG"}

    def test_combined_position_defaults(self) -> None:
        pos = CombinedPosition(symbol="X", side=Side.HOLD, net_weight=0.0, net_exposure=0.0)
        assert pos.contributors == []
        assert pos.signals == []

    def test_portfolio_evaluation_defaults(self) -> None:
        ev = PortfolioEvaluation()
        assert ev.signals == []
        assert ev.positions == {}
        assert ev.per_strategy_signals == {}
        assert ev.errors == {}
        assert ev.total_capital == 0.0
        assert ev.is_noop is True  # zero capital default -> noop
