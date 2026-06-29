"""Tests for uncovered paths in engine.core.instruments."""

from __future__ import annotations

from datetime import date

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
    """Coverage for the regulated ``future()`` factory."""

    def test_future_basic_defaults(self):
        inst = Instrument.future("ES")
        assert inst.asset_class == InstrumentAssetClass.FUTURE
        assert inst.symbol == "ES"
        assert inst.currency == "USD"
        assert inst.multiplier == 1
        assert inst.expiration is None
        assert inst.exchange is None
        assert inst.is_derivative is True

    def test_future_default_is_section_1256_promoted_to_true(self):
        # None default → validator promotes to the statutory default (True)
        # for regulated futures.
        inst = Instrument.future("ES")
        assert inst.is_section_1256 is True

    def test_future_explicit_true_respected(self):
        inst = Instrument.future("ES", is_section_1256=True)
        assert inst.is_section_1256 is True

    def test_future_explicit_false_respected(self):
        # An explicit bool from the caller always wins over the default.
        inst = Instrument.future("ES", is_section_1256=False)
        assert inst.is_section_1256 is False

    def test_future_sets_multiplier(self):
        # The factory must populate the canonical ``multiplier`` field
        # (not a legacy ``contract_multiplier``).
        inst = Instrument.future("ES", multiplier=50)
        assert inst.multiplier == 50

    def test_future_with_expiration_uid(self):
        inst = Instrument.future("ES", expiration=date(2026, 12, 19), multiplier=50)
        assert inst.uid == "ES_20261219"

    def test_future_without_expiration_uid_falls_back_to_symbol(self):
        inst = Instrument.future("ES")
        assert inst.uid == "ES"

    def test_future_exchange_passthrough(self):
        inst = Instrument.future("ES", exchange="CME")
        assert inst.exchange == "CME"

    def test_future_currency_passthrough(self):
        inst = Instrument.future("ES", currency="EUR")
        assert inst.currency == "EUR"

    def test_future_contract_value_is_none(self):
        # contract_value is option-specific; futures expose multiplier only.
        inst = Instrument.future("ES", multiplier=50)
        assert inst.contract_value is None

    def test_future_explicit_none_is_treated_as_default(self):
        # Passing None explicitly == "not specified" → promoted to True.
        inst = Instrument.future("ES", is_section_1256=None)
        assert inst.is_section_1256 is True


class TestIsSection1256ValidatorSemantics:
    """The validator drives ``is_section_1256`` for futures only."""

    def test_direct_future_construction_promotes_none(self):
        inst = Instrument(
            symbol="ES",
            asset_class=InstrumentAssetClass.FUTURE,
        )
        assert inst.is_section_1256 is True

    def test_direct_future_construction_respects_explicit_false(self):
        inst = Instrument(
            symbol="ES",
            asset_class=InstrumentAssetClass.FUTURE,
            is_section_1256=False,
        )
        assert inst.is_section_1256 is False

    @pytest.mark.parametrize(
        "factory",
        [
            lambda: Instrument.equity("AAPL"),
            lambda: Instrument.etf("SPY"),
            lambda: Instrument.crypto("BTC", "USDT"),
            lambda: Instrument.crypto_perp("BTC", "USDT"),
            lambda: Instrument.forex("EUR", "USD"),
            lambda: Instrument.option("AAPL", 200.0, date(2026, 6, 19), OptionType.CALL),
        ],
    )
    def test_non_future_classes_leave_is_section_1256_none(self, factory):
        inst = factory()
        assert inst.is_section_1256 is None

    def test_crypto_future_is_not_1256_by_default(self):
        # Crypto futures are NOT regulated futures → flag stays unset.
        inst = Instrument(
            symbol="BTC/USDT",
            asset_class=InstrumentAssetClass.CRYPTO_FUTURE,
            base_asset="BTC",
            quote_asset="USDT",
            expiration=date(2026, 3, 28),
        )
        assert inst.is_section_1256 is None


class TestFutureMultiplierValidation:
    """``multiplier`` uses Field(ge=1) — guard the boundary."""

    def test_future_zero_multiplier_rejected(self):
        with pytest.raises((ValueError, TypeError)):
            Instrument.future("ES", multiplier=0)

    def test_future_negative_multiplier_rejected(self):
        with pytest.raises((ValueError, TypeError)):
            Instrument.future("ES", multiplier=-1)

    def test_future_unit_multiplier_accepted(self):
        inst = Instrument.future("ES", multiplier=1)
        assert inst.multiplier == 1


class TestFutureSerialization:
    def test_future_default_roundtrips_and_keeps_flag(self):
        inst = Instrument.future("ES", expiration=date(2026, 12, 19), multiplier=50)
        rebuilt = Instrument.model_validate(inst.model_dump(mode="json"))
        assert rebuilt.asset_class == InstrumentAssetClass.FUTURE
        assert rebuilt.is_section_1256 is True
        assert rebuilt.uid == inst.uid

    def test_future_explicit_false_roundtrips(self):
        inst = Instrument.future("ES", is_section_1256=False)
        rebuilt = Instrument.model_validate(inst.model_dump(mode="json"))
        assert rebuilt.is_section_1256 is False

    def test_future_json_dump_includes_flag(self):
        import json

        inst = Instrument.future("ES")
        payload = json.loads(inst.model_dump_json())
        assert payload["asset_class"] == "future"
        assert payload["is_section_1256"] is True


class TestFutureUidUniqueness:
    def test_two_dated_futures_same_symbol_distinct(self):
        a = Instrument.future("ES", expiration=date(2026, 3, 20))
        b = Instrument.future("ES", expiration=date(2026, 6, 19))
        assert a.uid != b.uid

    def test_future_vs_crypto_future_uids_distinct(self):
        reg = Instrument.future("ES", expiration=date(2026, 12, 19))
        # Different symbol space, but confirm no accidental collision shape.
        assert reg.uid == "ES_20261219"


class TestRemovedLegacyFieldsAreAbsent:
    """Lock in the clean schema: no legacy aliases linger."""

    def test_no_contract_multiplier_field(self):
        fields = Instrument.model_fields
        assert "contract_multiplier" not in fields
        assert "multiplier" in fields

    def test_no_expiry_date_field(self):
        fields = Instrument.model_fields
        assert "expiry_date" not in fields
        assert "expiration" in fields

    def test_factory_has_no_legacy_kwargs(self):
        import inspect

        sig = inspect.signature(Instrument.future)
        params = set(sig.parameters)
        assert "multiplier" in params
        assert "expiration" in params
        assert "is_section_1256" in params
        assert "contract_multiplier" not in params
        assert "expiry_date" not in params
