"""Tests for engine.core.signal_aggregator — multi-strategy aggregation."""

from __future__ import annotations

import pytest

from engine.core.signal import Side, Signal, SignalBatch
from engine.core.signal_aggregator import (
    AggregationMethod,
    SignalAggregator,
    SignalAggregatorError,
)


def _batch(strategy_id: str, *signals: Signal) -> SignalBatch:
    return SignalBatch(strategy_id=strategy_id, signals=list(signals))


def _sig(symbol: str, side: Side, strategy_id: str, **kw) -> Signal:
    return Signal(symbol=symbol, side=side, strategy_id=strategy_id, **kw)


class TestUnanimous:
    def test_all_agree_emits_signal(self):
        agg = SignalAggregator(AggregationMethod.UNANIMOUS)
        out = agg.aggregate(
            [
                _batch("s1", _sig("AAPL", Side.BUY, "s1")),
                _batch("s2", _sig("AAPL", Side.BUY, "s2")),
                _batch("s3", _sig("AAPL", Side.BUY, "s3")),
            ]
        )
        assert len(out) == 1
        assert out[0].symbol == "AAPL"
        assert out[0].side == Side.BUY

    def test_one_dissents_emits_hold(self):
        agg = SignalAggregator(AggregationMethod.UNANIMOUS)
        out = agg.aggregate(
            [
                _batch("s1", _sig("AAPL", Side.BUY, "s1")),
                _batch("s2", _sig("AAPL", Side.BUY, "s2")),
                _batch("s3", _sig("AAPL", Side.SELL, "s3")),
            ]
        )
        assert len(out) == 1
        assert out[0].symbol == "AAPL"
        assert out[0].side == Side.HOLD

    def test_partial_coverage_does_not_block(self):
        # Only s1 voted on AAPL; unanimous over the strategies that did
        # express an opinion is still a clear signal.
        agg = SignalAggregator(AggregationMethod.UNANIMOUS)
        out = agg.aggregate(
            [
                _batch("s1", _sig("AAPL", Side.BUY, "s1")),
                _batch("s2"),  # no signals at all
            ]
        )
        assert len(out) == 1
        assert out[0].side == Side.BUY


class TestMajority:
    def test_majority_buys_emits_buy(self):
        agg = SignalAggregator(AggregationMethod.MAJORITY)
        out = agg.aggregate(
            [
                _batch("s1", _sig("AAPL", Side.BUY, "s1")),
                _batch("s2", _sig("AAPL", Side.BUY, "s2")),
                _batch("s3", _sig("AAPL", Side.SELL, "s3")),
            ]
        )
        assert len(out) == 1
        assert out[0].side == Side.BUY

    def test_tie_emits_hold(self):
        agg = SignalAggregator(AggregationMethod.MAJORITY)
        out = agg.aggregate(
            [
                _batch("s1", _sig("AAPL", Side.BUY, "s1")),
                _batch("s2", _sig("AAPL", Side.SELL, "s2")),
            ]
        )
        assert len(out) == 1
        assert out[0].side == Side.HOLD


class TestWeighted:
    def test_higher_weight_wins(self):
        agg = SignalAggregator(
            AggregationMethod.WEIGHTED,
            strategy_weights={"s1": 0.7, "s2": 0.2, "s3": 0.1},
        )
        out = agg.aggregate(
            [
                _batch("s1", _sig("AAPL", Side.BUY, "s1")),
                _batch("s2", _sig("AAPL", Side.SELL, "s2")),
                _batch("s3", _sig("AAPL", Side.SELL, "s3")),
            ]
        )
        # buy weight 0.7, sell weight 0.3 -> BUY wins
        assert len(out) == 1
        assert out[0].side == Side.BUY

    def test_unknown_strategy_defaults_to_unit_weight(self):
        agg = SignalAggregator(
            AggregationMethod.WEIGHTED, strategy_weights={"s1": 0.5}
        )
        out = agg.aggregate(
            [
                _batch("s1", _sig("AAPL", Side.BUY, "s1")),
                _batch("s2", _sig("AAPL", Side.SELL, "s2")),
            ]
        )
        # buy=0.5, sell=1.0 -> SELL
        assert out[0].side == Side.SELL


class TestPriority:
    def test_highest_priority_wins(self):
        agg = SignalAggregator(
            AggregationMethod.PRIORITY,
            strategy_weights={"s1": 1.0, "s2": 5.0, "s3": 3.0},
        )
        out = agg.aggregate(
            [
                _batch("s1", _sig("AAPL", Side.BUY, "s1")),
                _batch("s2", _sig("AAPL", Side.SELL, "s2")),
                _batch("s3", _sig("AAPL", Side.BUY, "s3")),
            ]
        )
        # s2 has the highest priority -> SELL wins
        assert len(out) == 1
        assert out[0].side == Side.SELL

    def test_priority_falls_back_when_top_silent(self):
        agg = SignalAggregator(
            AggregationMethod.PRIORITY,
            strategy_weights={"s1": 1.0, "s2": 5.0, "s3": 3.0},
        )
        out = agg.aggregate(
            [
                _batch("s1", _sig("AAPL", Side.BUY, "s1")),
                _batch("s2"),  # silent on AAPL
                _batch("s3", _sig("AAPL", Side.SELL, "s3")),
            ]
        )
        assert out[0].side == Side.SELL


class TestIndependent:
    def test_passthrough_concatenates(self):
        agg = SignalAggregator(AggregationMethod.INDEPENDENT)
        out = agg.aggregate(
            [
                _batch("s1", _sig("AAPL", Side.BUY, "s1")),
                _batch("s2", _sig("MSFT", Side.SELL, "s2")),
                _batch("s3", _sig("AAPL", Side.SELL, "s3")),
            ]
        )
        assert len(out) == 3
        sides = sorted(
            (s.symbol, s.side.value, s.strategy_id) for s in out
        )
        assert sides == [
            ("AAPL", "buy", "s1"),
            ("AAPL", "sell", "s3"),
            ("MSFT", "sell", "s2"),
        ]


class TestMultiSymbol:
    def test_per_symbol_aggregation_is_independent(self):
        agg = SignalAggregator(AggregationMethod.MAJORITY)
        out = agg.aggregate(
            [
                _batch(
                    "s1",
                    _sig("AAPL", Side.BUY, "s1"),
                    _sig("MSFT", Side.SELL, "s1"),
                ),
                _batch(
                    "s2",
                    _sig("AAPL", Side.BUY, "s2"),
                    _sig("MSFT", Side.SELL, "s2"),
                ),
            ]
        )
        by_symbol = {s.symbol: s.side for s in out}
        assert by_symbol == {"AAPL": Side.BUY, "MSFT": Side.SELL}


class TestHoldHandling:
    def test_holds_dont_count_against_unanimous(self):
        # HOLD = "no opinion"; unanimous among the strategies that did
        # take a position should still emit the agreed-upon side.
        agg = SignalAggregator(AggregationMethod.UNANIMOUS)
        out = agg.aggregate(
            [
                _batch("s1", _sig("AAPL", Side.BUY, "s1")),
                _batch("s2", _sig("AAPL", Side.HOLD, "s2")),
                _batch("s3", _sig("AAPL", Side.BUY, "s3")),
            ]
        )
        assert out[0].side == Side.BUY

    def test_only_holds_emits_hold(self):
        agg = SignalAggregator(AggregationMethod.MAJORITY)
        out = agg.aggregate(
            [
                _batch("s1", _sig("AAPL", Side.HOLD, "s1")),
                _batch("s2", _sig("AAPL", Side.HOLD, "s2")),
            ]
        )
        assert out[0].side == Side.HOLD


class TestEmptyInput:
    def test_no_batches_returns_empty(self):
        agg = SignalAggregator(AggregationMethod.MAJORITY)
        assert agg.aggregate([]) == []

    def test_only_empty_batches_returns_empty(self):
        agg = SignalAggregator(AggregationMethod.MAJORITY)
        assert agg.aggregate([_batch("s1"), _batch("s2")]) == []


class TestPriorityTie:
    def test_equal_priority_conflicting_sides_emit_hold(self):
        # s1 and s2 both have priority 5; they disagree -> HOLD rather
        # than dict-order-dependent winner.
        agg = SignalAggregator(
            AggregationMethod.PRIORITY,
            strategy_weights={"s1": 5.0, "s2": 5.0, "s3": 1.0},
        )
        out = agg.aggregate(
            [
                _batch("s1", _sig("AAPL", Side.BUY, "s1")),
                _batch("s2", _sig("AAPL", Side.SELL, "s2")),
                _batch("s3", _sig("AAPL", Side.BUY, "s3")),
            ]
        )
        assert out[0].side == Side.HOLD

    def test_equal_priority_agreeing_sides_emit_signal(self):
        agg = SignalAggregator(
            AggregationMethod.PRIORITY,
            strategy_weights={"s1": 5.0, "s2": 5.0, "s3": 1.0},
        )
        out = agg.aggregate(
            [
                _batch("s1", _sig("AAPL", Side.BUY, "s1")),
                _batch("s2", _sig("AAPL", Side.BUY, "s2")),
                _batch("s3", _sig("AAPL", Side.SELL, "s3")),
            ]
        )
        assert out[0].side == Side.BUY


class TestDuplicateSignalsWithinBatch:
    def test_last_signal_in_batch_overrides_earlier(self):
        # If a strategy emits two signals on the same symbol within one
        # batch, the LAST one is the one the aggregator considers.
        agg = SignalAggregator(AggregationMethod.UNANIMOUS)
        out = agg.aggregate(
            [
                _batch(
                    "s1",
                    _sig("AAPL", Side.BUY, "s1"),
                    _sig("AAPL", Side.SELL, "s1"),
                ),
            ]
        )
        assert len(out) == 1
        assert out[0].side == Side.SELL


class TestMetadataIsolation:
    def test_output_metadata_is_independent_of_source(self):
        # Mutating the aggregated signal's metadata must not leak back
        # into the source strategy's signal.
        src = _sig("AAPL", Side.BUY, "s1", metadata={"k": 1})
        agg = SignalAggregator(AggregationMethod.UNANIMOUS)
        out = agg.aggregate([_batch("s1", src)])
        out[0].metadata["k"] = 999
        assert src.metadata == {"k": 1}


class TestValidation:
    def test_unknown_method_rejected(self):
        with pytest.raises(SignalAggregatorError):
            SignalAggregator("not-a-real-method")  # type: ignore[arg-type]

    def test_negative_weight_rejected(self):
        with pytest.raises(SignalAggregatorError):
            SignalAggregator(
                AggregationMethod.WEIGHTED,
                strategy_weights={"s1": -1.0},
            )

    def test_nan_weight_rejected(self):
        with pytest.raises(SignalAggregatorError):
            SignalAggregator(
                AggregationMethod.WEIGHTED,
                strategy_weights={"s1": float("nan")},
            )

    def test_weighted_all_zero_weights_rejected(self):
        with pytest.raises(SignalAggregatorError):
            SignalAggregator(
                AggregationMethod.WEIGHTED,
                strategy_weights={"s1": 0.0, "s2": 0.0},
            )
