"""Tests for drawdown duration + recovery analytics (gh#97 follow-up)."""

from __future__ import annotations

import pytest

from engine.core.drawdown_analytics import (
    DrawdownEpisode,
    average_drawdown,
    current_drawdown_pct,
    drawdown_episodes,
    max_drawdown_duration,
    time_to_recovery,
    underwater_curve,
)

# ---------------------------------------------------------------------------
# underwater_curve
# ---------------------------------------------------------------------------


class TestUnderwaterCurve:
    def test_empty_input_empty_output(self):
        assert underwater_curve([]) == []

    def test_monotonic_increase_all_zero(self):
        assert underwater_curve([100.0, 110.0, 120.0]) == [0.0, 0.0, 0.0]

    def test_first_bar_is_zero(self):
        # First bar matches its own peak.
        out = underwater_curve([100.0, 90.0, 95.0])
        assert out[0] == 0.0

    def test_drawdown_then_recovery(self):
        # 100 → 80 (-20%) → 100 (back to peak).
        out = underwater_curve([100.0, 80.0, 100.0])
        assert out == [0.0, pytest.approx(-0.2), 0.0]

    def test_new_peak_resets_underwater(self):
        out = underwater_curve([100.0, 80.0, 110.0, 99.0])
        assert out[2] == 0.0
        assert out[3] == pytest.approx(-0.1)

    def test_non_positive_peak_yields_zero(self):
        # All-zero curve — peak <= 0, all bars underwater 0.
        assert underwater_curve([0.0, 0.0, 0.0]) == [0.0, 0.0, 0.0]


# ---------------------------------------------------------------------------
# drawdown_episodes
# ---------------------------------------------------------------------------


class TestDrawdownEpisodes:
    def test_empty_returns_empty(self):
        assert drawdown_episodes([]) == []

    def test_single_point_returns_empty(self):
        assert drawdown_episodes([100.0]) == []

    def test_monotonic_increase_returns_empty(self):
        assert drawdown_episodes([100.0, 110.0, 120.0]) == []

    def test_simple_episode(self):
        # 100 → 80 → 100 → recovery.
        out = drawdown_episodes([100.0, 90.0, 80.0, 90.0, 100.0])
        assert len(out) == 1
        e = out[0]
        assert e.peak_idx == 0
        assert e.trough_idx == 2
        assert e.recovery_idx == 4
        assert e.depth_pct == pytest.approx(0.2)
        assert e.is_open is False

    def test_open_episode_no_recovery(self):
        # 100 → 80 → 90 (still below peak at end).
        out = drawdown_episodes([100.0, 80.0, 90.0])
        assert len(out) == 1
        e = out[0]
        assert e.recovery_idx is None
        assert e.is_open is True
        assert e.trough_idx == 1
        assert e.depth_pct == pytest.approx(0.2)

    def test_two_independent_episodes(self):
        # Peak 100 → trough 80 → recovery 100 → new peak 120 → trough 100 → recovery 120.
        out = drawdown_episodes([100.0, 80.0, 100.0, 120.0, 100.0, 120.0])
        assert len(out) == 2
        assert out[0].peak_idx == 0
        assert out[0].trough_idx == 1
        assert out[0].recovery_idx == 2
        assert out[1].peak_idx == 3
        assert out[1].trough_idx == 4
        assert out[1].recovery_idx == 5

    def test_new_peak_after_recovery_starts_new_episode(self):
        # Recovery to original peak is enough — new high after that
        # still belongs to the next episode.
        out = drawdown_episodes([100.0, 80.0, 100.0, 110.0, 90.0, 110.0])
        assert len(out) == 2

    def test_zero_peak_depth_is_zero(self):
        # Peak == 0 → depth_pct guard returns 0.0.
        out = drawdown_episodes([0.0, -10.0, 0.0])
        assert len(out) == 1
        assert out[0].depth_pct == 0.0

    def test_episode_duration_property(self):
        # Peak idx 0, recovery idx 4 → duration 4.
        out = drawdown_episodes([100.0, 90.0, 80.0, 90.0, 100.0])
        assert out[0].duration == 4

    def test_open_episode_duration_uses_trough(self):
        # Peak 0, trough 1, no recovery → duration 1 (peak → trough).
        out = drawdown_episodes([100.0, 80.0, 90.0])
        assert out[0].duration == 1

    def test_time_to_trough_property(self):
        # Peak 0 → trough 2.
        out = drawdown_episodes([100.0, 90.0, 80.0, 90.0, 100.0])
        assert out[0].time_to_trough == 2


# ---------------------------------------------------------------------------
# max_drawdown_duration
# ---------------------------------------------------------------------------


class TestMaxDrawdownDuration:
    def test_empty_returns_zero(self):
        assert max_drawdown_duration([]) == 0

    def test_monotonic_returns_zero(self):
        assert max_drawdown_duration([100.0, 110.0, 120.0]) == 0

    def test_simple_episode(self):
        # Peak 0 → recovery 4 = duration 4.
        assert max_drawdown_duration([100.0, 90.0, 80.0, 90.0, 100.0]) == 4

    def test_takes_longest_episode(self):
        # Episode A: idx 0→2 (dur 2). Episode B: idx 3→8 (dur 5).
        equity = [100.0, 80.0, 100.0, 120.0, 110.0, 100.0, 90.0, 110.0, 120.0]
        assert max_drawdown_duration(equity) == 5

    def test_open_episode_counted(self):
        # 100 → 80 → 90 (still down at end). Duration = 1 (peak → trough).
        assert max_drawdown_duration([100.0, 80.0, 90.0]) == 1


# ---------------------------------------------------------------------------
# time_to_recovery
# ---------------------------------------------------------------------------


class TestTimeToRecovery:
    def test_empty_returns_zero(self):
        assert time_to_recovery([]) == 0

    def test_monotonic_returns_zero(self):
        assert time_to_recovery([100.0, 110.0, 120.0]) == 0

    def test_recovered_episode_returns_periods(self):
        # Trough at idx 2, recovery at idx 4 → 2 periods.
        assert time_to_recovery([100.0, 90.0, 80.0, 90.0, 100.0]) == 2

    def test_open_deepest_returns_none(self):
        # 100 → 80 → 90 → not recovered → None.
        assert time_to_recovery([100.0, 80.0, 90.0]) is None

    def test_picks_deepest_episode(self):
        # Episode A: depth 10 %, recovers in 1.
        # Episode B: depth 30 %, recovers in 2.
        equity = [100.0, 90.0, 100.0, 120.0, 84.0, 100.0, 120.0]
        # Episode B is deepest. Trough at idx 4 (84). Recovery at idx 6 (120).
        assert time_to_recovery(equity) == 2


# ---------------------------------------------------------------------------
# average_drawdown
# ---------------------------------------------------------------------------


class TestAverageDrawdown:
    def test_empty_returns_zero(self):
        assert average_drawdown([]) == 0.0

    def test_no_drawdown_returns_zero(self):
        assert average_drawdown([100.0, 110.0, 120.0]) == 0.0

    def test_single_episode(self):
        assert average_drawdown([100.0, 80.0, 100.0]) == pytest.approx(0.2)

    def test_two_episodes_averages_depth(self):
        # Episode A: 20% depth. Episode B: 10% depth. Mean = 0.15.
        equity = [100.0, 80.0, 100.0, 120.0, 108.0, 120.0]
        assert average_drawdown(equity) == pytest.approx(0.15)


# ---------------------------------------------------------------------------
# current_drawdown_pct
# ---------------------------------------------------------------------------


class TestCurrentDrawdownPct:
    def test_empty_returns_zero(self):
        assert current_drawdown_pct([]) == 0.0

    def test_at_peak_returns_zero(self):
        assert current_drawdown_pct([100.0, 110.0, 120.0]) == 0.0

    def test_below_peak(self):
        # Peak 120, last bar 96 → 20 % drawdown.
        assert current_drawdown_pct([100.0, 120.0, 96.0]) == pytest.approx(0.2)

    def test_non_positive_peak_returns_zero(self):
        assert current_drawdown_pct([0.0, -5.0, -10.0]) == 0.0

    def test_recovered_above_old_peak(self):
        # New peak. Current matches latest peak → 0.0.
        assert current_drawdown_pct([100.0, 80.0, 110.0, 110.0]) == 0.0


# ---------------------------------------------------------------------------
# DrawdownEpisode invariants
# ---------------------------------------------------------------------------


class TestDrawdownEpisodeInvariants:
    def test_frozen_dataclass(self):
        e = DrawdownEpisode(0, 1, 2, 0.1)
        with pytest.raises(Exception):
            e.peak_idx = 99  # type: ignore[misc]

    def test_is_open_when_no_recovery(self):
        e = DrawdownEpisode(0, 5, None, 0.2)
        assert e.is_open is True
        assert e.duration == 5

    def test_is_closed_when_recovery_set(self):
        e = DrawdownEpisode(0, 5, 10, 0.2)
        assert e.is_open is False
        assert e.duration == 10
