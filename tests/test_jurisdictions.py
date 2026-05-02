"""Unit tests for the tax-jurisdiction engine (gh#81)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from engine.core.tax.jurisdictions import (
    LotMethod,
    TaxJurisdiction,
    UnitedStates,
    get_jurisdiction,
    list_jurisdictions,
    register_jurisdiction,
)
from engine.core.tax.jurisdictions.registry import _reset_for_tests


@pytest.fixture(autouse=True)
def _reset():
    _reset_for_tests()
    yield
    _reset_for_tests()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make(code_value: str, name: str) -> TaxJurisdiction:
    @dataclass(frozen=True)
    class _J:
        code: str = code_value
        display_name: str = name
        currency: str = "EUR"
        long_term_days: int = 365
        wash_sale_window_days: int = 0
        default_lot_method: LotMethod = LotMethod.FIFO
        allowed_lot_methods: frozenset[LotMethod] = field(
            default_factory=lambda: frozenset({LotMethod.FIFO, LotMethod.AVERAGE_COST})
        )

    return _J()


# ---------------------------------------------------------------------------
# UnitedStates record
# ---------------------------------------------------------------------------


class TestUnitedStates:
    def test_protocol_compatible(self):
        us = UnitedStates()
        assert isinstance(us, TaxJurisdiction)

    def test_default_values(self):
        us = UnitedStates()
        assert us.code == "US"
        assert us.currency == "USD"
        assert us.long_term_days == 365
        assert us.wash_sale_window_days == 30
        assert us.default_lot_method == LotMethod.FIFO

    def test_default_lot_method_in_allowed_set(self):
        us = UnitedStates()
        assert us.default_lot_method in us.allowed_lot_methods

    def test_lifo_not_allowed(self):
        # IRS Pub. 550 — LIFO is not a permitted default for non-mutual-fund
        # securities accounts. The model reflects that.
        us = UnitedStates()
        assert LotMethod.LIFO not in us.allowed_lot_methods


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_register_and_lookup(self):
        us = UnitedStates()
        register_jurisdiction(us)
        assert get_jurisdiction("US") is us

    def test_lookup_unknown_raises(self):
        with pytest.raises(KeyError):
            get_jurisdiction("ZZ")

    def test_list_returns_sorted(self):
        register_jurisdiction(UnitedStates())
        register_jurisdiction(_make("GB", "United Kingdom"))
        register_jurisdiction(_make("DE", "Germany"))
        assert list_jurisdictions() == ["DE", "GB", "US"]

    def test_re_register_overwrites(self):
        register_jurisdiction(UnitedStates(long_term_days=999))
        register_jurisdiction(UnitedStates())
        assert get_jurisdiction("US").long_term_days == 365

    def test_register_rejects_non_protocol(self):
        with pytest.raises(TypeError):
            register_jurisdiction("not-a-jurisdiction")  # type: ignore[arg-type]

    def test_register_rejects_empty_code(self):
        bad = _make("", "Empty code")
        with pytest.raises(ValueError):
            register_jurisdiction(bad)


# ---------------------------------------------------------------------------
# Protocol contract
# ---------------------------------------------------------------------------


class TestProtocol:
    def test_custom_implementation_satisfies_protocol(self):
        custom = _make("FR", "France")
        assert isinstance(custom, TaxJurisdiction)

    def test_missing_field_does_not_satisfy_protocol(self):
        @dataclass(frozen=True)
        class Incomplete:
            code: str = "XX"

        assert not isinstance(Incomplete(), TaxJurisdiction)
