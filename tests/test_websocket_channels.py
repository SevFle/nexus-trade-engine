"""Unit tests for engine.api.websocket.channels (SEV-275)."""

from __future__ import annotations

import uuid

import pytest

from engine.api.websocket.channels import (
    for_market,
    for_market_depth,
    for_orders,
    for_portfolio,
    parse,
)


class TestBuilders:
    def test_portfolio_includes_user_id(self):
        u = uuid.uuid4()
        c = for_portfolio(u)
        assert c.family == "portfolio"
        assert c.key == str(u)
        assert c.name == f"portfolio:{u}"
        assert c.is_user_scoped
        assert not c.is_symbol_scoped
        assert c.user_id() == u

    def test_orders_includes_user_id(self):
        u = uuid.uuid4()
        c = for_orders(u)
        assert c.family == "orders"
        assert c.name == f"orders:{u}"
        assert c.user_id() == u

    def test_market_normalises_symbol(self):
        c = for_market(" aapl ")
        assert c.key == "AAPL"
        assert c.name == "market:AAPL"
        assert c.is_symbol_scoped
        assert c.user_id() is None

    def test_market_depth_normalises_symbol(self):
        c = for_market_depth("msft")
        assert c.family == "market_depth"
        assert c.key == "MSFT"

    def test_market_rejects_invalid_symbol(self):
        for bad in ("", "  ", "bad*symbol", "with spaces", "x" * 33):
            with pytest.raises(ValueError):
                for_market(bad)


class TestParser:
    def test_round_trip_portfolio(self):
        c = for_portfolio(uuid.uuid4())
        parsed = parse(c.name)
        assert parsed == c

    def test_round_trip_orders(self):
        c = for_orders(uuid.uuid4())
        parsed = parse(c.name)
        assert parsed == c

    def test_round_trip_market(self):
        c = for_market("AAPL")
        parsed = parse(c.name)
        assert parsed == c

    def test_round_trip_market_depth(self):
        c = for_market_depth("AAPL")
        parsed = parse(c.name)
        assert parsed == c

    def test_unknown_family_returns_none(self):
        assert parse("wizardry:AAPL") is None

    def test_missing_separator_returns_none(self):
        assert parse("portfolio") is None
        assert parse("") is None

    def test_empty_segments_return_none(self):
        assert parse("portfolio:") is None
        assert parse(":AAPL") is None

    def test_parser_does_not_validate_symbol_characters(self):
        # parse() is permissive on the key side because the bridge
        # may receive unprefixed keys from older producers; only the
        # family is validated. The builder enforces symbol syntax.
        parsed = parse("market:weird-sym-OK")
        assert parsed is not None
        assert parsed.family == "market"


class TestChannelEquality:
    def test_channels_hash_and_compare(self):
        u = uuid.uuid4()
        a = for_portfolio(u)
        b = for_portfolio(u)
        assert a == b
        assert hash(a) == hash(b)
        assert {a, b} == {a}

    def test_channels_unequal_across_families(self):
        u = uuid.uuid4()
        assert for_portfolio(u) != for_orders(u)
