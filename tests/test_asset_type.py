"""Tests for the public ``AssetType`` taxonomy and its integration into
:class:`engine.core.instruments.Instrument`.

Covers the three behaviors required when ``asset_type`` was switched from
a ``STOCK`` default to a ``None`` sentinel:

1. An explicit, contradictory ``asset_type`` (e.g. ``STOCK`` on an
   ``option`` ``asset_class``) is rejected rather than silently rewritten.
2. An omitted ``asset_type`` (``None``) still auto-syncs from
   ``asset_class`` so legacy callers keep seeing a coherent value.
3. A contradictory ``asset_type`` on an ``equity`` ``asset_class`` is
   caught by cross-validation.

Also exercises the standalone ``AssetType`` coercion helpers so the
public enum contract stays covered.
"""

from __future__ import annotations

from datetime import date

import pydantic
import pytest

from engine.core.instruments import (
    Instrument,
    InstrumentAssetClass,
    OptionType,
)
from engine.instruments import AssetType


class TestAssetTypeAutoSync:
    """Requirement (2): omitted ``asset_type`` auto-syncs from asset_class."""

    @pytest.mark.parametrize(
        ("factory", "args", "expected"),
        [
            (Instrument.equity, ("AAPL",), AssetType.STOCK),
            (Instrument.etf, ("SPY",), AssetType.ETF),
            (Instrument.crypto, ("BTC", "USDT"), AssetType.CRYPTO),
            (Instrument.crypto_perp, ("BTC", "USDT"), AssetType.CRYPTO),
            (Instrument.forex, ("EUR", "USD"), AssetType.FOREX),
            (Instrument.future, ("ES", date(2026, 12, 19)), AssetType.FUTURE),
        ],
    )
    def test_factory_syncs_asset_type(self, factory, args, expected):
        inst = factory(*args)
        assert inst.asset_type == expected

    def test_explicit_none_syncs_to_implied(self):
        # ``asset_type=None`` is the sentinel meaning "not provided";
        # the validator fills it in from asset_class.
        inst = Instrument(
            symbol="BTC/USDT",
            asset_class=InstrumentAssetClass.CRYPTO,
            base_asset="BTC",
            quote_asset="USDT",
            asset_type=None,
        )
        assert inst.asset_type == AssetType.CRYPTO

    def test_from_string_path_syncs_to_stock(self):
        # The legacy string path must still produce a STOCK asset_type.
        inst = Instrument.from_string("AAPL")
        assert inst.asset_type == AssetType.STOCK

    def test_crypto_future_syncs_to_crypto(self):
        inst = Instrument(
            symbol="BTC/USDT:20260328",
            asset_class=InstrumentAssetClass.CRYPTO_FUTURE,
            base_asset="BTC",
            quote_asset="USDT",
            expiration=date(2026, 3, 28),
        )
        assert inst.asset_type == AssetType.CRYPTO


class TestContradictoryAssetTypeRejected:
    """Requirement (1) & (3): explicit contradictions raise."""

    def test_explicit_stock_on_option_class_raises(self):
        # (1) explicit asset_type=STOCK on an OPTION asset_class must not
        # be silently preserved as STOCK; it contradicts the class and is
        # rejected so callers learn about the mismatch.
        with pytest.raises(pydantic.ValidationError, match="contradicts"):
            Instrument(
                symbol="AAPL_20260619_C_200.00",
                asset_class=InstrumentAssetClass.OPTION,
                underlying="AAPL",
                strike=200.0,
                expiration=date(2026, 6, 19),
                option_type=OptionType.CALL,
                asset_type=AssetType.STOCK,
            )

    def test_contradictory_asset_type_on_equity_raises(self):
        # (3) A contradictory explicit asset_type on an equity is caught.
        with pytest.raises(pydantic.ValidationError, match="contradicts"):
            Instrument(
                symbol="AAPL",
                asset_class=InstrumentAssetClass.EQUITY,
                asset_type=AssetType.OPTION,
            )

    def test_string_form_of_contradictory_asset_type_raises(self):
        # The validator should normalize string-typed asset_type the same
        # way (round-trips through AssetType.from_string semantics) and
        # still flag the mismatch.
        with pytest.raises(pydantic.ValidationError, match="contradicts"):
            Instrument(
                symbol="AAPL",
                asset_class=InstrumentAssetClass.EQUITY,
                asset_type="crypto",
            )

    def test_explicit_matching_asset_type_is_preserved(self):
        # Sanity: an explicit value that *agrees* with asset_class is fine
        # and survives construction untouched.
        inst = Instrument(
            symbol="AAPL",
            asset_class=InstrumentAssetClass.EQUITY,
            asset_type=AssetType.STOCK,
        )
        assert inst.asset_type == AssetType.STOCK


class TestAssetTypeSerialization:
    """The synced asset_type must survive serialization round-trips."""

    def test_round_trip_preserves_synced_asset_type(self):
        inst = Instrument.option("AAPL", 200.0, date(2026, 6, 19), OptionType.CALL)
        payload = inst.model_dump(mode="json")
        # The dumped value matches what the sync produced.
        assert payload["asset_type"] == "option"
        rebuilt = Instrument.model_validate(payload)
        assert rebuilt.asset_type == AssetType.OPTION
        assert rebuilt.uid == inst.uid

    def test_round_trip_does_not_trip_contradiction_check(self):
        # Because the synced value agrees with asset_class, rebuilding a
        # dumped instrument must NOT raise the contradiction error.
        for inst in (
            Instrument.equity("AAPL"),
            Instrument.crypto("BTC", "USDT"),
            Instrument.forex("EUR", "USD"),
        ):
            rebuilt = Instrument.model_validate(inst.model_dump(mode="json"))
            assert rebuilt.asset_type == inst.asset_type


class TestAssetTypeEnum:
    """Standalone coverage for the public AssetType coercion helpers."""

    def test_from_string_passthrough(self):
        assert AssetType.from_string(AssetType.STOCK) == AssetType.STOCK

    def test_from_string_case_insensitive(self):
        assert AssetType.from_string(" Stock ") == AssetType.STOCK
        assert AssetType.from_string("CRYPTO") == AssetType.CRYPTO

    def test_from_string_unknown_raises(self):
        with pytest.raises(ValueError, match="unknown asset type"):
            AssetType.from_string("bond")

    def test_from_string_wrong_type_raises(self):
        with pytest.raises(TypeError, match="cannot parse AssetType"):
            AssetType.from_string(42)

    @pytest.mark.parametrize(
        ("asset_class", "asset_type"),
        [
            ("equity", AssetType.STOCK),
            ("etf", AssetType.ETF),
            ("option", AssetType.OPTION),
            ("future", AssetType.FUTURE),
            ("forex", AssetType.FOREX),
            ("crypto", AssetType.CRYPTO),
            ("crypto_perp", AssetType.CRYPTO),
            ("crypto_future", AssetType.CRYPTO),
        ],
    )
    def test_from_asset_class_mapping(self, asset_class, asset_type):
        assert AssetType.from_asset_class(asset_class) == asset_type

    def test_from_asset_class_unknown_raises(self):
        with pytest.raises(ValueError, match="unsupported asset_class"):
            AssetType.from_asset_class("bond")

    def test_from_instrument_prefers_asset_type(self):
        inst = Instrument.equity("AAPL")
        assert AssetType.from_instrument(inst) == AssetType.STOCK

    def test_from_instrument_falls_back_to_asset_class(self):
        class BareDTO:
            asset_class = "crypto"

        assert AssetType.from_instrument(BareDTO()) == AssetType.CRYPTO

    def test_from_instrument_ignores_unparseable_asset_type(self):
        # If asset_type is present but cannot be parsed, fall through to
        # the asset_class bridge instead of failing outright.
        class DtoWithBadType:
            asset_type = "not-a-real-type"
            asset_class = "forex"

        assert AssetType.from_instrument(DtoWithBadType()) == AssetType.FOREX

    def test_from_instrument_no_attributes_raises(self):
        with pytest.raises(ValueError, match="cannot determine AssetType"):
            AssetType.from_instrument(object())
