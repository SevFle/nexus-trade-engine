"""Tests for MAE/MFE excursion analytics (gh#97 follow-up)."""

from __future__ import annotations

import pytest

from engine.core.excursion_stats import (
    TradeExcursion,
    adverse_efficiency,
    edge_ratio,
    max_mae,
    max_mfe,
    mean_mae,
    mean_mfe,
    trade_efficiency,
)


# ---------------------------------------------------------------------------
# TradeExcursion DTO
# ---------------------------------------------------------------------------


class TestTradeExcursion:
    def test_valid_construction(self):
        t = TradeExcursion(pnl=10.0, mfe=15.0, mae=3.0)
        assert t.pnl == 10.0
        assert t.mfe == 15.0
        assert t.mae == 3.0

    def test_negative_mfe_rejected(self):
        with pytest.raises(ValueError, match="mfe must be"):
            TradeExcursion(pnl=10.0, mfe=-1.0, mae=3.0)

    def test_negative_mae_rejected(self):
        with pytest.raises(ValueError, match="mae must be"):
            TradeExcursion(pnl=10.0, mfe=15.0, mae=-1.0)

    def test_zero_excursions_allowed(self):
        # Trade closed instantly with no path data.
        t = TradeExcursion(pnl=0.0, mfe=0.0, mae=0.0)
        assert t.mfe == 0.0
        assert t.mae == 0.0

    def test_frozen(self):
        t = TradeExcursion(pnl=10.0, mfe=15.0, mae=3.0)
        with pytest.raises(Exception):
            t.pnl = 99.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# mean_mae / max_mae
# ---------------------------------------------------------------------------


class TestMAE:
    def test_mean_empty_returns_zero(self):
        assert mean_mae([]) == 0.0

    def test_max_empty_returns_zero(self):
        assert max_mae([]) == 0.0

    def test_mean_known_value(self):
        trades = [
            TradeExcursion(pnl=10.0, mfe=15.0, mae=2.0),
            TradeExcursion(pnl=-5.0, mfe=3.0, mae=8.0),
            TradeExcursion(pnl=20.0, mfe=25.0, mae=5.0),
        ]
        assert mean_mae(trades) == pytest.approx(5.0)

    def test_max_known_value(self):
        trades = [
            TradeExcursion(pnl=10.0, mfe=15.0, mae=2.0),
            TradeExcursion(pnl=-5.0, mfe=3.0, mae=8.0),
            TradeExcursion(pnl=20.0, mfe=25.0, mae=5.0),
        ]
        assert max_mae(trades) == 8.0


# ---------------------------------------------------------------------------
# mean_mfe / max_mfe
# ---------------------------------------------------------------------------


class TestMFE:
    def test_mean_empty_returns_zero(self):
        assert mean_mfe([]) == 0.0

    def test_max_empty_returns_zero(self):
        assert max_mfe([]) == 0.0

    def test_mean_known_value(self):
        trades = [
            TradeExcursion(pnl=10.0, mfe=15.0, mae=2.0),
            TradeExcursion(pnl=20.0, mfe=25.0, mae=5.0),
        ]
        assert mean_mfe(trades) == pytest.approx(20.0)

    def test_max_known_value(self):
        trades = [
            TradeExcursion(pnl=10.0, mfe=15.0, mae=2.0),
            TradeExcursion(pnl=20.0, mfe=25.0, mae=5.0),
        ]
        assert max_mfe(trades) == 25.0


# ---------------------------------------------------------------------------
# edge_ratio
# ---------------------------------------------------------------------------


class TestEdgeRatio:
    def test_empty_returns_zero(self):
        assert edge_ratio([]) == 0.0

    def test_zero_mae_returns_zero(self):
        # All trades with mae=0 → division would explode → 0.0 short-circuit.
        trades = [
            TradeExcursion(pnl=10.0, mfe=15.0, mae=0.0),
            TradeExcursion(pnl=20.0, mfe=25.0, mae=0.0),
        ]
        assert edge_ratio(trades) == 0.0

    def test_known_value(self):
        # mean MFE = 20, mean MAE = 5 → edge ratio 4.0.
        trades = [
            TradeExcursion(pnl=10.0, mfe=15.0, mae=2.0),
            TradeExcursion(pnl=20.0, mfe=25.0, mae=8.0),
        ]
        assert edge_ratio(trades) == pytest.approx(4.0)

    def test_edge_below_one_means_more_pain_than_gain(self):
        # MFE smaller than MAE on average — edge ratio < 1.
        trades = [
            TradeExcursion(pnl=-10.0, mfe=2.0, mae=15.0),
            TradeExcursion(pnl=-5.0, mfe=3.0, mae=10.0),
        ]
        assert edge_ratio(trades) < 1.0


# ---------------------------------------------------------------------------
# trade_efficiency
# ---------------------------------------------------------------------------


class TestTradeEfficiency:
    def test_empty_returns_zero(self):
        assert trade_efficiency([]) == 0.0

    def test_no_winning_trades_returns_zero(self):
        trades = [
            TradeExcursion(pnl=-10.0, mfe=5.0, mae=12.0),
            TradeExcursion(pnl=-5.0, mfe=2.0, mae=8.0),
        ]
        assert trade_efficiency(trades) == 0.0

    def test_perfect_efficiency_one(self):
        # pnl == mfe → exited at the high.
        trades = [
            TradeExcursion(pnl=10.0, mfe=10.0, mae=0.0),
            TradeExcursion(pnl=20.0, mfe=20.0, mae=2.0),
        ]
        assert trade_efficiency(trades) == pytest.approx(1.0)

    def test_half_efficiency(self):
        # pnl == 0.5 × mfe — gave back half.
        trades = [
            TradeExcursion(pnl=5.0, mfe=10.0, mae=2.0),
            TradeExcursion(pnl=10.0, mfe=20.0, mae=3.0),
        ]
        assert trade_efficiency(trades) == pytest.approx(0.5)

    def test_only_winning_trades_count(self):
        # 1 win at perfect efficiency, 1 loss ignored.
        trades = [
            TradeExcursion(pnl=10.0, mfe=10.0, mae=2.0),
            TradeExcursion(pnl=-5.0, mfe=3.0, mae=8.0),
        ]
        assert trade_efficiency(trades) == pytest.approx(1.0)

    def test_zero_mfe_winners_excluded(self):
        # Winners with mfe=0 are degenerate and excluded.
        trades = [
            TradeExcursion(pnl=5.0, mfe=0.0, mae=1.0),
        ]
        assert trade_efficiency(trades) == 0.0


# ---------------------------------------------------------------------------
# adverse_efficiency
# ---------------------------------------------------------------------------


class TestAdverseEfficiency:
    def test_empty_returns_zero(self):
        assert adverse_efficiency([]) == 0.0

    def test_no_losing_trades_returns_zero(self):
        trades = [TradeExcursion(pnl=10.0, mfe=15.0, mae=2.0)]
        assert adverse_efficiency(trades) == 0.0

    def test_stopped_at_worst_tick_returns_one(self):
        # |pnl| == mae on every loss → adverse_efficiency = 1.0.
        trades = [
            TradeExcursion(pnl=-10.0, mfe=2.0, mae=10.0),
            TradeExcursion(pnl=-5.0, mfe=1.0, mae=5.0),
        ]
        assert adverse_efficiency(trades) == pytest.approx(1.0)

    def test_tight_stop_yields_lower_adverse(self):
        # Loss only 50 % of MAE — exited before worst tick.
        trades = [
            TradeExcursion(pnl=-5.0, mfe=2.0, mae=10.0),
            TradeExcursion(pnl=-5.0, mfe=1.0, mae=10.0),
        ]
        assert adverse_efficiency(trades) == pytest.approx(0.5)

    def test_only_losing_trades_count(self):
        trades = [
            TradeExcursion(pnl=10.0, mfe=15.0, mae=2.0),
            TradeExcursion(pnl=-10.0, mfe=2.0, mae=10.0),
        ]
        assert adverse_efficiency(trades) == pytest.approx(1.0)
