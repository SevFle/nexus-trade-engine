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


class TestFuturesFactory:
    """Regulated futures contracts — data-model foundation (SEV-223+)."""

    def test_future_factory_basic(self):
        inst = Instrument.future(
            "ES",
            contract_multiplier=50,
            expiry_date=date(2026, 12, 19),
            exchange="CME",
            currency="USD",
        )
        assert inst.asset_class == InstrumentAssetClass.FUTURE
        assert inst.symbol == "ES"
        assert inst.contract_multiplier == 50
        assert inst.expiry_date == date(2026, 12, 19)
        assert inst.exchange == "CME"
        assert inst.currency == "USD"
        # A future is a derivative.
        assert inst.is_derivative is True
        # uid embeds the expiry so distinct contracts never collapse.
        assert inst.uid == "ES_20261219"

    def test_future_uid_without_expiry_falls_back_to_symbol(self):
        # No expiry or expiration → bare symbol uid (continuous/index style).
        inst = Instrument.future("ES", contract_multiplier=50)
        assert inst.uid == "ES"

    def test_future_uid_prefers_expiry_date_over_legacy_expiration(self):
        inst = Instrument(
            symbol="ES",
            asset_class=InstrumentAssetClass.FUTURE,
            expiry_date=date(2026, 12, 19),
            expiration=date(2025, 6, 20),
        )
        assert inst.uid == "ES_20261219"


class TestFuturesFieldValidation:
    def test_contract_multiplier_defaults_to_one(self):
        inst = Instrument.future("ES")
        assert inst.contract_multiplier == 1

    def test_zero_contract_multiplier_rejected(self):
        with pytest.raises((ValueError, TypeError)):
            Instrument.future("ES", contract_multiplier=0)

    def test_negative_contract_multiplier_rejected(self):
        with pytest.raises((ValueError, TypeError)):
            Instrument.future("ES", contract_multiplier=-50)

    def test_expiry_date_is_optional(self):
        inst = Instrument.future("ES", contract_multiplier=50)
        assert inst.expiry_date is None


class TestSection1256Flag:
    def test_factory_defaults_section_1256_true(self):
        inst = Instrument.future("ES", contract_multiplier=50)
        assert inst.is_section_1256 is True

    def test_direct_construction_auto_enables_section_1256(self):
        # Constructing a FUTURE directly (no factory) still defaults to
        # §1256 treatment so callers can't accidentally skip it.
        inst = Instrument(
            symbol="ES",
            asset_class=InstrumentAssetClass.FUTURE,
            contract_multiplier=50,
        )
        assert inst.is_section_1256 is True

    def test_section_1256_opt_out_respected(self):
        # §1256(d) opt-out (or non-qualifying contract) must survive both
        # factory and direct construction paths.
        via_factory = Instrument.future(
            "ES", contract_multiplier=50, is_section_1256=False
        )
        assert via_factory.is_section_1256 is False

        direct = Instrument(
            symbol="ES",
            asset_class=InstrumentAssetClass.FUTURE,
            contract_multiplier=50,
            is_section_1256=False,
        )
        assert direct.is_section_1256 is False

    def test_non_future_instruments_default_section_1256_false(self):
        assert Instrument.equity("AAPL").is_section_1256 is False
        assert Instrument.crypto("BTC", "USDT").is_section_1256 is False

    def test_future_round_trips_section_1256_flag(self):
        inst = Instrument.future(
            "CL",
            contract_multiplier=1000,
            expiry_date=date(2026, 3, 20),
            is_section_1256=True,
        )
        rebuilt = Instrument.model_validate(inst.model_dump(mode="json"))
        assert rebuilt.asset_class == InstrumentAssetClass.FUTURE
        assert rebuilt.contract_multiplier == 1000
        assert rebuilt.expiry_date == date(2026, 3, 20)
        assert rebuilt.is_section_1256 is True
        assert rebuilt.uid == inst.uid
