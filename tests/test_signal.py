"""Tests for Signal types and convenience constructors."""

from engine.core.signal import Side, Signal, SignalBatch, SignalStrength


class TestSignalConstructors:
    def test_buy_constructor(self):
        sig = Signal.buy(symbol="AAPL", strategy_id="test")
        assert sig.side == Side.BUY
        assert sig.symbol == "AAPL"
        assert sig.strategy_id == "test"

    def test_sell_constructor(self):
        sig = Signal.sell(symbol="MSFT", strategy_id="test")
        assert sig.side == Side.SELL

    def test_hold_constructor(self):
        sig = Signal.hold(symbol="GOOGL", strategy_id="test")
        assert sig.side == Side.HOLD


class TestSignalDefaults:
    def test_default_weight(self):
        sig = Signal.buy(symbol="AAPL", strategy_id="test")
        assert sig.weight == 1.0

    def test_default_strength(self):
        sig = Signal.buy(symbol="AAPL", strategy_id="test")
        assert sig.strength == SignalStrength.MODERATE

    def test_auto_generated_id(self):
        sig1 = Signal.buy(symbol="AAPL", strategy_id="test")
        sig2 = Signal.buy(symbol="AAPL", strategy_id="test")
        assert sig1.id != sig2.id

    def test_auto_generated_timestamp(self):
        sig = Signal.buy(symbol="AAPL", strategy_id="test")
        assert sig.timestamp is not None


class TestSignalBatch:
    def test_trade_signals_excludes_holds(self):
        batch = SignalBatch(
            strategy_id="test",
            signals=[
                Signal.buy(symbol="AAPL", strategy_id="test"),
                Signal.hold(symbol="MSFT", strategy_id="test"),
                Signal.sell(symbol="GOOGL", strategy_id="test"),
            ],
        )
        trade_signals = batch.trade_signals
        assert len(trade_signals) == 2
        assert all(s.side != Side.HOLD for s in trade_signals)

    def test_empty_batch(self):
        batch = SignalBatch(strategy_id="test")
        assert batch.trade_signals == []

    def test_all_hold_signals(self):
        batch = SignalBatch(
            strategy_id="test",
            signals=[
                Signal.hold(symbol="AAPL", strategy_id="test"),
                Signal.hold(symbol="MSFT", strategy_id="test"),
            ],
        )
        assert batch.trade_signals == []


class TestSignalFields:
    def test_signal_with_quantity(self):
        sig = Signal.buy(symbol="AAPL", strategy_id="test", quantity=100)
        assert sig.quantity == 100

    def test_signal_with_weight(self):
        sig = Signal.buy(symbol="AAPL", strategy_id="test", weight=0.5)
        assert sig.weight == 0.5

    def test_signal_with_reason(self):
        sig = Signal.buy(symbol="AAPL", strategy_id="test", reason="momentum breakout")
        assert sig.reason == "momentum breakout"

    def test_signal_with_metadata(self):
        sig = Signal.buy(symbol="AAPL", strategy_id="test", metadata={"confidence": 0.95})
        assert sig.metadata["confidence"] == 0.95

    def test_signal_with_stop_loss(self):
        sig = Signal.buy(symbol="AAPL", strategy_id="test", stop_loss_pct=0.05)
        assert sig.stop_loss_pct == 0.05

    def test_signal_with_take_profit(self):
        sig = Signal.buy(symbol="AAPL", strategy_id="test", take_profit_pct=0.10)
        assert sig.take_profit_pct == 0.10

    def test_signal_with_max_cost_pct(self):
        sig = Signal.buy(symbol="AAPL", strategy_id="test", max_cost_pct=0.01)
        assert sig.max_cost_pct == 0.01

    def test_signal_defaults_null_optional_fields(self):
        sig = Signal.buy(symbol="AAPL", strategy_id="test")
        assert sig.quantity is None
        assert sig.stop_loss_pct is None
        assert sig.take_profit_pct is None
        assert sig.max_cost_pct is None

    def test_signal_strength_values(self):
        assert SignalStrength.STRONG == "strong"
        assert SignalStrength.MODERATE == "moderate"
        assert SignalStrength.WEAK == "weak"


class TestSideEnum:
    def test_side_values(self):
        assert Side.BUY == "buy"
        assert Side.SELL == "sell"
        assert Side.HOLD == "hold"


class TestSignalBatchEvaluationTime:
    def test_default_evaluation_time(self):
        batch = SignalBatch(strategy_id="test")
        assert batch.evaluation_time_ms == 0.0

    def test_custom_evaluation_time(self):
        batch = SignalBatch(strategy_id="test", evaluation_time_ms=42.5)
        assert batch.evaluation_time_ms == 42.5


class TestSignalInstrument:
    def test_instrument_auto_populated(self):
        sig = Signal.buy(symbol="AAPL", strategy_id="test")
        assert sig.instrument is not None

    def test_instrument_symbol_matches(self):
        sig = Signal.buy(symbol="MSFT", strategy_id="test")
        assert sig.instrument.symbol == "MSFT"
