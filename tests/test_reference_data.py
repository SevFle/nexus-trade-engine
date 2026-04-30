"""Tests for engine.reference — symbol master + identifier resolution."""

from __future__ import annotations

from datetime import date

import pytest

from engine.reference import (
    AmbiguousSymbolError,
    Classification,
    GICSNode,
    InstrumentIds,
    Listing,
    RefInstrument,
    Resolver,
    Venue,
)
from engine.reference.classification import (
    crypto_taxonomy,
    forex_pair_class,
    is_valid_gics_path,
)
from engine.reference.search import SearchIndex, Suggestion


def _aapl() -> RefInstrument:
    return RefInstrument(
        primary_ticker="AAPL",
        primary_venue="XNAS",
        asset_class="equity",
        name="Apple Inc.",
        currency="USD",
        ids=InstrumentIds(
            isin="US0378331005",
            cusip="037833100",
            figi="BBG000B9XRY4",
            cik="0000320193",
        ),
        classification=Classification(
            gics_sector="Information Technology",
            gics_industry_group="Technology Hardware & Equipment",
            gics_industry="Technology Hardware, Storage & Peripherals",
            gics_sub_industry="Technology Hardware, Storage & Peripherals",
        ),
        listings=[
            Listing(
                venue="XNAS",
                ticker="AAPL",
                currency="USD",
                active_from=date(1980, 12, 12),
            ),
            Listing(
                venue="XLON",
                ticker="AAPL.L",
                currency="GBP",
                active_from=date(2003, 6, 1),
            ),
        ],
    )


def _aapl_lse() -> RefInstrument:
    return RefInstrument(
        primary_ticker="AAPL",
        primary_venue="XLON",
        asset_class="equity",
        name="Apple Inc. (LSE)",
        currency="GBP",
        ids=InstrumentIds(isin="US0378331005"),
        listings=[
            Listing(
                venue="XLON",
                ticker="AAPL.L",
                currency="GBP",
                active_from=date(2003, 6, 1),
            )
        ],
    )


def _shop_nyse() -> RefInstrument:
    return RefInstrument(
        primary_ticker="SHOP",
        primary_venue="XNYS",
        asset_class="equity",
        name="Shopify Inc.",
        currency="USD",
        ids=InstrumentIds(isin="CA82509L1076"),
    )


def _shop_lse() -> RefInstrument:
    return RefInstrument(
        primary_ticker="SHOP",
        primary_venue="XLON",
        asset_class="equity",
        name="Shoprite Holdings",
        currency="GBP",
        ids=InstrumentIds(isin="ZAE000012084"),
    )


class TestRefInstrumentModel:
    def test_construction_minimal(self):
        inst = RefInstrument(
            primary_ticker="MSFT",
            primary_venue="XNAS",
            asset_class="equity",
            name="Microsoft Corp.",
            currency="USD",
        )
        assert inst.id is not None
        assert inst.active is True
        assert inst.ids.isin is None

    def test_each_constructed_instance_gets_fresh_id(self):
        a = RefInstrument(
            primary_ticker="X",
            primary_venue="XNAS",
            asset_class="equity",
            name="X",
        )
        b = RefInstrument(
            primary_ticker="X",
            primary_venue="XNAS",
            asset_class="equity",
            name="X",
        )
        assert a.id != b.id

    def test_validates_currency_iso_4217_length(self):
        with pytest.raises((ValueError, TypeError)):
            RefInstrument(
                primary_ticker="X",
                primary_venue="XNAS",
                asset_class="equity",
                name="X",
                currency="DOLLAR",
            )


class TestInstrumentIdsValidation:
    def test_isin_must_be_12_chars(self):
        with pytest.raises((ValueError, TypeError)):
            InstrumentIds(isin="US123")

    def test_cusip_must_be_9_chars(self):
        with pytest.raises((ValueError, TypeError)):
            InstrumentIds(cusip="ABC")

    def test_figi_must_be_12_chars(self):
        with pytest.raises((ValueError, TypeError)):
            InstrumentIds(figi="BBGAA")

    def test_all_optional_passes(self):
        ids = InstrumentIds()
        assert ids.isin is None and ids.cusip is None and ids.figi is None


class TestResolverByTicker:
    def test_resolve_unique_ticker(self):
        r = Resolver()
        r.register(_aapl())
        out = r.resolve("AAPL")
        assert out is not None
        assert out.primary_ticker == "AAPL"

    def test_resolve_returns_none_for_unknown(self):
        r = Resolver()
        assert r.resolve("ZZZZZZ") is None

    def test_ambiguous_ticker_raises(self):
        r = Resolver()
        r.register(_shop_nyse())
        r.register(_shop_lse())
        with pytest.raises(AmbiguousSymbolError) as exc:
            r.resolve("SHOP")
        assert len(exc.value.candidates) == 2

    def test_disambiguate_with_venue(self):
        r = Resolver()
        r.register(_shop_nyse())
        r.register(_shop_lse())
        out = r.resolve({"ticker": "SHOP", "venue": "XNYS"})
        assert out is not None
        assert out.primary_venue == "XNYS"

    def test_dot_suffix_routes_to_venue(self):
        r = Resolver()
        r.register(_aapl())
        r.register(_aapl_lse())
        out = r.resolve("AAPL.L")
        assert out is not None
        assert out.primary_venue == "XLON"


class TestResolverByIdentifier:
    def test_resolve_by_isin(self):
        r = Resolver()
        r.register(_aapl())
        out = r.resolve({"isin": "US0378331005"})
        assert out is not None and out.primary_ticker == "AAPL"

    def test_resolve_by_cusip(self):
        r = Resolver()
        r.register(_aapl())
        out = r.resolve({"cusip": "037833100"})
        assert out is not None and out.primary_ticker == "AAPL"

    def test_resolve_by_figi(self):
        r = Resolver()
        r.register(_aapl())
        out = r.resolve({"figi": "BBG000B9XRY4"})
        assert out is not None and out.primary_ticker == "AAPL"

    def test_resolve_by_cik(self):
        r = Resolver()
        r.register(_aapl())
        out = r.resolve({"cik": "0000320193"})
        assert out is not None and out.primary_ticker == "AAPL"


class TestResolverFuzz:
    @pytest.mark.parametrize(
        "raw",
        [
            "",
            " ",
            "/",
            "AAPL/",
            ".",
            "..L",
            "VERY-LONG-" + "X" * 200,
            "🚀",
            "<script>",
        ],
    )
    def test_garbage_returns_none_or_raises_value_error(self, raw):
        r = Resolver()
        r.register(_aapl())
        try:
            out = r.resolve(raw)
            assert out is None or out.primary_ticker == "AAPL"
        except ValueError:
            pass


class TestGICSValidation:
    def test_valid_gics_path(self):
        assert is_valid_gics_path(
            "Information Technology",
            "Software & Services",
            "Software",
            "Application Software",
        )

    def test_invalid_sector_rejected(self):
        assert not is_valid_gics_path(
            "Made Up Sector",
            "Software & Services",
            "Software",
            "Application Software",
        )

    def test_partial_path_invalid(self):
        assert not is_valid_gics_path(
            "Health Care",
            "Software & Services",
            "Software",
            "Application Software",
        )


class TestCryptoTaxonomy:
    def test_known_l1(self):
        assert crypto_taxonomy("BTC") == "l1"

    def test_known_stablecoin(self):
        assert crypto_taxonomy("USDC") == "stablecoin"

    def test_unknown_falls_back(self):
        assert crypto_taxonomy("XXXXX") == "unknown"


class TestForexClassification:
    def test_major(self):
        assert forex_pair_class("EUR", "USD") == "major"

    def test_minor(self):
        assert forex_pair_class("EUR", "GBP") == "minor"

    def test_exotic(self):
        assert forex_pair_class("USD", "TRY") == "exotic"


class TestSearchIndex:
    def test_substring_match(self):
        idx = SearchIndex()
        idx.add(_aapl())
        idx.add(_shop_nyse())
        results = idx.search("apple")
        tickers = [r.primary_ticker for r in results]
        assert "AAPL" in tickers

    def test_ticker_prefix_match(self):
        idx = SearchIndex()
        idx.add(_aapl())
        idx.add(_shop_nyse())
        results = idx.search("SHO")
        tickers = [r.primary_ticker for r in results]
        assert "SHOP" in tickers

    def test_asset_class_filter(self):
        idx = SearchIndex()
        idx.add(_aapl())
        results = idx.search("apple", asset_class="crypto")
        assert results == []

    def test_returns_top_n_only(self):
        idx = SearchIndex()
        for i in range(50):
            idx.add(
                RefInstrument(
                    primary_ticker=f"TST{i}",
                    primary_venue="XNAS",
                    asset_class="equity",
                    name=f"Test Co {i}",
                )
            )
        results = idx.search("Test", limit=10)
        assert len(results) == 10


class TestSymbolAndNameSearch:
    """Both ticker and company-name queries are first-class entry points."""

    def _idx(self) -> SearchIndex:
        idx = SearchIndex()
        idx.add(_aapl())
        idx.add(
            RefInstrument(
                primary_ticker="MSFT",
                primary_venue="XNAS",
                asset_class="equity",
                name="Microsoft Corp.",
            )
        )
        idx.add(
            RefInstrument(
                primary_ticker="BRK.B",
                primary_venue="XNYS",
                asset_class="equity",
                name="Berkshire Hathaway Inc.",
            )
        )
        idx.add(
            RefInstrument(
                primary_ticker="WDFC",
                primary_venue="XNAS",
                asset_class="equity",
                name="WD-40 Company",
            )
        )
        return idx

    def test_name_full_word_finds_ticker(self):
        results = self._idx().search("Apple")
        assert results
        assert results[0].primary_ticker == "AAPL"

    def test_name_prefix_finds_ticker(self):
        results = self._idx().search("Micro")
        assert results
        assert results[0].primary_ticker == "MSFT"

    def test_name_word_token_finds_ticker(self):
        # Multi-word names: "Berk" matches the first word of
        # "Berkshire Hathaway Inc." and ranks BRK.B above non-matches.
        results = self._idx().search("Berk")
        assert results
        assert results[0].primary_ticker == "BRK.B"

    def test_ticker_exact_still_wins_over_name_partial(self):
        # If query matches one record's ticker exactly AND another
        # record's name as a substring, the ticker-exact match comes
        # first.
        idx = SearchIndex()
        idx.add(
            RefInstrument(
                primary_ticker="ABC",
                primary_venue="XNAS",
                asset_class="equity",
                name="Some Company",
            )
        )
        idx.add(
            RefInstrument(
                primary_ticker="ZZZ",
                primary_venue="XNAS",
                asset_class="equity",
                name="ABC Industries",
            )
        )
        results = idx.search("ABC")
        assert results[0].primary_ticker == "ABC"

    def test_name_search_is_case_insensitive(self):
        results = self._idx().search("MICROSOFT")
        assert results
        assert results[0].primary_ticker == "MSFT"

    def test_name_internal_substring_still_matches(self):
        results = self._idx().search("soft")
        tickers = [r.primary_ticker for r in results]
        assert "MSFT" in tickers

    def test_no_match_returns_empty(self):
        results = self._idx().search("xyznotapresent")
        assert results == []


class TestGICSNode:
    def test_construction(self):
        node = GICSNode(code="45", name="Information Technology", level="sector")
        assert node.code == "45"


class TestVenue:
    def test_mic_format(self):
        v = Venue(mic="XNAS", name="Nasdaq", country="US", timezone="America/New_York")
        assert v.mic == "XNAS"

    def test_invalid_mic_rejected(self):
        with pytest.raises((ValueError, TypeError)):
            Venue(mic="NAS", name="Nasdaq", country="US", timezone="America/New_York")


class TestRegisterIdempotence:
    def test_double_register_is_noop(self):
        r = Resolver()
        inst = _aapl()
        r.register(inst)
        r.register(inst)
        # If non-idempotent, _by_ticker would have duplicates and the
        # ambiguity branch could trip; this just verifies resolve still works.
        out = r.resolve("AAPL")
        assert out is not None and out.id == inst.id


class TestDottedSuffixNoFallthrough:
    def test_known_suffix_unknown_venue_returns_none(self):
        r = Resolver()
        # Apple registered with XNAS only — no XLON listing.
        inst = RefInstrument(
            primary_ticker="AAPL",
            primary_venue="XNAS",
            asset_class="equity",
            name="Apple",
        )
        r.register(inst)
        # `.L` maps to XLON; no listing exists. Must NOT silently route
        # to the XNAS-primary record.
        assert r.resolve("AAPL.L") is None


class TestTickerAllowlist:
    def test_injection_payload_in_ticker_rejected(self):
        with pytest.raises((ValueError, TypeError)):
            RefInstrument(
                primary_ticker="<script>alert(1)</script>",
                primary_venue="XNAS",
                asset_class="equity",
                name="x",
            )

    def test_path_traversal_in_listing_ticker_rejected(self):
        with pytest.raises((ValueError, TypeError)):
            Listing(
                venue="XNAS",
                ticker="../../etc/passwd",
                currency="USD",
                active_from=date(2020, 1, 1),
            )

    def test_legitimate_separators_accepted(self):
        # Real-world ticker formats: BRK.B (NYSE share class),
        # BTC-USD (crypto), ES_F (futures), AAPL.L (LSE), AAPL:US.
        for tk in ("BRK.B", "BTC-USD", "ES_F", "AAPL.L", "AAPL:US"):
            inst = RefInstrument(
                primary_ticker=tk,
                primary_venue="XNAS",
                asset_class="equity",
                name="x",
            )
            assert inst.primary_ticker == tk


class TestUnicodeGarbageGuard:
    def test_bidi_override_in_query_returns_none(self):
        r = Resolver()
        r.register(_aapl())
        rlo = chr(0x202E)  # RIGHT-TO-LEFT OVERRIDE
        assert r.resolve(f"AAPL{rlo}") is None

    def test_zero_width_space_in_query_returns_none(self):
        r = Resolver()
        r.register(_aapl())
        zwsp = chr(0x200B)  # ZERO-WIDTH SPACE
        assert r.resolve(f"A{zwsp}APL") is None

    def test_dict_path_ticker_garbage_filtered(self):
        r = Resolver()
        r.register(_aapl())
        assert r.resolve({"ticker": "<script>"}) is None


class TestSearchQueryLengthCap:
    def test_oversize_query_returns_empty(self):
        idx = SearchIndex()
        idx.add(_aapl())
        results = idx.search("a" * 1000)
        assert results == []


class TestOpenFIGIRepr:
    def test_repr_masks_api_key(self):
        from engine.reference.ingestion.openfigi import OpenFIGIAdapter

        adapter = OpenFIGIAdapter(api_key="sk-live-very-secret")
        rendered = repr(adapter)
        assert "sk-live-very-secret" not in rendered
        assert "***" in rendered

    def test_repr_with_no_key(self):
        from engine.reference.ingestion.openfigi import OpenFIGIAdapter

        adapter = OpenFIGIAdapter()
        rendered = repr(adapter)
        assert "None" in rendered


class TestIngestionResultImmutability:
    def test_errors_is_tuple(self):
        from engine.reference.ingestion import IngestionResult

        r = IngestionResult(adapter="x", fetched=0, new=0, updated=0)
        assert isinstance(r.errors, tuple)


class TestSymbolChange:
    def test_old_ticker_still_resolves_via_listing_history(self):
        r = Resolver()
        meta = RefInstrument(
            primary_ticker="META",
            primary_venue="XNAS",
            asset_class="equity",
            name="Meta Platforms, Inc.",
            listings=[
                Listing(
                    venue="XNAS",
                    ticker="FB",
                    currency="USD",
                    active_from=date(2012, 5, 18),
                    active_to=date(2022, 6, 8),
                ),
                Listing(
                    venue="XNAS",
                    ticker="META",
                    currency="USD",
                    active_from=date(2022, 6, 9),
                ),
            ],
        )
        r.register(meta)
        old = r.resolve({"ticker": "FB", "venue": "XNAS"})
        new = r.resolve({"ticker": "META", "venue": "XNAS"})
        assert old is not None and new is not None
        assert old.id == new.id


class TestTypeaheadSuggest:
    """`suggest()` is the typeahead-optimized variant of search.

    It prefers prefix completions (ticker-prefix, then word-token-prefix
    in the company name) over substring matches, accepts very short
    queries (a single character is enough to start narrowing), and
    falls back to typo-tolerant fuzzy matching only when no prefix or
    substring match is found.
    """

    def _idx(self) -> SearchIndex:
        idx = SearchIndex()
        idx.add(_aapl())
        idx.add(
            RefInstrument(
                primary_ticker="MSFT",
                primary_venue="XNAS",
                asset_class="equity",
                name="Microsoft Corp.",
            )
        )
        idx.add(
            RefInstrument(
                primary_ticker="GOOG",
                primary_venue="XNAS",
                asset_class="equity",
                name="Alphabet Inc.",
            )
        )
        idx.add(
            RefInstrument(
                primary_ticker="BRK.B",
                primary_venue="XNYS",
                asset_class="equity",
                name="Berkshire Hathaway Inc.",
            )
        )
        idx.add(
            RefInstrument(
                primary_ticker="META",
                primary_venue="XNAS",
                asset_class="equity",
                name="Meta Platforms Inc.",
            )
        )
        return idx

    def test_single_char_ticker_prefix_suggests(self):
        out = self._idx().suggest("M")
        tickers = [s.record.primary_ticker for s in out]
        assert "MSFT" in tickers
        assert "META" in tickers

    def test_single_char_name_prefix_suggests(self):
        out = self._idx().suggest("A")
        tickers = [s.record.primary_ticker for s in out]
        # AAPL ticker prefix and Alphabet name prefix should both surface.
        assert "AAPL" in tickers
        assert "GOOG" in tickers

    def test_two_char_name_prefix_narrows(self):
        out = self._idx().suggest("Mi")
        assert out
        assert out[0].record.primary_ticker == "MSFT"

    def test_returns_suggestion_objects_with_completion(self):
        out = self._idx().suggest("Micro")
        assert out
        assert isinstance(out[0], Suggestion)
        # The completion is a presentational hint of what the user is
        # filling in: the matched word from name or ticker.
        assert out[0].completion.lower().startswith("micro")

    def test_typo_tolerant_one_edit_distance(self):
        # "Aple" → "Apple" (one insertion). Pure substring search would
        # miss this; suggest() falls back to Levenshtein ≤ 1 on tokens.
        out = self._idx().suggest("Aple")
        tickers = [s.record.primary_ticker for s in out]
        assert "AAPL" in tickers

    def test_typo_tolerant_on_company_name(self):
        out = self._idx().suggest("Berksire")
        tickers = [s.record.primary_ticker for s in out]
        assert "BRK.B" in tickers

    def test_default_limit_is_typeahead_friendly(self):
        # Typeahead UI shows ~10 results max; default should reflect that.
        idx = SearchIndex()
        # 30 records all matching "A" prefix on either ticker or name.
        for i in range(30):
            idx.add(
                RefInstrument(
                    primary_ticker=f"A{i:02d}",
                    primary_venue="XNAS",
                    asset_class="equity",
                    name=f"Acme{i:02d} Corp.",
                )
            )
        out = idx.suggest("A")
        assert len(out) <= 10

    def test_explicit_limit_respected(self):
        out = self._idx().suggest("A", limit=2)
        assert len(out) <= 2

    def test_empty_query_returns_empty(self):
        assert self._idx().suggest("") == []
        assert self._idx().suggest("   ") == []

    def test_no_match_returns_empty(self):
        out = self._idx().suggest("xyznotapresent")
        assert out == []

    def test_prefix_ranks_above_substring(self):
        idx = SearchIndex()
        idx.add(
            RefInstrument(
                primary_ticker="ZZZ",
                primary_venue="XNAS",
                asset_class="equity",
                name="ZZZ has microsoft inside",
            )
        )
        idx.add(
            RefInstrument(
                primary_ticker="MSFT",
                primary_venue="XNAS",
                asset_class="equity",
                name="Microsoft Corp.",
            )
        )
        out = idx.suggest("Micr")
        assert out
        assert out[0].record.primary_ticker == "MSFT"

    def test_asset_class_filter(self):
        idx = self._idx()
        idx.add(
            RefInstrument(
                primary_ticker="ETH",
                primary_venue="XCRY",
                asset_class="crypto",
                name="Ethereum",
            )
        )
        out = idx.suggest("E", asset_class="crypto")
        assert all(s.record.asset_class == "crypto" for s in out)

    def test_typo_only_kicks_in_when_no_exact_or_prefix_hit(self):
        # If any prefix-tier result exists, fuzzy candidates should not
        # dilute the top of the list — fuzzy is a fallback.
        idx = self._idx()
        out = idx.suggest("App")  # prefix hits Apple
        assert out
        assert out[0].record.primary_ticker == "AAPL"
