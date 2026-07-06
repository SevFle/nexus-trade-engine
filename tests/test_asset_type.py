"""Tests for the public ``AssetType`` taxonomy and its integration into
:class:`engine.core.instruments.Instrument`.

Covers the three behaviors required when ``asset_type`` was switched from
a ``STOCK`` default to a ``None`` sentinel:

1. An explicit, contradictory ``asset_type`` (e.g. ``STOCK`` on an
   ``option`` ``asset_class``) is **preserved** (the explicit value wins
   for the public taxonomy) and a **warning** is logged so callers learn
   about the mismatch — it is neither silently rewritten nor rejected.
2. An omitted ``asset_type`` (``None``) still auto-syncs from
   ``asset_class`` so legacy callers keep seeing a coherent value.
3. A contradictory ``asset_type`` on an ``equity`` ``asset_class`` is
   likewise preserved with a warning.

Also exercises the standalone ``AssetType`` coercion helpers — including
``AssetType.from_instrument``'s handling of the ``None`` sentinel — so the
public enum contract stays covered.
"""

from __future__ import annotations

import logging
from datetime import date

import pytest

from engine.core.instruments import (
    Instrument,
    InstrumentAssetClass,
    OptionType,
)
from engine.instruments import AssetType

_MODULE_LOGGER = "engine.core.instruments"


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

    def test_option_factory_syncs_to_option(self):
        inst = Instrument.option("AAPL", 200.0, date(2026, 6, 19), OptionType.CALL)
        assert inst.asset_type == AssetType.OPTION


class TestContradictoryAssetTypePreservedWithWarning:
    """Requirement (1) & (3): explicit contradictions are preserved + warned.

    The validator must NOT silently rewrite an explicit value, and it
    must NOT raise — an explicit choice is authoritative for the public
    taxonomy, so we preserve it and log a warning so the mismatch is
    discoverable.
    """

    def test_explicit_stock_on_option_class_preserved_with_warning(self, caplog):
        # (1) explicit asset_type=STOCK on an OPTION asset_class: the
        # value is preserved as STOCK and a warning is logged.
        with caplog.at_level(logging.WARNING, logger=_MODULE_LOGGER):
            inst = Instrument(
                symbol="AAPL_20260619_C_200.00",
                asset_class=InstrumentAssetClass.OPTION,
                underlying="AAPL",
                strike=200.0,
                expiration=date(2026, 6, 19),
                option_type=OptionType.CALL,
                asset_type=AssetType.STOCK,
            )
        assert inst.asset_type == AssetType.STOCK  # preserved
        assert any("contradicts" in rec.message for rec in caplog.records)
        assert all(rec.levelno == logging.WARNING for rec in caplog.records)

    def test_explicit_option_on_equity_preserved_with_warning(self, caplog):
        # (3) A contradictory explicit asset_type on an equity is
        # preserved (stays OPTION) and a warning is emitted.
        with caplog.at_level(logging.WARNING, logger=_MODULE_LOGGER):
            inst = Instrument(
                symbol="AAPL",
                asset_class=InstrumentAssetClass.EQUITY,
                asset_type=AssetType.OPTION,
            )
        assert inst.asset_type == AssetType.OPTION  # preserved
        assert any("contradicts" in rec.message for rec in caplog.records)

    def test_string_form_of_contradictory_asset_type_preserved_with_warning(self, caplog):
        # The validator should normalize a string-typed asset_type the
        # same way (pydantic coerces it to the enum) and still preserve
        # it + warn about the mismatch.
        with caplog.at_level(logging.WARNING, logger=_MODULE_LOGGER):
            inst = Instrument(
                symbol="AAPL",
                asset_class=InstrumentAssetClass.EQUITY,
                asset_type="crypto",
            )
        assert inst.asset_type == AssetType.CRYPTO  # preserved
        assert any("contradicts" in rec.message for rec in caplog.records)

    def test_explicit_matching_asset_type_is_preserved_silently(self, caplog):
        # Sanity: an explicit value that *agrees* with asset_class is fine
        # and survives construction untouched — and emits NO warning.
        with caplog.at_level(logging.WARNING, logger=_MODULE_LOGGER):
            inst = Instrument(
                symbol="AAPL",
                asset_class=InstrumentAssetClass.EQUITY,
                asset_type=AssetType.STOCK,
            )
        assert inst.asset_type == AssetType.STOCK
        assert not caplog.records  # no warning when they agree

    def test_synced_path_emits_no_warning(self, caplog):
        # The auto-sync path (None → derived) is the normal case and must
        # not produce any warnings.
        with caplog.at_level(logging.WARNING, logger=_MODULE_LOGGER):
            Instrument.equity("AAPL")
            Instrument.crypto("BTC", "USDT")
        assert not caplog.records


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

    def test_round_trip_does_not_emit_warning(self, caplog):
        # Because the synced value agrees with asset_class, rebuilding a
        # dumped instrument must NOT warn about a contradiction.
        for inst in (
            Instrument.equity("AAPL"),
            Instrument.crypto("BTC", "USDT"),
            Instrument.forex("EUR", "USD"),
        ):
            with caplog.at_level(logging.WARNING, logger=_MODULE_LOGGER):
                rebuilt = Instrument.model_validate(inst.model_dump(mode="json"))
            assert rebuilt.asset_type == inst.asset_type
        assert not caplog.records

    def test_model_copy_keeps_synced_asset_type(self):
        # The revalidation-based model_copy must carry the synced value.
        inst = Instrument.equity("AAPL")
        copy = inst.model_copy(update={"symbol": "MSFT"})
        assert copy.asset_type == AssetType.STOCK
        assert copy.symbol == "MSFT"


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

    def test_from_instrument_handles_none_asset_type(self):
        # The None sentinel means "not provided": from_instrument must
        # fall through to the asset_class bridge rather than return None.
        class DtoWithNoneType:
            asset_type = None
            asset_class = "forex"

        assert AssetType.from_instrument(DtoWithNoneType()) == AssetType.FOREX

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

    def test_asset_type_is_serializable_string_value(self):
        # The StrEnum string values are the public contract.
        assert AssetType.STOCK.value == "stock"
        assert str(AssetType.CRYPTO) == "crypto"
