"""Tests for cross-portfolio aggregation helpers (gh#89)."""

from __future__ import annotations

import math

import pytest

from engine.core.portfolio_aggregator import (
    PortfolioView,
    aggregate_exposure,
    combined_equity_curve,
    combined_nav,
    correlation_matrix,
    filter_by_tags,
    position_overlap,
)


def _pv(
    pid: str,
    nav: float = 0.0,
    *,
    tags: frozenset[str] | None = None,
    positions: dict[str, float] | None = None,
) -> PortfolioView:
    return PortfolioView(
        portfolio_id=pid,
        nav=nav,
        tags=tags or frozenset(),
        positions=positions or {},
    )


# ---------------------------------------------------------------------------
# combined_nav
# ---------------------------------------------------------------------------


class TestCombinedNav:
    def test_empty_input_zero(self):
        assert combined_nav([]) == 0.0

    def test_single_portfolio(self):
        assert combined_nav([_pv("a", 100.0)]) == 100.0

    def test_sums_across_portfolios(self):
        portfolios = [_pv("a", 100.0), _pv("b", 250.0), _pv("c", 50.0)]
        assert combined_nav(portfolios) == 400.0

    def test_negative_nav_subtracts(self):
        # Margin account in the red — combined NAV must reflect.
        portfolios = [_pv("a", 200.0), _pv("b", -50.0)]
        assert combined_nav(portfolios) == 150.0


# ---------------------------------------------------------------------------
# combined_equity_curve
# ---------------------------------------------------------------------------


class TestCombinedEquityCurve:
    def test_empty_input_empty_output(self):
        assert combined_equity_curve([]) == []

    def test_single_curve_returned_as_list(self):
        out = combined_equity_curve([[100.0, 110.0, 105.0]])
        assert out == [100.0, 110.0, 105.0]

    def test_two_curves_element_wise_sum(self):
        out = combined_equity_curve([[100.0, 110.0], [50.0, 55.0]])
        assert out == [150.0, 165.0]

    def test_three_curves_element_wise_sum(self):
        curves = [[1.0, 2.0, 3.0], [10.0, 20.0, 30.0], [100.0, 200.0, 300.0]]
        out = combined_equity_curve(curves)
        assert out == [111.0, 222.0, 333.0]

    def test_unequal_length_rejected(self):
        with pytest.raises(ValueError, match="equal length"):
            combined_equity_curve([[1.0, 2.0], [1.0, 2.0, 3.0]])

    def test_all_empty_curves(self):
        out = combined_equity_curve([[], []])
        assert out == []


# ---------------------------------------------------------------------------
# correlation_matrix
# ---------------------------------------------------------------------------


class TestCorrelationMatrix:
    def test_empty_input(self):
        assert correlation_matrix({}) == {}

    def test_single_series_diagonal_one(self):
        out = correlation_matrix({"a": [0.01, 0.02, -0.01]})
        assert out == {"a": {"a": 1.0}}

    def test_perfectly_correlated(self):
        out = correlation_matrix({"a": [1.0, 2.0, 3.0], "b": [2.0, 4.0, 6.0]})
        assert out["a"]["b"] == pytest.approx(1.0)
        assert out["b"]["a"] == pytest.approx(1.0)
        assert out["a"]["a"] == 1.0
        assert out["b"]["b"] == 1.0

    def test_perfectly_anti_correlated(self):
        out = correlation_matrix({"a": [1.0, 2.0, 3.0], "b": [3.0, 2.0, 1.0]})
        assert out["a"]["b"] == pytest.approx(-1.0)

    def test_uncorrelated(self):
        # Symmetric series — Pearson should be 0.
        out = correlation_matrix(
            {"a": [1.0, -1.0, 1.0, -1.0], "b": [1.0, 1.0, -1.0, -1.0]}
        )
        assert out["a"]["b"] == pytest.approx(0.0, abs=1e-9)

    def test_constant_series_zero_correlation(self):
        # No variance → undefined → 0.0 by convention.
        out = correlation_matrix({"a": [1.0, 1.0, 1.0], "b": [1.0, 2.0, 3.0]})
        assert out["a"]["b"] == 0.0
        # Diagonal of constant series is 0 (no variation = undefined).
        assert out["a"]["a"] == 0.0
        assert out["b"]["b"] == 1.0

    def test_unequal_length_rejected(self):
        with pytest.raises(ValueError, match="equal length"):
            correlation_matrix({"a": [1.0, 2.0], "b": [1.0, 2.0, 3.0]})

    def test_matrix_is_symmetric(self):
        out = correlation_matrix(
            {
                "a": [1.0, 2.0, 3.0, 4.0],
                "b": [4.0, 3.0, 2.0, 1.0],
                "c": [1.0, 4.0, 2.0, 3.0],
            }
        )
        for a in out:
            for b in out[a]:
                assert math.isclose(out[a][b], out[b][a])


# ---------------------------------------------------------------------------
# position_overlap
# ---------------------------------------------------------------------------


class TestPositionOverlap:
    def test_no_overlap_empty_result(self):
        portfolios = [
            _pv("a", positions={"AAPL": 10.0}),
            _pv("b", positions={"GOOG": 5.0}),
        ]
        assert position_overlap(portfolios) == {}

    def test_overlap_two_portfolios(self):
        portfolios = [
            _pv("a", positions={"AAPL": 10.0, "MSFT": 5.0}),
            _pv("b", positions={"AAPL": 20.0}),
        ]
        out = position_overlap(portfolios)
        assert out == {"AAPL": {"a": 10.0, "b": 20.0}}

    def test_overlap_three_portfolios(self):
        portfolios = [
            _pv("a", positions={"AAPL": 10.0}),
            _pv("b", positions={"AAPL": 20.0}),
            _pv("c", positions={"AAPL": 30.0, "TSLA": 5.0}),
        ]
        out = position_overlap(portfolios)
        assert out == {"AAPL": {"a": 10.0, "b": 20.0, "c": 30.0}}

    def test_single_holder_excluded(self):
        # TSLA only in one portfolio — must not appear in overlap.
        portfolios = [
            _pv("a", positions={"AAPL": 10.0, "TSLA": 5.0}),
            _pv("b", positions={"AAPL": 20.0}),
        ]
        out = position_overlap(portfolios)
        assert "TSLA" not in out
        assert "AAPL" in out

    def test_empty_portfolios(self):
        assert position_overlap([]) == {}


# ---------------------------------------------------------------------------
# aggregate_exposure
# ---------------------------------------------------------------------------


class TestAggregateExposure:
    def test_rolls_up_by_bucket(self):
        portfolios = [
            _pv("a", positions={"AAPL": 100.0, "GOOG": 50.0, "BTC": 1.0}),
            _pv("b", positions={"AAPL": 200.0, "BTC": 2.0}),
        ]
        classifier = {"AAPL": "equity", "GOOG": "equity", "BTC": "crypto"}
        out = aggregate_exposure(portfolios, classifier)
        assert out == {"equity": 350.0, "crypto": 3.0}

    def test_unknown_symbol_goes_to_default(self):
        portfolios = [_pv("a", positions={"FOO": 10.0, "AAPL": 5.0})]
        classifier = {"AAPL": "equity"}
        out = aggregate_exposure(portfolios, classifier)
        assert out == {"equity": 5.0, "unknown": 10.0}

    def test_custom_default_bucket(self):
        portfolios = [_pv("a", positions={"FOO": 10.0})]
        out = aggregate_exposure(portfolios, {}, default_bucket="other")
        assert out == {"other": 10.0}

    def test_empty_portfolios(self):
        assert aggregate_exposure([], {"AAPL": "equity"}) == {}


# ---------------------------------------------------------------------------
# filter_by_tags
# ---------------------------------------------------------------------------


class TestFilterByTags:
    @pytest.fixture
    def portfolios(self) -> list[PortfolioView]:
        return [
            _pv("a", tags=frozenset({"paper", "crypto-only"})),
            _pv("b", tags=frozenset({"live", "aggressive"})),
            _pv("c", tags=frozenset({"paper", "aggressive"})),
            _pv("d", tags=frozenset()),
        ]

    def test_empty_tags_returns_all(self, portfolios):
        out = filter_by_tags(portfolios, [])
        assert [p.portfolio_id for p in out] == ["a", "b", "c", "d"]

    def test_match_any_default(self, portfolios):
        out = filter_by_tags(portfolios, ["paper"])
        assert {p.portfolio_id for p in out} == {"a", "c"}

    def test_match_any_multiple(self, portfolios):
        out = filter_by_tags(portfolios, ["paper", "live"])
        assert {p.portfolio_id for p in out} == {"a", "b", "c"}

    def test_match_all(self, portfolios):
        out = filter_by_tags(portfolios, ["paper", "aggressive"], match="all")
        assert {p.portfolio_id for p in out} == {"c"}

    def test_match_all_no_match(self, portfolios):
        out = filter_by_tags(portfolios, ["paper", "live"], match="all")
        assert out == []

    def test_match_none(self, portfolios):
        out = filter_by_tags(portfolios, ["paper"], match="none")
        assert {p.portfolio_id for p in out} == {"b", "d"}

    def test_invalid_match_mode_rejected(self, portfolios):
        with pytest.raises(ValueError, match="match must be"):
            filter_by_tags(portfolios, ["paper"], match="weird")
