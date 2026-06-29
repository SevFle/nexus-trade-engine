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
        # uid carries an explicit FX marker so it cannot collide with a
        # crypto pair on the same base/quote letters (e.g. EUR/USD vs
        # a hypothetical EUR/USD crypto listing).
        assert inst.uid == "EUR/USD:FX"
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

    def test_spot_vs_perp_vs_future_uids_distinct(self):
        spot = Instrument.crypto("BTC", "USDT")
        perp = Instrument.crypto_perp("BTC", "USDT")
        # CRYPTO_FUTURE uid includes expiration when set; without
        # expiration falls back to ":FUT" suffix.
        future_dated = Instrument(
            symbol="BTC/USDT",
            asset_class=InstrumentAssetClass.CRYPTO_FUTURE,
            base_asset="BTC",
            quote_asset="USDT",
            expiration=date(2026, 12, 26),
        )
        future_perpetual = Instrument(
            symbol="BTC/USDT",
            asset_class=InstrumentAssetClass.CRYPTO_FUTURE,
            base_asset="BTC",
            quote_asset="USDT",
        )
        uids = {spot.uid, perp.uid, future_dated.uid, future_perpetual.uid}
        assert len(uids) == 4, f"expected 4 distinct uids, got {uids}"

    def test_forex_pair_uid_includes_fx_marker(self):
        fx = Instrument.forex("EUR", "USD")
        crypto = Instrument.crypto("EUR", "USD")
        assert fx.uid != crypto.uid


class TestSymbolValidation:
    def test_empty_symbol_rejected(self):
        with pytest.raises((ValueError, TypeError)):
            Instrument.equity("")

    def test_whitespace_symbol_rejected(self):
        with pytest.raises((ValueError, TypeError)):
            Instrument.equity("   ")

    def test_from_string_rejects_empty(self):
        with pytest.raises(ValueError, match="non-empty"):
            Instrument.from_string("")

    def test_from_string_rejects_whitespace(self):
        with pytest.raises(ValueError, match="non-empty"):
            Instrument.from_string("   ")

    def test_from_string_strips_and_classifies_as_equity(self):
        # Conservative default: "/" no longer routes to crypto; share
        # classes and forex pairs survive without misclassification.
        for raw in ("AAPL", "EUR/USD", "BRK/B", "AAPL "):
            inst = Instrument.from_string(raw)
            assert inst.asset_class == InstrumentAssetClass.EQUITY


class TestSerializationRoundtrip:
    def test_string_enum_serializes_cleanly(self):
        import json

        inst = Instrument.equity("AAPL")
        payload = json.loads(inst.model_dump_json())
        assert payload["asset_class"] == "equity"

    def test_perp_round_trips_via_model_dump(self):
        a = Instrument.crypto_perp("BTC", "USDT", exchange="BINANCE")
        b = Instrument.model_validate(a.model_dump(mode="json"))
        assert b.asset_class == InstrumentAssetClass.CRYPTO_PERP
        assert b.uid == a.uid


class TestStrikeBoundary:
    def test_zero_strike_rejected(self):
        with pytest.raises((ValueError, TypeError)):
            Instrument.option(
                "AAPL", 0.0, date(2026, 6, 19), OptionType.CALL
            )


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


class TestFutureFactory:
    """The future() factory sets multiplier to the contract size and
    classifies as a derivative — there is no separate contract_multiplier."""

    def test_es_contract_size_defaults_multiplier_to_50(self):
        inst = Instrument.future("ES", expiration=date(2026, 12, 19))
        assert inst.asset_class == InstrumentAssetClass.FUTURE
        assert inst.multiplier == 50  # $50 x index, the ES contract size
        assert inst.expiration == date(2026, 12, 19)
        assert inst.is_derivative is True
        assert inst.uid == "ES_20261219"

    def test_known_contract_sizes(self):
        assert Instrument.future("NQ").multiplier == 20
        assert Instrument.future("CL").multiplier == 1000
        assert Instrument.future("GC").multiplier == 100

    def test_unknown_symbol_defaults_multiplier_to_one(self):
        inst = Instrument.future("WIDGET")
        assert inst.multiplier == 1

    def test_explicit_multiplier_overrides_contract_size(self):
        inst = Instrument.future("ES", multiplier=500)
        assert inst.multiplier == 500

    def test_no_contract_multiplier_field_exists(self):
        # The refactor removed contract_multiplier in favour of multiplier.
        assert "contract_multiplier" not in Instrument.model_fields
        inst = Instrument.future("ES")
        assert not hasattr(inst, "contract_multiplier")

    def test_case_insensitive_symbol_lookup(self):
        assert Instrument.future("es").multiplier == 50


class TestSection1256AutoDetection:
    """is_section_1256 defaults to None and is auto-set from model_fields_set."""

    def test_future_auto_detected_via_factory(self):
        # Factory omits the kwarg -> validator auto-detects True for futures.
        inst = Instrument.future("ES")
        assert inst.is_section_1256 is True

    def test_future_auto_detected_via_direct_construction(self):
        inst = Instrument(
            symbol="NQ",
            asset_class=InstrumentAssetClass.FUTURE,
        )
        assert inst.is_section_1256 is True

    def test_non_future_auto_detected_false(self):
        assert Instrument.equity("AAPL").is_section_1256 is False
        assert Instrument.etf("SPY").is_section_1256 is False
        assert Instrument.crypto("BTC", "USDT").is_section_1256 is False
        assert Instrument.option(
            "AAPL", 200.0, date(2026, 6, 19), OptionType.CALL
        ).is_section_1256 is False

    def test_factory_explicit_override_respected(self):
        inst = Instrument.future("ES", is_section_1256=False)
        assert inst.is_section_1256 is False

    def test_direct_explicit_override_respected(self):
        inst = Instrument(
            symbol="ES",
            asset_class=InstrumentAssetClass.FUTURE,
            is_section_1256=False,
        )
        assert inst.is_section_1256 is False

    def test_default_field_value_is_none_before_construction(self):
        # The field's declared default is None (auto-detect sentinel).
        assert Instrument.model_fields["is_section_1256"].default is None


class TestNoDuplicateFields:
    """Guards against re-introducing the removed duplicate fields."""

    def test_no_expiry_date_field(self):
        assert "expiry_date" not in Instrument.model_fields

    def test_no_contract_multiplier_field(self):
        assert "contract_multiplier" not in Instrument.model_fields

    def test_single_canonical_date_and_size_fields(self):
        # expiration and multiplier are the only canonical fields for these.
        assert "expiration" in Instrument.model_fields
        assert "multiplier" in Instrument.model_fields


class TestExpiryDateAlias:
    """expiration is canonical; a conflicting expiry_date must raise."""

    def test_conflicting_dates_raise(self):
        with pytest.raises(ValueError, match="expiry_date"):
            Instrument(
                symbol="ES",
                asset_class=InstrumentAssetClass.FUTURE,
                expiration=date(2026, 12, 19),
                expiry_date=date(2026, 6, 19),
            )

    def test_conflicting_dates_raise_via_validate(self):
        with pytest.raises(ValueError, match="expiration"):
            Instrument.model_validate(
                {
                    "symbol": "ES",
                    "asset_class": "future",
                    "expiration": "2026-12-19",
                    "expiry_date": "2026-06-19",
                }
            )

    def test_equal_dates_tolerated_and_expiration_kept(self):
        # Legacy callers passing the same value on both keys stay compatible.
        inst = Instrument.model_validate(
            {
                "symbol": "ES",
                "asset_class": "future",
                "expiration": "2026-12-19",
                "expiry_date": "2026-12-19",
            }
        )
        assert inst.expiration == date(2026, 12, 19)
        # expiry_date is not a field, so it never lands on the instance.
        assert not hasattr(inst, "expiry_date")

    def test_expiry_date_alone_is_ignored_not_stored(self):
        # expiration stays canonical: passing only the alias is a no-op.
        inst = Instrument.model_validate(
            {
                "symbol": "ES",
                "asset_class": "future",
                "expiry_date": "2026-12-19",
            }
        )
        assert inst.expiration is None
        assert not hasattr(inst, "expiry_date")


class TestFutureSerialization:
    def test_future_round_trips_through_dict(self):
        inst = Instrument.future("ES", expiration=date(2026, 12, 19))
        rebuilt = Instrument.model_validate(inst.model_dump(mode="json"))
        assert rebuilt.asset_class == InstrumentAssetClass.FUTURE
        assert rebuilt.multiplier == 50
        assert rebuilt.is_section_1256 is True
        assert rebuilt.uid == inst.uid
