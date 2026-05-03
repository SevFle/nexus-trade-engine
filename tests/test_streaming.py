"""Unit tests for streaming primitives (gh#133)."""

from __future__ import annotations

import pytest

from engine.data.streaming import BoundedBuffer, DropPolicy, ReplayLog

# ---------------------------------------------------------------------------
# BoundedBuffer
# ---------------------------------------------------------------------------


class TestBoundedBufferConstructor:
    def test_zero_maxsize_rejected(self):
        with pytest.raises(ValueError):
            BoundedBuffer(maxsize=0)

    def test_negative_maxsize_rejected(self):
        with pytest.raises(ValueError):
            BoundedBuffer(maxsize=-1)


class TestBoundedBufferDropOldest:
    def test_put_below_capacity(self):
        b: BoundedBuffer[int] = BoundedBuffer(maxsize=3)
        assert b.put(1) is True
        assert b.put(2) is True
        assert len(b) == 2
        assert b.dropped_total == 0

    def test_put_at_capacity_drops_oldest(self):
        b: BoundedBuffer[int] = BoundedBuffer(maxsize=3)
        for v in (1, 2, 3):
            b.put(v)
        assert b.put(4) is True
        assert b.snapshot() == [2, 3, 4]
        assert b.dropped_oldest == 1
        assert b.dropped_newest == 0

    def test_get_returns_oldest_first(self):
        b: BoundedBuffer[int] = BoundedBuffer(maxsize=3)
        for v in (1, 2, 3):
            b.put(v)
        assert b.get() == 1
        assert b.get() == 2
        assert b.get() == 3

    def test_get_empty_raises(self):
        b: BoundedBuffer[int] = BoundedBuffer(maxsize=3)
        with pytest.raises(IndexError):
            b.get()

    def test_get_nowait_or_default(self):
        b: BoundedBuffer[int] = BoundedBuffer(maxsize=3)
        assert b.get_nowait_or(-1) == -1

    def test_drain_returns_all_and_empties(self):
        b: BoundedBuffer[int] = BoundedBuffer(maxsize=5)
        for v in (1, 2, 3):
            b.put(v)
        assert b.drain() == [1, 2, 3]
        assert b.is_empty()

    def test_iteration_is_snapshot(self):
        b: BoundedBuffer[int] = BoundedBuffer(maxsize=5)
        for v in (1, 2, 3):
            b.put(v)
        snapshot = list(b)
        b.put(4)  # mutation after snapshot
        assert snapshot == [1, 2, 3]


class TestBoundedBufferDropNewest:
    def test_put_at_capacity_rejects_new(self):
        b: BoundedBuffer[int] = BoundedBuffer(maxsize=3, policy=DropPolicy.DROP_NEWEST)
        for v in (1, 2, 3):
            b.put(v)
        assert b.put(4) is False
        assert b.snapshot() == [1, 2, 3]
        assert b.dropped_oldest == 0
        assert b.dropped_newest == 1


class TestDropCounters:
    def test_reset_zeroes_counters(self):
        b: BoundedBuffer[int] = BoundedBuffer(maxsize=2)
        for v in (1, 2, 3, 4):
            b.put(v)
        assert b.dropped_total == 2
        b.reset_drop_counters()
        assert b.dropped_total == 0


# ---------------------------------------------------------------------------
# ReplayLog
# ---------------------------------------------------------------------------


class TestReplayLogConstructor:
    def test_zero_capacity_rejected(self):
        with pytest.raises(ValueError):
            ReplayLog(capacity=0)


class TestReplayLogRecord:
    def test_seq_is_monotonic(self):
        log: ReplayLog[str] = ReplayLog(capacity=10)
        assert log.record("a") == 0
        assert log.record("b") == 1
        assert log.record("c") == 2
        assert log.next_seq == 3

    def test_capacity_bounds_history(self):
        log: ReplayLog[int] = ReplayLog(capacity=3)
        for v in range(5):
            log.record(v)
        assert len(log) == 3
        assert log.oldest_seq() == 2  # 0,1 evicted

    def test_seq_remains_monotonic_after_eviction(self):
        log: ReplayLog[int] = ReplayLog(capacity=2)
        for v in range(5):
            log.record(v)
        # Even after evictions, next_seq keeps climbing.
        assert log.next_seq == 5


class TestReplayLogSinceSeq:
    def test_returns_only_after(self):
        log: ReplayLog[int] = ReplayLog(capacity=10)
        for v in range(5):
            log.record(v)
        assert log.since_seq(2) == [3, 4]

    def test_after_negative_returns_all(self):
        log: ReplayLog[int] = ReplayLog(capacity=10)
        for v in range(3):
            log.record(v)
        assert log.since_seq(-1) == [0, 1, 2]

    def test_after_too_old_returns_oldest_retained(self):
        log: ReplayLog[int] = ReplayLog(capacity=3)
        for v in range(10):
            log.record(v)
        # Retained seqs: 7,8,9. Asking after seq=2 is older than the
        # window; we still get the retained tail.
        assert log.since_seq(2) == [7, 8, 9]

    def test_after_latest_returns_empty(self):
        log: ReplayLog[int] = ReplayLog(capacity=3)
        for v in range(3):
            log.record(v)
        assert log.since_seq(2) == []


class TestReplayLogLatest:
    def test_latest_n(self):
        log: ReplayLog[int] = ReplayLog(capacity=10)
        for v in range(5):
            log.record(v)
        assert log.latest(2) == [3, 4]

    def test_latest_more_than_size(self):
        log: ReplayLog[int] = ReplayLog(capacity=10)
        for v in range(3):
            log.record(v)
        assert log.latest(99) == [0, 1, 2]

    def test_latest_zero_or_negative_returns_empty(self):
        log: ReplayLog[int] = ReplayLog(capacity=5)
        log.record(1)
        assert log.latest(0) == []
        assert log.latest(-3) == []


class TestReplayLogClear:
    def test_clear_drops_history_keeps_seq(self):
        log: ReplayLog[int] = ReplayLog(capacity=3)
        for v in range(3):
            log.record(v)
        log.clear()
        assert len(log) == 0
        assert log.oldest_seq() is None
        # next_seq does NOT reset.
        assert log.next_seq == 3
