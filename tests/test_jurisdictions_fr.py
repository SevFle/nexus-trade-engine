"""Unit tests for the FR / PFU jurisdiction (gh#81 follow-up)."""

from __future__ import annotations

from engine.core.tax.jurisdictions import (
    France,
    LotMethod,
    TaxJurisdiction,
)


class TestFrance:
    def test_protocol_compatible(self):
        assert isinstance(France(), TaxJurisdiction)

    def test_default_values(self):
        fr = France()
        assert fr.code == "FR"
        assert fr.display_name == "France"
        assert fr.currency == "EUR"

    def test_no_long_term_distinction(self):
        # PFU regime since 2018 — no long-term boundary by default.
        fr = France()
        assert fr.long_term_days == 0

    def test_no_wash_sale(self):
        fr = France()
        assert fr.wash_sale_window_days == 0

    def test_default_lot_method_is_fifo(self):
        # CGI Article 150-0 D mandates FIFO.
        fr = France()
        assert fr.default_lot_method == LotMethod.FIFO

    def test_only_fifo_allowed(self):
        fr = France()
        assert fr.allowed_lot_methods == frozenset({LotMethod.FIFO})
        assert LotMethod.AVERAGE_COST not in fr.allowed_lot_methods
        assert LotMethod.HIFO not in fr.allowed_lot_methods
        assert LotMethod.LIFO not in fr.allowed_lot_methods

    def test_default_in_allowed(self):
        fr = France()
        assert fr.default_lot_method in fr.allowed_lot_methods
