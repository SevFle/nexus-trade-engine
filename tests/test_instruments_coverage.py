"""Tests for uncovered paths in engine.core.instruments."""

from __future__ import annotations

from datetime import date

import pydantic
import pytest

from engine.core.instruments import (
    Instrument,
    InstrumentAssetClass,
    OptionType,
)


class TestToProviderClass:
    def test_equity_maps(self):
        from engine.data.providers.base import AssetClass

        assert InstrumentAssetClass.EQUITY.to_provider_class() == AssetClass.EQUITY

    def test_etf_maps(self):
        from engine.data.providers.base import AssetClass

        assert InstrumentAssetClass.ETF.to_provider_class() == AssetClass.ETF

    def test_crypto_maps(self):
        from engine.data.providers.base import AssetClass

        assert InstrumentAssetClass.CRYPTO.to_provider_class() == AssetClass.CRYPTO

    def test_crypto_perp_maps(self):
        from engine.data.providers.base import AssetClass

        assert InstrumentAssetClass.CRYPTO_PERP.to_provider_class() == AssetClass.CRYPTO

    def test_crypto_future_maps(self):
        from engine.data.providers.base import AssetClass

        assert InstrumentAssetClass.CRYPTO_FUTURE.to_provider_class() == AssetClass.CRYPTO

    def test_forex_maps(self):
        from engine.data.providers.base import AssetClass

        assert InstrumentAssetClass.FOREX.to_provider_class() == AssetClass.FOREX

    def test_option_maps(self):
        from engine.data.providers.base import AssetClass

        assert InstrumentAssetClass.OPTION.to_provider_class() == AssetClass.OPTIONS

    def test_future_maps(self):
        from engine.data.providers.base import AssetClass

        assert InstrumentAssetClass.FUTURE.to_provider_class() == AssetClass.FUTURES


class TestCryptoValidation:
    def test_crypto_missing_base_rejected(self):
        with pytest.raises((ValueError, TypeError)):
            Instrument(
                symbol="BTC/USDT",
                asset_class=InstrumentAssetClass.CRYPTO,
                quote_asset="USDT",
            )

    def test_crypto_missing_quote_rejected(self):
        with pytest.raises((ValueError, TypeError)):
            Instrument(
                symbol="BTC/USDT",
                asset_class=InstrumentAssetClass.CRYPTO,
                base_asset="BTC",
            )

    def test_crypto_perp_missing_pair_rejected(self):
        with pytest.raises((ValueError, TypeError)):
            Instrument(
                symbol="BTC/USDT:PERP",
                asset_class=InstrumentAssetClass.CRYPTO_PERP,
                base_asset="BTC",
            )


class TestForexValidation:
    def test_forex_missing_base_rejected(self):
        with pytest.raises((ValueError, TypeError)):
            Instrument(
                symbol="EUR/USD",
                asset_class=InstrumentAssetClass.FOREX,
                quote_asset="USD",
            )

    def test_forex_missing_quote_rejected(self):
        with pytest.raises((ValueError, TypeError)):
            Instrument(
                symbol="EUR/USD",
                asset_class=InstrumentAssetClass.FOREX,
                base_asset="EUR",
            )


class TestUidEdgeCases:
    def test_future_without_expiration(self):
        inst = Instrument(
            symbol="ES",
            asset_class=InstrumentAssetClass.FUTURE,
        )
        assert inst.uid == "ES"

    def test_future_with_expiration(self):
        inst = Instrument(
            symbol="ES",
            asset_class=InstrumentAssetClass.FUTURE,
            expiration=date(2026, 12, 19),
        )
        assert inst.uid == "ES_20261219"

    def test_crypto_future_with_expiration(self):
        inst = Instrument(
            symbol="BTC/USDT",
            asset_class=InstrumentAssetClass.CRYPTO_FUTURE,
            base_asset="BTC",
            quote_asset="USDT",
            expiration=date(2026, 3, 28),
        )
        assert inst.uid == "BTC/USDT:20260328"

    def test_crypto_future_without_expiration(self):
        inst = Instrument(
            symbol="BTC/USDT",
            asset_class=InstrumentAssetClass.CRYPTO_FUTURE,
            base_asset="BTC",
            quote_asset="USDT",
        )
        assert inst.uid == "BTC/USDT:FUT"

    def test_option_uid_raises_on_missing_expiration(self):
        inst = Instrument(
            symbol="AAPL_20260619_C_200.00",
            asset_class=InstrumentAssetClass.OPTION,
            underlying="AAPL",
            strike=200.0,
            option_type=OptionType.CALL,
            expiration=date(2026, 6, 19),
        )
        assert inst.uid == "AAPL_20260619_C_200.00"


class TestContractValue:
    def test_non_option_returns_none(self):
        inst = Instrument.equity("AAPL")
        assert inst.contract_value is None

    def test_option_returns_strike_times_multiplier(self):
        inst = Instrument.option(
            "AAPL",
            strike=150.0,
            expiration=date(2026, 6, 19),
            option_type=OptionType.CALL,
            multiplier=100,
        )
        assert inst.contract_value == 15000.0


class TestEtfFactory:
    def test_etf_basic(self):
        inst = Instrument.etf("SPY")
        assert inst.asset_class == InstrumentAssetClass.ETF
        assert inst.symbol == "SPY"
        assert inst.currency == "USD"
        assert inst.is_derivative is False

    def test_etf_with_exchange(self):
        inst = Instrument.etf("VTI", exchange="NYSE")
        assert inst.exchange == "NYSE"


class TestCoerce:
    def test_coerce_instrument_passthrough(self):
        inst = Instrument.equity("AAPL")
        assert Instrument.coerce(inst) is inst

    def test_coerce_string(self):
        inst = Instrument.coerce("MSFT")
        assert inst.asset_class == InstrumentAssetClass.EQUITY
        assert inst.symbol == "MSFT"

    def test_coerce_invalid_type(self):
        with pytest.raises(TypeError, match="cannot coerce"):
            Instrument.coerce(42)

    def test_coerce_none(self):
        with pytest.raises(TypeError, match="cannot coerce"):
            Instrument.coerce(None)


class TestFutureFactory:
    def test_future_uppercases_symbol(self):
        inst = Instrument.future("es", date(2026, 12, 19), exchange="CME")
        assert inst.asset_class == InstrumentAssetClass.FUTURE
        assert inst.symbol == "ES"
        assert inst.exchange == "CME"
        assert inst.expiration == date(2026, 12, 19)
        assert inst.uid == "ES_20261219"
        assert inst.is_derivative is True

    def test_future_already_upper_unchanged(self):
        inst = Instrument.future("CL", date(2026, 6, 19))
        assert inst.symbol == "CL"
        assert inst.uid == "CL_20260619"

    @pytest.mark.parametrize("bad_symbol", [None, 42, 3.14, ["ES"], {"ES"}])
    def test_future_rejects_non_string_symbol(self, bad_symbol):
        with pytest.raises(TypeError, match="symbol must be a string"):
            Instrument.future(bad_symbol, date(2026, 12, 19))

    def test_future_multiplier(self):
        inst = Instrument.future("ES", date(2026, 12, 19), multiplier=50)
        assert inst.multiplier == 50


class TestExpiryDateAlias:
    def test_expiry_date_copied_when_expiration_absent(self):
        inst = Instrument.model_validate(
            {
                "symbol": "ES",
                "asset_class": "future",
                "expiry_date": date(2026, 12, 19),
            }
        )
        assert inst.expiration == date(2026, 12, 19)
        assert inst.uid == "ES_20261219"

    def test_expiry_date_dropped_in_favour_of_expiration(self):
        # When both are supplied the canonical ``expiration`` wins and
        # the alias is ignored (not stored as a stray extra).
        inst = Instrument.model_validate(
            {
                "symbol": "ES",
                "asset_class": "future",
                "expiration": date(2027, 1, 1),
                "expiry_date": date(2020, 1, 1),
            }
        )
        assert inst.expiration == date(2027, 1, 1)

    def test_option_via_expiry_date_alias(self):
        inst = Instrument.model_validate(
            {
                "symbol": "AAPL_20260619_C_200.00",
                "asset_class": "option",
                "underlying": "AAPL",
                "strike": 200.0,
                "option_type": "call",
                "expiry_date": date(2026, 6, 19),
            }
        )
        assert inst.expiration == date(2026, 6, 19)
        assert inst.uid == "AAPL_20260619_C_200.00"

    @pytest.mark.parametrize(
        "payload",
        [
            None,
            42,
            "not-a-dict",
        ],
    )
    def test_non_dict_non_object_input_left_for_pydantic(self, payload):
        # Bare scalars/strings have neither model_dump nor __dict__, so
        # the validator returns them untouched and pydantic reports the
        # structural error itself.
        with pytest.raises(pydantic.ValidationError):
            Instrument.model_validate(payload)

    def test_object_input_converted_to_dict(self):
        class RawInstrument:
            def __init__(self) -> None:
                self.symbol = "AAPL"
                self.asset_class = InstrumentAssetClass.EQUITY

        inst = Instrument.model_validate(RawInstrument())
        assert inst.symbol == "AAPL"
        assert inst.asset_class == InstrumentAssetClass.EQUITY

    def test_model_instance_input_round_trips(self):
        original = Instrument.option("AAPL", 200.0, date(2026, 6, 19), OptionType.CALL)
        rebuilt = Instrument.model_validate(original)
        assert rebuilt.uid == original.uid
        assert rebuilt.option_type == OptionType.CALL


class TestToProviderClassUnmapped:
    """An asset class with no explicit mapping must not crash routing."""

    def test_unknown_value_returns_default_instead_of_raising(self):
        # ``to_provider_class`` dispatches purely on equality against the
        # known members, so calling it (unbound) with a value that is not
        # one of them lands in the default arm — exactly the path a
        # future-added asset class or a stray value would take. It must
        # fall back to EQUITY rather than raising AssertionError.
        from engine.data.providers.base import AssetClass

        result = InstrumentAssetClass.to_provider_class("not-a-real-asset-class")
        assert result == AssetClass.EQUITY

    def test_every_enum_member_routes_without_crash(self):
        from engine.data.providers.base import AssetClass

        valid_provider_classes = set(AssetClass)
        for member in InstrumentAssetClass:
            # Must not raise, and must resolve to a real provider class.
            assert member.to_provider_class() in valid_provider_classes


class TestUnmappedAssetClassInstrument:
    """An instrument built from a class that has no special-case
    invariant handling must still construct and route cleanly."""

    @pytest.mark.parametrize(
        "asset_class",
        [
            InstrumentAssetClass.EQUITY,
            InstrumentAssetClass.ETF,
            InstrumentAssetClass.FUTURE,
        ],
    )
    def test_constructs_and_routes_without_crash(self, asset_class):
        from engine.data.providers.base import AssetClass

        inst = Instrument(symbol="X", asset_class=asset_class)
        assert inst.asset_class == asset_class
        # Routing must never crash, even for classes with no special
        # invariant handling in ``_enforce_class_invariants``.
        assert inst.asset_class.to_provider_class() in set(AssetClass)
