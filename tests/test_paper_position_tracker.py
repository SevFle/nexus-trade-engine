"""Tests for paper trade position tracker with P&L calculation."""

from __future__ import annotations

import pytest

from engine.core.execution.position_tracker import PaperPositionTracker


class TestBasicPositionTracking:
    def test_open_long_position(self):
        tracker = PaperPositionTracker(initial_cash=100_000.0)
        pos = tracker.open_or_update("AAPL", 100, 150.0)
        assert pos.quantity == 100
        assert pos.avg_entry_price == 150.0
        assert pos.is_long

    def test_open_short_position(self):
        tracker = PaperPositionTracker(initial_cash=100_000.0)
        pos = tracker.open_or_update("AAPL", -100, 150.0)
        assert pos.quantity == -100
        assert pos.is_short

    def test_cash_deducted_on_buy(self):
        tracker = PaperPositionTracker(initial_cash=100_000.0)
        tracker.open_or_update("AAPL", 100, 150.0)
        assert tracker.cash == 85_000.0

    def test_cash_increased_on_sell(self):
        tracker = PaperPositionTracker(initial_cash=100_000.0)
        tracker.open_or_update("AAPL", 100, 150.0)
        tracker.open_or_update("AAPL", -100, 160.0)
        assert tracker.cash == 101_000.0

    def test_get_position(self):
        tracker = PaperPositionTracker(initial_cash=100_000.0)
        tracker.open_or_update("AAPL", 100, 150.0)
        pos = tracker.get_position("AAPL")
        assert pos is not None
        assert pos.quantity == 100

    def test_get_missing_position(self):
        tracker = PaperPositionTracker(initial_cash=100_000.0)
        assert tracker.get_position("AAPL") is None

    def test_get_positions_excludes_zero(self):
        tracker = PaperPositionTracker(initial_cash=100_000.0)
        tracker.open_or_update("AAPL", 100, 150.0)
        tracker.open_or_update("AAPL", -100, 160.0)
        positions = tracker.get_positions()
        assert "AAPL" not in positions


class TestPnLCalculation:
    def test_unrealized_pnl_long(self):
        tracker = PaperPositionTracker(initial_cash=100_000.0)
        tracker.open_or_update("AAPL", 100, 150.0)
        tracker.update_price("AAPL", 160.0)
        assert tracker.total_unrealized_pnl == 1000.0

    def test_unrealized_pnl_short(self):
        tracker = PaperPositionTracker(initial_cash=100_000.0)
        tracker.open_or_update("AAPL", -100, 150.0)
        tracker.update_price("AAPL", 140.0)
        assert tracker.total_unrealized_pnl == 1000.0

    def test_realized_pnl_on_close(self):
        tracker = PaperPositionTracker(initial_cash=100_000.0)
        tracker.open_or_update("AAPL", 100, 150.0)
        pos = tracker.open_or_update("AAPL", -100, 160.0)
        assert pos.realized_pnl == 1000.0
        assert tracker.total_realized_pnl == 1000.0

    def test_realized_pnl_loss(self):
        tracker = PaperPositionTracker(initial_cash=100_000.0)
        tracker.open_or_update("AAPL", 100, 150.0)
        pos = tracker.open_or_update("AAPL", -100, 140.0)
        assert pos.realized_pnl == -1000.0

    def test_total_pnl(self):
        tracker = PaperPositionTracker(initial_cash=100_000.0)
        tracker.open_or_update("AAPL", 100, 150.0)
        tracker.update_price("AAPL", 160.0)
        assert tracker.total_pnl == 1000.0

    def test_total_equity(self):
        tracker = PaperPositionTracker(initial_cash=100_000.0)
        tracker.open_or_update("AAPL", 100, 150.0)
        tracker.update_price("AAPL", 160.0)
        assert tracker.total_equity == 101_000.0


class TestPartialClose:
    def test_partial_close_realizes_proportional_pnl(self):
        tracker = PaperPositionTracker(initial_cash=100_000.0)
        tracker.open_or_update("AAPL", 100, 150.0)
        pos = tracker.open_or_update("AAPL", -50, 160.0)
        assert pos.quantity == 50
        assert pos.realized_pnl == 500.0
        assert pos.avg_entry_price == 150.0

    def test_close_then_reverse(self):
        tracker = PaperPositionTracker(initial_cash=100_000.0)
        tracker.open_or_update("AAPL", 100, 150.0)
        tracker.open_or_update("AAPL", -100, 160.0)
        assert tracker.total_realized_pnl == 1000.0
        tracker.open_or_update("AAPL", -50, 170.0)
        pos = tracker.get_position("AAPL")
        assert pos is not None
        assert pos.quantity == -50
        assert pos.avg_entry_price == 170.0


class TestPyramiding:
    def test_add_to_existing_long(self):
        tracker = PaperPositionTracker(initial_cash=100_000.0)
        tracker.open_or_update("AAPL", 100, 150.0)
        pos = tracker.open_or_update("AAPL", 50, 160.0)
        assert pos.quantity == 150
        avg = (100 * 150.0 + 50 * 160.0) / 150
        assert abs(pos.avg_entry_price - avg) < 0.01

    def test_add_to_existing_short(self):
        tracker = PaperPositionTracker(initial_cash=100_000.0)
        tracker.open_or_update("AAPL", -100, 150.0)
        pos = tracker.open_or_update("AAPL", -50, 140.0)
        assert pos.quantity == -150


class TestMultiAsset:
    def test_multiple_positions(self):
        tracker = PaperPositionTracker(initial_cash=200_000.0)
        tracker.open_or_update("AAPL", 100, 150.0)
        tracker.open_or_update("MSFT", 50, 300.0)
        positions = tracker.get_positions()
        assert len(positions) == 2
        assert "AAPL" in positions
        assert "MSFT" in positions

    def test_portfolio_pnl_across_assets(self):
        tracker = PaperPositionTracker(initial_cash=200_000.0)
        tracker.open_or_update("AAPL", 100, 150.0)
        tracker.open_or_update("MSFT", 50, 300.0)
        tracker.update_prices({"AAPL": 160.0, "MSFT": 310.0})
        assert tracker.total_unrealized_pnl == 1500.0


class TestCommission:
    def test_commission_deducted_from_cash(self):
        tracker = PaperPositionTracker(initial_cash=100_000.0)
        tracker.open_or_update("AAPL", 100, 150.0, commission=5.0)
        assert tracker.cash == 100_000.0 - 15_000.0 - 5.0


class TestDrawdown:
    def test_max_drawdown_tracking(self):
        tracker = PaperPositionTracker(initial_cash=100_000.0)
        tracker.open_or_update("AAPL", 100, 150.0)
        tracker.update_price("AAPL", 160.0)
        assert tracker.max_drawdown == 0.0
        tracker.update_price("AAPL", 140.0)
        assert tracker.max_drawdown > 0.0


class TestWinRate:
    def test_win_rate_after_trades(self):
        tracker = PaperPositionTracker(initial_cash=200_000.0)
        tracker.open_or_update("AAPL", 100, 150.0)
        tracker.open_or_update("AAPL", -100, 160.0)
        assert tracker.win_rate == 1.0
        tracker.open_or_update("MSFT", 50, 300.0)
        tracker.open_or_update("MSFT", -50, 290.0)
        assert tracker.win_rate == 0.5


class TestSnapshot:
    def test_snapshot_includes_all_fields(self):
        tracker = PaperPositionTracker(initial_cash=100_000.0)
        tracker.open_or_update("AAPL", 100, 150.0)
        snapshot = tracker.get_snapshot()
        assert snapshot.cash == 85_000.0
        assert snapshot.total_equity == 100_000.0
        assert "AAPL" in snapshot.positions
        assert snapshot.win_count == 0
        assert snapshot.loss_count == 0

    def test_get_position_quantity(self):
        tracker = PaperPositionTracker(initial_cash=100_000.0)
        tracker.open_or_update("AAPL", 100, 150.0)
        assert tracker.get_position_quantity("AAPL") == 100
        assert tracker.get_position_quantity("MSFT") == 0


class TestUpdatePrices:
    def test_update_prices_batch(self):
        tracker = PaperPositionTracker(initial_cash=200_000.0)
        tracker.open_or_update("AAPL", 100, 150.0)
        tracker.open_or_update("MSFT", 50, 300.0)
        tracker.update_prices({"AAPL": 160.0, "MSFT": 310.0})
        aapl_pos = tracker.get_position("AAPL")
        msft_pos = tracker.get_position("MSFT")
        assert aapl_pos.current_price == 160.0
        assert msft_pos.current_price == 310.0

    def test_close_position_method(self):
        tracker = PaperPositionTracker(initial_cash=100_000.0)
        tracker.open_or_update("AAPL", 100, 150.0)
        pos = tracker.close_position("AAPL", 50, 160.0)
        assert pos.quantity == 50

    def test_close_nonexistent_raises(self):
        tracker = PaperPositionTracker(initial_cash=100_000.0)
        with pytest.raises(ValueError, match="No position"):
            tracker.close_position("AAPL", 100, 160.0)
