"""Tests for Signal ↔ Instrument backward-compat."""

from __future__ import annotations

from engine.core.instruments import Instrument, InstrumentAssetClass
from engine.core.signal import Side, Signal


class TestSymbolOnlyStillWorks:
    def test_signal_with_symbol_string_creates_equity_instrument(self):
        s = Signal(symbol="AAPL", side=Side.BUY, strategy_id="strat-1")
        assert s.symbol == "AAPL"
        assert s.instrument is not None
        assert s.instrument.asset_class == InstrumentAssetClass.EQUITY
        assert s.instrument.uid == "AAPL"

    def test_forex_pair_string_does_not_misclassify_as_crypto(self):
        # Conservative default: bare strings are always equity-by-default.
        # Forex/crypto callers must use the explicit factory.
        s = Signal(symbol="EUR/USD", side=Side.BUY, strategy_id="strat-fx")
        assert s.instrument.asset_class == InstrumentAssetClass.EQUITY

    def test_share_class_notation_does_not_misclassify_as_crypto(self):
        s = Signal(symbol="BRK/B", side=Side.BUY, strategy_id="strat-eq")
        assert s.instrument.asset_class == InstrumentAssetClass.EQUITY


class TestExplicitInstrument:
    def test_signal_with_full_instrument(self):
        inst = Instrument.crypto("BTC", "USDT", exchange="BINANCE")
        s = Signal(
            symbol=inst.symbol,
            instrument=inst,
            side=Side.BUY,
            strategy_id="strat-1",
        )
        assert s.instrument is inst
        assert s.symbol == "BTC/USDT"

    def test_buy_factory_keeps_string_path(self):
        s = Signal.buy("MSFT", "strat-2")
        assert s.symbol == "MSFT"
        assert s.instrument is not None
        assert s.instrument.symbol == "MSFT"
