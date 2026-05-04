"""Tests for gaps in engine.reference.model — validate_assignment, Classification,
Listing edge cases, defaults, and ticker allowlist additions."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from engine.reference.model import (
    Classification,
    GICSNode,
    InstrumentIds,
    Listing,
    RefInstrument,
    Venue,
)


def _valid_instrument(**overrides) -> dict:
    base = {
        "primary_ticker": "AAPL",
        "primary_venue": "XNAS",
        "asset_class": "equity",
        "name": "Apple Inc.",
    }
    base.update(overrides)
    return base


class TestRefInstrumentValidateAssignment:
    def test_mutation_valid_value_accepted(self):
        inst = RefInstrument(**_valid_instrument())
        inst.name = "New Name"
        assert inst.name == "New Name"

    def test_mutation_invalid_ticker_rejected(self):
        inst = RefInstrument(**_valid_instrument())
        with pytest.raises(ValidationError):
            inst.primary_ticker = " AAPL"

    def test_mutation_whitespace_ticker_rejected(self):
        inst = RefInstrument(**_valid_instrument())
        with pytest.raises(ValidationError):
            inst.primary_ticker = "A AP L"

    def test_mutation_empty_name_rejected(self):
        inst = RefInstrument(**_valid_instrument())
        with pytest.raises(ValidationError):
            inst.name = ""

    def test_mutation_invalid_currency_rejected(self):
        inst = RefInstrument(**_valid_instrument())
        with pytest.raises(ValidationError):
            inst.currency = "us"

    def test_mutation_valid_currency_accepted(self):
        inst = RefInstrument(**_valid_instrument())
        inst.currency = "EUR"
        assert inst.currency == "EUR"

    def test_mutation_invalid_venue_rejected(self):
        inst = RefInstrument(**_valid_instrument())
        with pytest.raises(ValidationError):
            inst.primary_venue = "AB"

    def test_mutation_invalid_asset_class_rejected(self):
        inst = RefInstrument(**_valid_instrument())
        with pytest.raises(ValidationError):
            inst.asset_class = "invalid_class"


class TestRefInstrumentDefaults:
    def test_lot_size_default(self):
        inst = RefInstrument(**_valid_instrument())
        assert inst.lot_size == 1

    def test_tick_size_default(self):
        inst = RefInstrument(**_valid_instrument())
        assert inst.tick_size == Decimal("0.01")

    def test_currency_default_usd(self):
        inst = RefInstrument(**_valid_instrument())
        assert inst.currency == "USD"

    def test_active_default_true(self):
        inst = RefInstrument(**_valid_instrument())
        assert inst.active is True

    def test_metadata_default_empty_dict(self):
        inst = RefInstrument(**_valid_instrument())
        assert inst.metadata == {}

    def test_listings_default_empty_list(self):
        inst = RefInstrument(**_valid_instrument())
        assert inst.listings == []

    def test_ids_default_all_none(self):
        inst = RefInstrument(**_valid_instrument())
        assert inst.ids.isin is None
        assert inst.ids.cusip is None
        assert inst.ids.figi is None
        assert inst.ids.sedol is None
        assert inst.ids.cik is None

    def test_classification_default_all_none(self):
        inst = RefInstrument(**_valid_instrument())
        assert inst.classification.gics_sector is None
        assert inst.classification.naics is None

    def test_unique_id_per_instance(self):
        a = RefInstrument(**_valid_instrument())
        b = RefInstrument(**_valid_instrument())
        assert a.id != b.id


class TestClassification:
    def test_all_fields_accept_valid_string(self):
        c = Classification(
            gics_sector="Technology",
            gics_industry_group="Software",
            gics_industry="Application Software",
            gics_sub_industry="Enterprise Software",
            sic="7372",
            naics="511210",
            crypto_class="token",
            forex_class="major",
        )
        assert c.gics_sector == "Technology"
        assert c.naics == "511210"

    def test_max_length_128_boundary(self):
        Classification(gics_sector="A" * 128)
        with pytest.raises(ValidationError):
            Classification(gics_sector="A" * 129)

    def test_all_fields_default_none(self):
        c = Classification()
        assert c.gics_sector is None
        assert c.gics_industry_group is None
        assert c.gics_industry is None
        assert c.gics_sub_industry is None
        assert c.sic is None
        assert c.naics is None
        assert c.crypto_class is None
        assert c.forex_class is None

    def test_empty_string_accepted(self):
        c = Classification(gics_sector="")
        assert c.gics_sector == ""


class TestListing:
    def test_active_listing(self):
        listing = Listing(
            venue="XNAS", ticker="AAPL", currency="USD", active_from=date(2020, 1, 1)
        )
        assert listing.is_active is True

    def test_inactive_listing_with_past_date(self):
        listing = Listing(
            venue="XNAS",
            ticker="AAPL",
            currency="USD",
            active_from=date(2020, 1, 1),
            active_to=date(2023, 1, 1),
        )
        assert listing.is_active is False

    def test_inactive_listing_with_future_date(self):
        future = date.today() + timedelta(days=365)
        listing = Listing(
            venue="XNAS",
            ticker="AAPL",
            currency="USD",
            active_from=date(2020, 1, 1),
            active_to=future,
        )
        assert listing.is_active is False

    def test_invalid_sedol_wrong_length(self):
        with pytest.raises(ValidationError):
            InstrumentIds(sedol="AB12")

    def test_valid_sedol(self):
        ids = InstrumentIds(sedol="ABC1234")
        assert ids.sedol == "ABC1234"

    def test_invalid_venue_mic(self):
        with pytest.raises(ValidationError):
            Listing(
                venue="ABC",
                ticker="AAPL",
                currency="USD",
                active_from=date(2020, 1, 1),
            )

    def test_invalid_currency(self):
        with pytest.raises(ValidationError):
            Listing(
                venue="XNAS",
                ticker="AAPL",
                currency="US",
                active_from=date(2020, 1, 1),
            )


class TestTickerAllowlist:
    def test_equals_sign_allowed(self):
        inst = RefInstrument(**_valid_instrument(primary_ticker="EURUSD=X"))
        assert inst.primary_ticker == "EURUSD=X"

    def test_plus_sign_allowed(self):
        inst = RefInstrument(**_valid_instrument(primary_ticker="C++"))
        assert inst.primary_ticker == "C++"

    def test_slash_rejected(self):
        with pytest.raises(ValidationError):
            RefInstrument(**_valid_instrument(primary_ticker="AAPL/US"))

    def test_space_rejected(self):
        with pytest.raises(ValidationError):
            RefInstrument(**_valid_instrument(primary_ticker="AAPL US"))

    def test_ticker_max_length_32(self):
        RefInstrument(**_valid_instrument(primary_ticker="A" * 32))
        with pytest.raises(ValidationError):
            RefInstrument(**_valid_instrument(primary_ticker="A" * 33))


class TestGICSNode:
    def test_with_parent_code(self):
        node = GICSNode(code="4510", name="Technology", level="sector", parent_code="45")
        assert node.parent_code == "45"

    def test_without_parent_code(self):
        node = GICSNode(code="45", name="Information Technology", level="sector")
        assert node.parent_code is None

    def test_invalid_level(self):
        with pytest.raises(ValidationError):
            GICSNode(code="45", name="Test", level="invalid")


class TestVenue:
    def test_valid_venue(self):
        v = Venue(mic="XNAS", name="Nasdaq", country="US", timezone="America/New_York")
        assert v.mic == "XNAS"

    def test_country_too_short(self):
        with pytest.raises(ValidationError):
            Venue(mic="XNAS", name="Nasdaq", country="U", timezone="UTC")

    def test_empty_timezone_rejected(self):
        with pytest.raises(ValidationError):
            Venue(mic="XNAS", name="Nasdaq", country="US", timezone="")
