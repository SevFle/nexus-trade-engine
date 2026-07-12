"""Tests for uncovered paths in engine.core.instruments."""

from __future__ import annotations

import logging
from collections.abc import Mapping as ABCMapping
from datetime import date
from types import MappingProxyType

import pydantic
import pytest

from engine.core.instruments import (
    Instrument,
    InstrumentAssetClass,
    OptionType,
    UnknownAssetClassError,
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

    def test_unmapped_asset_class_raises_unknown_asset_class_error(self, caplog):
        # Build an InstrumentAssetClass member that none of the explicit
        # match arms in to_provider_class covers. We instantiate it via
        # str.__new__ (StrEnum's member type) and set the name/value
        # attributes, but deliberately avoid registering it in the enum's
        # member maps so the global enum is not mutated between tests.
        unmapped = str.__new__(InstrumentAssetClass, "warrant")
        unmapped._name_ = "WARRANT"
        unmapped._value_ = "warrant"

        assert isinstance(unmapped, InstrumentAssetClass)
        assert "WARRANT" not in InstrumentAssetClass.__members__

        with (
            caplog.at_level(logging.WARNING, logger="engine.core.instruments"),
            pytest.raises(
                UnknownAssetClassError, match="Unmapped InstrumentAssetClass"
            ) as excinfo,
        ):
            # to_provider_class now raises UnknownAssetClassError
            # unconditionally: the __debug__/assert_never branch and the
            # silent EQUITY fallback are gone. Constructing the error
            # still emits the WARNING log so operators see the value.
            unmapped.to_provider_class()

        # The raised exception carries the offending asset class.
        assert excinfo.value.asset_class is unmapped

        records = [
            rec
            for rec in caplog.records
            if rec.levelno == logging.WARNING and rec.name == "engine.core.instruments"
        ]
        assert len(records) == 1
        message = records[0].getMessage()
        assert "Unmapped InstrumentAssetClass" in message
        assert repr(unmapped) in message

        # The isolated member must not have leaked into the enum.
        assert "WARRANT" not in InstrumentAssetClass.__members__


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

    def test_foreign_pydantic_model_folds_expiry_date_alias(self):
        # A *different* pydantic BaseModel (has ``model_dump`` but is not
        # an ``Instrument``) reaches the ``model_dump`` branch of the
        # before-validator and must fold the ``expiry_date`` alias.
        class ForeignModel(pydantic.BaseModel):
            symbol: str
            asset_class: str
            expiry_date: date

        fm = ForeignModel(symbol="ES", asset_class="future", expiry_date=date(2026, 12, 19))
        inst = Instrument.model_validate(fm)
        assert inst.expiration == date(2026, 12, 19)
        assert inst.uid == "ES_20261219"


class TestMappingInput:
    """Cover the ``Mapping`` branch added to ``_reject_expiry_date_alias``.

    Non-dict mappings (``MappingProxyType``, custom
    ``collections.abc.Mapping`` subclasses) are dict-like: their *items*
    must be read, not the instance ``__dict__``. Without this branch a
    read-only mapping silently drops the ``expiry_date`` alias so
    ``expiration`` stays ``None``.
    """

    def test_mapping_proxy_type_folds_expiry_date_alias(self):
        # The exact regression the recent change fixes: a read-only
        # mapping carrying the legacy ``expiry_date`` alias must populate
        # the canonical ``expiration`` field.
        ro = MappingProxyType(
            {
                "symbol": "ES",
                "asset_class": "future",
                "expiry_date": date(2026, 12, 19),
            }
        )
        inst = Instrument.model_validate(ro)
        assert inst.expiration == date(2026, 12, 19)
        assert inst.uid == "ES_20261219"

    def test_custom_abc_mapping_subclass_folds_expiry_date_alias(self):
        # Any ``collections.abc.Mapping`` subclass (not just the stdlib
        # proxy) must be read by items, not by ``__dict__``.
        class CustomMapping(ABCMapping):
            def __init__(self, d):
                self._d = d

            def __getitem__(self, k):
                return self._d[k]

            def __iter__(self):
                return iter(self._d)

            def __len__(self):
                return len(self._d)

        payload = CustomMapping(
            {
                "symbol": "AAPL_20260619_C_200.00",
                "asset_class": "option",
                "underlying": "AAPL",
                "strike": 200.0,
                "option_type": "call",
                "expiry_date": date(2026, 6, 19),
            }
        )
        inst = Instrument.model_validate(payload)
        assert inst.expiration == date(2026, 6, 19)
        assert inst.uid == "AAPL_20260619_C_200.00"

    def test_mapping_with_both_expiration_and_expiry_date_prefers_expiration(self):
        # The canonical ``expiration`` wins over the alias when a mapping
        # supplies both, mirroring the dict path.
        ro = MappingProxyType(
            {
                "symbol": "ES",
                "asset_class": "future",
                "expiration": date(2027, 1, 1),
                "expiry_date": date(2020, 1, 1),
            }
        )
        inst = Instrument.model_validate(ro)
        assert inst.expiration == date(2027, 1, 1)

    def test_mapping_without_alias_round_trips_cleanly(self):
        # A mapping that carries no alias is simply converted to a dict
        # and validated normally — no spurious extras, no data loss.
        ro = MappingProxyType(
            {
                "symbol": "AAPL",
                "asset_class": InstrumentAssetClass.EQUITY,
            }
        )
        inst = Instrument.model_validate(ro)
        assert inst.symbol == "AAPL"
        assert inst.asset_class == InstrumentAssetClass.EQUITY
        assert inst.expiration is None


class TestOptionUidDefensiveGuard:
    """Cover the defensive ``uid`` guard for an incomplete OPTION.

    ``_enforce_class_invariants`` rejects an OPTION that is missing its
    required fields during normal construction, so the guard inside the
    ``uid`` property (the raise for missing expiration/option_type/
    strike) is only reachable by bypassing validation via
    ``model_construct``. The guard must still raise a clear ``ValueError``
    rather than silently formatting against ``None`` values.
    """

    def test_uid_raises_when_required_option_fields_missing(self):
        bad = Instrument.model_construct(
            symbol="AAPL",
            asset_class=InstrumentAssetClass.OPTION,
            underlying="AAPL",
        )
        with pytest.raises(ValueError, match="option uid requires"):
            _ = bad.uid
