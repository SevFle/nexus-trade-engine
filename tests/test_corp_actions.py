"""Tests for engine.core.corp_actions — corporate-action back-adjustment."""

from __future__ import annotations

from datetime import date

import pytest

from engine.core.corp_actions import (
    CorporateAction,
    CorporateActionError,
    CorporateActionLog,
    adjust_price,
    adjust_volume,
)


def _split(eff: date, ratio: float) -> CorporateAction:
    return CorporateAction(
        action_type="split",
        symbol="AAPL",
        effective_date=eff,
        ratio=ratio,
    )


def _dividend(eff: date, cash: float) -> CorporateAction:
    return CorporateAction(
        action_type="cash_dividend",
        symbol="AAPL",
        effective_date=eff,
        cash_amount=cash,
    )


class TestSplitAdjustment:
    def test_two_for_one_split_halves_pre_split_price(self):
        log = CorporateActionLog([_split(date(2025, 6, 1), 2.0)])
        adj = adjust_price(
            log,
            symbol="AAPL",
            bar_date=date(2025, 5, 31),
            raw_price=200.0,
            as_of=date(2025, 12, 31),
        )
        assert adj == pytest.approx(100.0)

    def test_post_split_bar_unaffected(self):
        log = CorporateActionLog([_split(date(2025, 6, 1), 2.0)])
        adj = adjust_price(
            log,
            symbol="AAPL",
            bar_date=date(2025, 6, 1),
            raw_price=100.0,
            as_of=date(2025, 12, 31),
        )
        assert adj == pytest.approx(100.0)

    def test_three_for_two_split(self):
        log = CorporateActionLog([_split(date(2025, 6, 1), 1.5)])
        adj = adjust_price(
            log,
            symbol="AAPL",
            bar_date=date(2025, 5, 31),
            raw_price=150.0,
            as_of=date(2025, 12, 31),
        )
        assert adj == pytest.approx(100.0)

    def test_reverse_split_one_for_two(self):
        log = CorporateActionLog([_split(date(2025, 6, 1), 0.5)])
        adj = adjust_price(
            log,
            symbol="AAPL",
            bar_date=date(2025, 5, 31),
            raw_price=10.0,
            as_of=date(2025, 12, 31),
        )
        assert adj == pytest.approx(20.0)

    def test_compound_splits_multiply(self):
        log = CorporateActionLog(
            [
                _split(date(2024, 6, 1), 2.0),
                _split(date(2025, 6, 1), 2.0),
            ]
        )
        adj = adjust_price(
            log,
            symbol="AAPL",
            bar_date=date(2024, 1, 1),
            raw_price=400.0,
            as_of=date(2025, 12, 31),
        )
        assert adj == pytest.approx(100.0)

    def test_zero_ratio_rejected(self):
        with pytest.raises(CorporateActionError):
            CorporateAction(
                action_type="split",
                symbol="AAPL",
                effective_date=date(2025, 1, 1),
                ratio=0.0,
            )

    def test_negative_ratio_rejected(self):
        with pytest.raises(CorporateActionError):
            CorporateAction(
                action_type="split",
                symbol="AAPL",
                effective_date=date(2025, 1, 1),
                ratio=-2.0,
            )


class TestVolumeAdjustment:
    def test_two_for_one_split_doubles_pre_split_volume(self):
        log = CorporateActionLog([_split(date(2025, 6, 1), 2.0)])
        adj = adjust_volume(
            log,
            symbol="AAPL",
            bar_date=date(2025, 5, 31),
            raw_volume=1_000_000,
            as_of=date(2025, 12, 31),
        )
        assert adj == 2_000_000


class TestCashDividend:
    def test_pre_dividend_bar_back_adjusted(self):
        # Multiplicative back-adjustment: factor = (close_pre - cash) / close_pre
        # applied to bars strictly before the ex-date.
        log = CorporateActionLog([_dividend(date(2025, 6, 1), 1.50)])
        adj = adjust_price(
            log,
            symbol="AAPL",
            bar_date=date(2025, 5, 31),
            raw_price=100.0,
            as_of=date(2025, 12, 31),
            ex_date_close=100.0,
        )
        assert adj == pytest.approx(98.50, abs=1e-6)

    def test_dividend_amount_required(self):
        with pytest.raises(CorporateActionError):
            CorporateAction(
                action_type="cash_dividend",
                symbol="AAPL",
                effective_date=date(2025, 6, 1),
            )

    def test_dividend_negative_amount_rejected(self):
        with pytest.raises(CorporateActionError):
            CorporateAction(
                action_type="cash_dividend",
                symbol="AAPL",
                effective_date=date(2025, 6, 1),
                cash_amount=-1.0,
            )


class TestAsOfWindow:
    def test_action_after_as_of_ignored(self):
        log = CorporateActionLog([_split(date(2025, 6, 1), 2.0)])
        adj = adjust_price(
            log,
            symbol="AAPL",
            bar_date=date(2025, 1, 1),
            raw_price=200.0,
            as_of=date(2025, 5, 1),
        )
        assert adj == pytest.approx(200.0)


class TestSymbolFilter:
    def test_other_symbol_unaffected(self):
        log = CorporateActionLog([_split(date(2025, 6, 1), 2.0)])
        adj = adjust_price(
            log,
            symbol="MSFT",
            bar_date=date(2025, 5, 31),
            raw_price=300.0,
            as_of=date(2025, 12, 31),
        )
        assert adj == pytest.approx(300.0)


class TestLogQueries:
    def test_actions_for_symbol_returns_chronological(self):
        a = _split(date(2024, 6, 1), 2.0)
        b = _split(date(2025, 6, 1), 2.0)
        log = CorporateActionLog([b, a])
        out = log.actions_for("AAPL")
        assert [x.effective_date for x in out] == [a.effective_date, b.effective_date]

    def test_actions_for_unknown_symbol_returns_empty(self):
        log = CorporateActionLog([_split(date(2025, 6, 1), 2.0)])
        assert log.actions_for("NONE") == []

    def test_log_supports_append(self):
        log = CorporateActionLog([])
        log.append(_split(date(2025, 6, 1), 2.0))
        assert len(log.actions_for("AAPL")) == 1


class TestActionTypeValidation:
    def test_unknown_action_type_rejected(self):
        with pytest.raises(CorporateActionError):
            CorporateAction(
                action_type="bogus",  # type: ignore[arg-type]
                symbol="AAPL",
                effective_date=date(2025, 6, 1),
                ratio=1.0,
            )

    def test_split_requires_ratio(self):
        with pytest.raises(CorporateActionError):
            CorporateAction(
                action_type="split",
                symbol="AAPL",
                effective_date=date(2025, 6, 1),
            )

    def test_empty_symbol_rejected(self):
        with pytest.raises(CorporateActionError):
            CorporateAction(
                action_type="split",
                symbol="",
                effective_date=date(2025, 6, 1),
                ratio=2.0,
            )


# ---------------------------------------------------------------------------
# PR2 -- spinoffs, stock mergers, cash mergers, symbol changes
# ---------------------------------------------------------------------------


def _spinoff(eff: date, ratio: float, sym: str = "PARENT") -> CorporateAction:
    return CorporateAction(
        action_type="spinoff", symbol=sym, effective_date=eff, ratio=ratio
    )


def _stock_merger(
    eff: date, ratio: float, new_symbol: str = "ACQUIRER", sym: str = "TARGET"
) -> CorporateAction:
    return CorporateAction(
        action_type="merger",
        symbol=sym,
        effective_date=eff,
        ratio=ratio,
        new_symbol=new_symbol,
    )


def _cash_merger(eff: date, cash: float, sym: str = "TARGET") -> CorporateAction:
    return CorporateAction(
        action_type="merger",
        symbol=sym,
        effective_date=eff,
        cash_amount=cash,
    )


def _symbol_change(
    eff: date, new_symbol: str, sym: str = "OLD"
) -> CorporateAction:
    return CorporateAction(
        action_type="symbol_change",
        symbol=sym,
        effective_date=eff,
        new_symbol=new_symbol,
    )


class TestSpinoff:
    def test_pre_spinoff_price_scaled_by_retention_ratio(self):
        log = CorporateActionLog([_spinoff(date(2025, 6, 1), 0.80)])
        adj = adjust_price(
            log,
            symbol="PARENT",
            bar_date=date(2025, 5, 31),
            raw_price=100.0,
            as_of=date(2025, 12, 31),
        )
        assert adj == pytest.approx(80.0)

    def test_post_spinoff_bar_unaffected(self):
        log = CorporateActionLog([_spinoff(date(2025, 6, 1), 0.80)])
        adj = adjust_price(
            log,
            symbol="PARENT",
            bar_date=date(2025, 6, 1),
            raw_price=80.0,
            as_of=date(2025, 12, 31),
        )
        assert adj == pytest.approx(80.0)

    def test_spinoff_does_not_change_volume(self):
        log = CorporateActionLog([_spinoff(date(2025, 6, 1), 0.80)])
        adj = adjust_volume(
            log,
            symbol="PARENT",
            bar_date=date(2025, 5, 31),
            raw_volume=1_000_000,
            as_of=date(2025, 12, 31),
        )
        assert adj == 1_000_000

    def test_spinoff_requires_ratio(self):
        with pytest.raises(CorporateActionError):
            CorporateAction(
                action_type="spinoff",
                symbol="PARENT",
                effective_date=date(2025, 6, 1),
            )

    def test_spinoff_ratio_must_be_in_zero_to_one(self):
        with pytest.raises(CorporateActionError):
            CorporateAction(
                action_type="spinoff",
                symbol="PARENT",
                effective_date=date(2025, 6, 1),
                ratio=1.5,
            )

    def test_spinoff_ratio_zero_rejected(self):
        with pytest.raises(CorporateActionError):
            CorporateAction(
                action_type="spinoff",
                symbol="PARENT",
                effective_date=date(2025, 6, 1),
                ratio=0.0,
            )


class TestStockMerger:
    def test_pre_merger_price_adjusted_like_split(self):
        # ratio = 0.5 -> each old TARGET share becomes 0.5 ACQUIRER share;
        # back-adjustment uses split semantics -> divide pre-merger price.
        log = CorporateActionLog([_stock_merger(date(2025, 6, 1), 0.5)])
        adj = adjust_price(
            log,
            symbol="TARGET",
            bar_date=date(2025, 5, 31),
            raw_price=50.0,
            as_of=date(2025, 12, 31),
        )
        assert adj == pytest.approx(100.0)

    def test_pre_merger_volume_adjusted_like_split(self):
        log = CorporateActionLog([_stock_merger(date(2025, 6, 1), 0.5)])
        adj = adjust_volume(
            log,
            symbol="TARGET",
            bar_date=date(2025, 5, 31),
            raw_volume=2_000_000,
            as_of=date(2025, 12, 31),
        )
        assert adj == 1_000_000

    def test_stock_merger_requires_new_symbol(self):
        with pytest.raises(CorporateActionError):
            CorporateAction(
                action_type="merger",
                symbol="TARGET",
                effective_date=date(2025, 6, 1),
                ratio=0.5,
            )

    def test_stock_merger_requires_positive_ratio(self):
        with pytest.raises(CorporateActionError):
            CorporateAction(
                action_type="merger",
                symbol="TARGET",
                effective_date=date(2025, 6, 1),
                ratio=0.0,
                new_symbol="ACQUIRER",
            )


class TestCashMerger:
    def test_cash_merger_does_not_adjust_pre_merger_price(self):
        log = CorporateActionLog([_cash_merger(date(2025, 6, 1), 75.0)])
        adj = adjust_price(
            log,
            symbol="TARGET",
            bar_date=date(2025, 5, 31),
            raw_price=70.0,
            as_of=date(2025, 12, 31),
        )
        assert adj == pytest.approx(70.0)

    def test_cash_merger_does_not_adjust_volume(self):
        log = CorporateActionLog([_cash_merger(date(2025, 6, 1), 75.0)])
        adj = adjust_volume(
            log,
            symbol="TARGET",
            bar_date=date(2025, 5, 31),
            raw_volume=500_000,
            as_of=date(2025, 12, 31),
        )
        assert adj == 500_000

    def test_merger_must_specify_either_ratio_or_cash(self):
        with pytest.raises(CorporateActionError):
            CorporateAction(
                action_type="merger",
                symbol="TARGET",
                effective_date=date(2025, 6, 1),
            )

    def test_merger_cannot_specify_both_ratio_and_cash(self):
        with pytest.raises(CorporateActionError):
            CorporateAction(
                action_type="merger",
                symbol="TARGET",
                effective_date=date(2025, 6, 1),
                ratio=0.5,
                cash_amount=10.0,
                new_symbol="ACQUIRER",
            )

    def test_merger_negative_cash_rejected(self):
        with pytest.raises(CorporateActionError):
            CorporateAction(
                action_type="merger",
                symbol="TARGET",
                effective_date=date(2025, 6, 1),
                cash_amount=-1.0,
            )


class TestSymbolChange:
    def test_symbol_change_does_not_adjust_price(self):
        log = CorporateActionLog([_symbol_change(date(2025, 6, 1), "NEW")])
        adj = adjust_price(
            log,
            symbol="OLD",
            bar_date=date(2025, 5, 31),
            raw_price=42.0,
            as_of=date(2025, 12, 31),
        )
        assert adj == pytest.approx(42.0)

    def test_symbol_change_does_not_adjust_volume(self):
        log = CorporateActionLog([_symbol_change(date(2025, 6, 1), "NEW")])
        adj = adjust_volume(
            log,
            symbol="OLD",
            bar_date=date(2025, 5, 31),
            raw_volume=12_345,
            as_of=date(2025, 12, 31),
        )
        assert adj == 12_345

    def test_symbol_change_requires_new_symbol(self):
        with pytest.raises(CorporateActionError):
            CorporateAction(
                action_type="symbol_change",
                symbol="OLD",
                effective_date=date(2025, 6, 1),
            )


class TestPR2NumericValidation:
    def test_nan_spinoff_ratio_rejected(self):
        with pytest.raises(CorporateActionError):
            CorporateAction(
                action_type="spinoff",
                symbol="PARENT",
                effective_date=date(2025, 6, 1),
                ratio=float("nan"),
            )

    def test_nan_merger_ratio_rejected(self):
        with pytest.raises(CorporateActionError):
            CorporateAction(
                action_type="merger",
                symbol="TARGET",
                effective_date=date(2025, 6, 1),
                ratio=float("nan"),
                new_symbol="ACQUIRER",
            )

    def test_inf_split_ratio_rejected(self):
        with pytest.raises(CorporateActionError):
            CorporateAction(
                action_type="split",
                symbol="AAPL",
                effective_date=date(2025, 6, 1),
                ratio=float("inf"),
            )

    def test_nan_cash_amount_rejected(self):
        with pytest.raises(CorporateActionError):
            CorporateAction(
                action_type="cash_dividend",
                symbol="AAPL",
                effective_date=date(2025, 6, 1),
                cash_amount=float("nan"),
            )


class TestZeroPriceContract:
    # raw_price=0 is a legitimate input for halted/delisted bars; the
    # adjustment must remain a numerically clean 0.0 rather than NaN.
    def test_zero_price_through_split(self):
        log = CorporateActionLog([_split(date(2025, 6, 1), 2.0)])
        adj = adjust_price(
            log,
            symbol="AAPL",
            bar_date=date(2025, 5, 31),
            raw_price=0.0,
            as_of=date(2025, 12, 31),
        )
        assert adj == 0.0

    def test_zero_price_through_spinoff(self):
        log = CorporateActionLog([_spinoff(date(2025, 6, 1), 0.80)])
        adj = adjust_price(
            log,
            symbol="PARENT",
            bar_date=date(2025, 5, 31),
            raw_price=0.0,
            as_of=date(2025, 12, 31),
        )
        assert adj == 0.0

    def test_zero_price_through_stock_merger(self):
        log = CorporateActionLog([_stock_merger(date(2025, 6, 1), 0.5)])
        adj = adjust_price(
            log,
            symbol="TARGET",
            bar_date=date(2025, 5, 31),
            raw_price=0.0,
            as_of=date(2025, 12, 31),
        )
        assert adj == 0.0


class TestSpinoffPlusDividendComposition:
    def test_dividend_factor_unchanged_by_spinoff_ordering(self):
        # Multiplicative factors compose order-independently: the
        # dividend factor (ex_close - cash) / ex_close is dimensionless,
        # so it does not matter whether ex_close is in pre- or post-
        # spinoff units. Verifies that spinoff + dividend on the same
        # bar produces a stable result.
        log = CorporateActionLog(
            [
                _spinoff(date(2025, 3, 1), 0.80, sym="PARENT"),
                CorporateAction(
                    action_type="cash_dividend",
                    symbol="PARENT",
                    effective_date=date(2025, 9, 1),
                    cash_amount=1.50,
                ),
            ]
        )
        # bar pre-dates both events; ex-date close = 80 (post-spinoff parent).
        adj = adjust_price(
            log,
            symbol="PARENT",
            bar_date=date(2025, 1, 1),
            raw_price=100.0,
            as_of=date(2025, 12, 31),
            ex_date_close=80.0,
        )
        # spinoff factor 0.80 * dividend factor (80 - 1.50)/80 = 0.98125
        # -> total 0.785; 100 * 0.785 = 78.5
        assert adj == pytest.approx(78.5, abs=1e-6)


class TestCompoundPR2:
    def test_split_then_spinoff_compose(self):
        # 2-for-1 split on 2024-06-01, then 80%-retention spinoff
        # on 2025-06-01. A 2024-01-01 bar at $200 should adjust to:
        log = CorporateActionLog(
            [
                CorporateAction(
                    action_type="split",
                    symbol="X",
                    effective_date=date(2024, 6, 1),
                    ratio=2.0,
                ),
                CorporateAction(
                    action_type="spinoff",
                    symbol="X",
                    effective_date=date(2025, 6, 1),
                    ratio=0.80,
                ),
            ]
        )
        adj = adjust_price(
            log,
            symbol="X",
            bar_date=date(2024, 1, 1),
            raw_price=200.0,
            as_of=date(2025, 12, 31),
        )
        assert adj == pytest.approx(80.0)
