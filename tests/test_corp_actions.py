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
