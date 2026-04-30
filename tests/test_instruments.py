"""Tests for engine.core.instruments — abstract Instrument model."""

from __future__ import annotations

from datetime import date

import pytest

from engine.core.instruments import (
    Instrument,
    InstrumentAssetClass,
    OptionType,
)


class TestEquityFactory:
    def test_equity_basic(self):
        inst = Instrument.equity("AAPL")
        assert inst.symbol == "AAPL"
        assert inst.asset_class == InstrumentAssetClass.EQUITY
        assert inst.currency == "USD"
        assert inst.uid == "AAPL"
        assert inst.is_derivative is False

    def test_equity_with_exchange(self):
        inst = Instrument.equity("AAPL", exchange="NASDAQ")
        assert inst.exchange == "NASDAQ"
        assert inst.uid == "AAPL"


class TestCryptoFactory:
    def test_crypto_pair(self):
        inst = Instrument.crypto("BTC", "USDT", exchange="BINANCE")
        assert inst.asset_class == InstrumentAssetClass.CRYPTO
        assert inst.base_asset == "BTC"
        assert inst.quote_asset == "USDT"
        assert inst.exchange == "BINANCE"
        assert inst.uid == "BTC/USDT"
        assert inst.symbol == "BTC/USDT"

    def test_crypto_perp(self):
        inst = Instrument.crypto_perp("BTC", "USDT", exchange="BINANCE")
        assert inst.asset_class == InstrumentAssetClass.CRYPTO_PERP
        assert inst.is_derivative is True


class TestForexFactory:
    def test_forex_major(self):
        inst = Instrument.forex("EUR", "USD")
        assert inst.asset_class == InstrumentAssetClass.FOREX
        assert inst.base_asset == "EUR"
        assert inst.quote_asset == "USD"
        assert inst.uid == "EUR/USD"
        assert inst.pip_size == pytest.approx(0.0001)
        assert inst.lot_size == 100_000

    def test_forex_jpy_pair_uses_2_decimal_pip(self):
        inst = Instrument.forex("USD", "JPY")
        assert inst.pip_size == pytest.approx(0.01)


class TestOptionFactory:
    def test_call_option(self):
        inst = Instrument.option(
            "AAPL",
            strike=200.0,
            expiration=date(2026, 6, 19),
            option_type=OptionType.CALL,
        )
        assert inst.asset_class == InstrumentAssetClass.OPTION
        assert inst.underlying == "AAPL"
        assert inst.strike == 200.0
        assert inst.expiration == date(2026, 6, 19)
        assert inst.option_type == OptionType.CALL
        assert inst.multiplier == 100
        assert inst.is_derivative is True
        assert inst.uid == "AAPL_20260619_C_200.00"

    def test_put_option_uid(self):
        inst = Instrument.option(
            "TSLA",
            strike=150.5,
            expiration=date(2026, 12, 18),
            option_type=OptionType.PUT,
        )
        assert inst.uid == "TSLA_20261218_P_150.50"

    def test_contract_value_for_option(self):
        inst = Instrument.option(
            "AAPL",
            strike=200.0,
            expiration=date(2026, 6, 19),
            option_type=OptionType.CALL,
        )
        assert inst.contract_value == 200.0 * 100


class TestUidUniqueness:
    def test_equity_vs_crypto_distinct(self):
        a = Instrument.equity("BTC")
        b = Instrument.crypto("BTC", "USDT")
        assert a.uid != b.uid

    def test_two_options_with_different_strikes_distinct(self):
        a = Instrument.option(
            "AAPL", 200.0, date(2026, 6, 19), OptionType.CALL
        )
        b = Instrument.option(
            "AAPL", 210.0, date(2026, 6, 19), OptionType.CALL
        )
        assert a.uid != b.uid


class TestInstrumentValidation:
    def test_option_missing_fields_rejected(self):
        with pytest.raises((ValueError, TypeError)):
            Instrument(
                symbol="AAPL",
                asset_class=InstrumentAssetClass.OPTION,
            )

    def test_negative_strike_rejected(self):
        with pytest.raises((ValueError, TypeError)):
            Instrument.option(
                "AAPL", -1.0, date(2026, 6, 19), OptionType.CALL
            )


class TestSerialization:
    def test_round_trip_through_dict(self):
        inst = Instrument.option(
            "AAPL", 200.0, date(2026, 6, 19), OptionType.CALL
        )
        as_dict = inst.model_dump(mode="json")
        rebuilt = Instrument.model_validate(as_dict)
        assert rebuilt.uid == inst.uid
        assert rebuilt.option_type == OptionType.CALL


class TestFromString:
    def test_from_string_equity(self):
        inst = Instrument.from_string("AAPL")
        assert inst.asset_class == InstrumentAssetClass.EQUITY
        assert inst.symbol == "AAPL"

    def test_from_string_crypto_pair(self):
        inst = Instrument.from_string("BTC/USDT")
        assert inst.symbol == "BTC/USDT"

    def test_from_string_forex_short_pair_does_not_crash(self):
        inst = Instrument.from_string("EUR/USD")
        assert inst.symbol == "EUR/USD"
