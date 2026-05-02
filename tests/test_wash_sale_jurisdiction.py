"""Tests for the jurisdiction-aware wash-sale detector (gh#156 follow-up).

Covers the new ``window_days`` keyword on :func:`detect_wash_sales` and
the :func:`detect_wash_sales_for_jurisdiction` helper that dispatches
on a :class:`TaxJurisdiction`'s ``wash_sale_window_days``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from engine.core.tax import (
    Trade,
    TradeSide,
    detect_wash_sales,
    detect_wash_sales_for_jurisdiction,
)
from engine.core.tax.jurisdictions import (
    France,
    Germany,
    UnitedKingdom,
    UnitedStates,
)


def _trade(
    tid: str,
    symbol: str,
    side: TradeSide,
    qty: str,
    price: str,
    days_offset: int,
) -> Trade:
    base = datetime(2026, 5, 1, tzinfo=UTC)
    return Trade(
        trade_id=tid,
        symbol=symbol,
        side=side,
        quantity=Decimal(qty),
        price=Decimal(price),
        when=base + timedelta(days=days_offset),
    )


def _loss_with_replacement(replacement_offset: int) -> list[Trade]:
    """Buy@100, sell@80 (loss=200), buy@85 ``replacement_offset`` days later."""
    return [
        _trade("b1", "AAPL", TradeSide.BUY, "10", "100", 0),
        _trade("s1", "AAPL", TradeSide.SELL, "10", "80", 5),
        _trade("b2", "AAPL", TradeSide.BUY, "10", "85", 5 + replacement_offset),
    ]


# ---------------------------------------------------------------------------
# window_days keyword on detect_wash_sales
# ---------------------------------------------------------------------------


class TestWindowDaysKeyword:
    def test_zero_window_short_circuits(self):
        # A jurisdiction with no wash-sale rule should produce no adjustments
        # even when the trade pattern would otherwise be a textbook wash sale.
        trades = _loss_with_replacement(replacement_offset=5)
        assert detect_wash_sales(trades, window_days=0) == []

    def test_negative_window_rejected(self):
        with pytest.raises(ValueError):
            detect_wash_sales([], window_days=-1)

    def test_explicit_window_narrower_than_default(self):
        # Replacement 20 days after sale: matches default 30-day window
        # but not a 14-day window.
        trades = _loss_with_replacement(replacement_offset=20)
        assert len(detect_wash_sales(trades)) == 1
        assert detect_wash_sales(trades, window_days=14) == []

    def test_explicit_window_wider_than_default(self):
        # Replacement 40 days after sale: outside 30-day window, inside 60.
        trades = _loss_with_replacement(replacement_offset=40)
        assert detect_wash_sales(trades) == []
        assert len(detect_wash_sales(trades, window_days=60)) == 1


# ---------------------------------------------------------------------------
# detect_wash_sales_for_jurisdiction
# ---------------------------------------------------------------------------


class TestForJurisdiction:
    def test_us_runs_thirty_day_detector(self):
        trades = _loss_with_replacement(replacement_offset=5)
        adjustments = detect_wash_sales_for_jurisdiction(trades, UnitedStates())
        assert len(adjustments) == 1
        assert adjustments[0].sale_trade_id == "s1"
        assert adjustments[0].disallowed_loss == Decimal("200.0000")

    def test_gb_runs_thirty_day_detector(self):
        # GB bed-and-breakfasting rule: same 30-day window as the US for
        # repurchase-after-loss patterns.
        trades = _loss_with_replacement(replacement_offset=5)
        adjustments = detect_wash_sales_for_jurisdiction(trades, UnitedKingdom())
        assert len(adjustments) == 1

    def test_de_short_circuits(self):
        # Germany: no wash-sale rule. Same trade pattern, no adjustments.
        trades = _loss_with_replacement(replacement_offset=5)
        assert detect_wash_sales_for_jurisdiction(trades, Germany()) == []

    def test_fr_short_circuits(self):
        # France: PFU regime, no wash-sale rule.
        trades = _loss_with_replacement(replacement_offset=5)
        assert detect_wash_sales_for_jurisdiction(trades, France()) == []

    def test_threads_cost_basis_override(self):
        # Explicit basis says the sale was break-even — even US should skip.
        trades = _loss_with_replacement(replacement_offset=5)
        adjustments = detect_wash_sales_for_jurisdiction(
            trades,
            UnitedStates(),
            cost_basis_for={"s1": Decimal("800")},
        )
        assert adjustments == []
