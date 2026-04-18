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
