"""Unit tests for the GB / HMRC CGT jurisdiction (gh#81 follow-up)."""

from __future__ import annotations

from engine.core.tax.jurisdictions import (
    LotMethod,
    TaxJurisdiction,
    UnitedKingdom,
)


class TestUnitedKingdom:
    def test_protocol_compatible(self):
        assert isinstance(UnitedKingdom(), TaxJurisdiction)

    def test_default_values(self):
        gb = UnitedKingdom()
        assert gb.code == "GB"
        assert gb.display_name == "United Kingdom"
        assert gb.currency == "GBP"

    def test_no_long_term_distinction(self):
        # HMRC CGT has no long-term boundary; we encode this as
        # long_term_days == 0 so consumers can special-case it.
        gb = UnitedKingdom()
        assert gb.long_term_days == 0

    def test_thirty_day_bed_and_breakfasting_window(self):
        # TCGA s.105 — 30 days, symmetric with the US wash-sale window.
        gb = UnitedKingdom()
        assert gb.wash_sale_window_days == 30

    def test_default_lot_method_is_average_cost(self):
        # HMRC requires the s.104 holding pool: average cost of all
        # currently-held shares of the same class.
        gb = UnitedKingdom()
        assert gb.default_lot_method == LotMethod.AVERAGE_COST

    def test_only_average_cost_allowed(self):
        gb = UnitedKingdom()
        assert gb.allowed_lot_methods == frozenset({LotMethod.AVERAGE_COST})
        assert LotMethod.FIFO not in gb.allowed_lot_methods
        assert LotMethod.HIFO not in gb.allowed_lot_methods
        assert LotMethod.LIFO not in gb.allowed_lot_methods

    def test_default_in_allowed(self):
        gb = UnitedKingdom()
        assert gb.default_lot_method in gb.allowed_lot_methods
