"""Unit tests for the DE / KESt jurisdiction (gh#81 follow-up)."""

from __future__ import annotations

from engine.core.tax.jurisdictions import (
    Germany,
    LotMethod,
    TaxJurisdiction,
)


class TestGermany:
    def test_protocol_compatible(self):
        assert isinstance(Germany(), TaxJurisdiction)

    def test_default_values(self):
        de = Germany()
        assert de.code == "DE"
        assert de.display_name == "Germany"
        assert de.currency == "EUR"

    def test_no_long_term_distinction(self):
        # Abgeltungsteuer reform (2009) abolished the holding-period
        # boundary. Encoded as 0.
        de = Germany()
        assert de.long_term_days == 0

    def test_no_wash_sale(self):
        # No equivalent of US Section 1091 for cash-equity sales.
        de = Germany()
        assert de.wash_sale_window_days == 0

    def test_default_lot_method_is_fifo(self):
        # §20 EStG + BMF letter 18.01.2016 mandate FIFO.
        de = Germany()
        assert de.default_lot_method == LotMethod.FIFO

    def test_only_fifo_allowed(self):
        de = Germany()
        assert de.allowed_lot_methods == frozenset({LotMethod.FIFO})
        assert LotMethod.AVERAGE_COST not in de.allowed_lot_methods
        assert LotMethod.HIFO not in de.allowed_lot_methods
        assert LotMethod.LIFO not in de.allowed_lot_methods

    def test_default_in_allowed(self):
        de = Germany()
        assert de.default_lot_method in de.allowed_lot_methods
