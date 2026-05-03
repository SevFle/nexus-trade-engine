"""Tests for sliding-window cleanup helpers (gh#90 follow-up)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from engine.data.retention import RetentionPolicy
from engine.data.retention_cleanup import (
    DEFAULT_BATCH_SIZE,
    MAX_BATCH_SIZE,
    MIN_BATCH_SIZE,
    CleanupWindow,
    batch_iter,
    compute_cleanup_window,
    estimate_batches,
    partition_boundaries,
)

UTC = timezone.utc
NOW = datetime(2026, 5, 3, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# compute_cleanup_window
# ---------------------------------------------------------------------------


class TestComputeCleanupWindow:
    def test_full_policy_resolves_both_cutoffs(self):
        p = RetentionPolicy("ohlcv_1m", retain_days=90, compress_after_days=30)
        w = compute_cleanup_window(p, now=NOW)
        assert w.dataset == "ohlcv_1m"
        assert w.delete_before == NOW - timedelta(days=90)
        assert w.compress_before == NOW - timedelta(days=30)
        assert w.archive_to is None
        assert w.is_noop is False

    def test_keep_forever_policy_no_delete_cutoff(self):
        p = RetentionPolicy("ohlcv_1d", retain_days=None, compress_after_days=365)
        w = compute_cleanup_window(p, now=NOW)
        assert w.delete_before is None
        assert w.compress_before == NOW - timedelta(days=365)
        assert w.is_noop is False

    def test_no_compression_policy(self):
        p = RetentionPolicy("webhook_deliveries", retain_days=30)
        w = compute_cleanup_window(p, now=NOW)
        assert w.delete_before == NOW - timedelta(days=30)
        assert w.compress_before is None

    def test_keep_forever_no_compression_is_noop(self):
        # backtest_results: never deletes, never compresses.
        p = RetentionPolicy("backtest_results", retain_days=None)
        w = compute_cleanup_window(p, now=NOW)
        assert w.delete_before is None
        assert w.compress_before is None
        assert w.is_noop is True

    def test_archive_to_propagated(self):
        p = RetentionPolicy(
            "ohlcv_1m",
            retain_days=90,
            compress_after_days=30,
            archive_to="s3://bucket/archive/",
        )
        w = compute_cleanup_window(p, now=NOW)
        assert w.archive_to == "s3://bucket/archive/"

    def test_naive_now_rejected(self):
        p = RetentionPolicy("foo", retain_days=30)
        with pytest.raises(ValueError, match="timezone-aware"):
            compute_cleanup_window(p, now=datetime(2026, 1, 1))

    def test_default_now_uses_utc(self):
        p = RetentionPolicy("foo", retain_days=30)
        w = compute_cleanup_window(p)
        assert w.delete_before is not None
        assert w.delete_before.tzinfo is not None


# ---------------------------------------------------------------------------
# partition_boundaries
# ---------------------------------------------------------------------------


class TestPartitionBoundaries:
    def test_noop_window_returns_empty(self):
        w = CleanupWindow(
            "foo", delete_before=None, compress_before=None, archive_to=None
        )
        assert partition_boundaries(w, now=NOW) == []

    def test_simple_horizon_chunked(self):
        # delete_before = 21 days ago; chunk_days=7 → 3 slices.
        w = CleanupWindow(
            "foo",
            delete_before=NOW - timedelta(days=21),
            compress_before=None,
            archive_to=None,
        )
        out = partition_boundaries(w, chunk_days=7, now=NOW)
        assert len(out) == 3
        assert out[0] == (NOW - timedelta(days=21), NOW - timedelta(days=14))
        assert out[1] == (NOW - timedelta(days=14), NOW - timedelta(days=7))
        assert out[2] == (NOW - timedelta(days=7), NOW)

    def test_uses_delete_floor_when_set(self):
        # delete_before is set → it floors the horizon (newer than compress_before).
        w = CleanupWindow(
            "foo",
            delete_before=NOW - timedelta(days=5),
            compress_before=NOW - timedelta(days=14),
            archive_to=None,
        )
        out = partition_boundaries(w, chunk_days=7, now=NOW)
        assert out[0][0] == NOW - timedelta(days=5)

    def test_keep_forever_uses_compress_floor(self):
        # delete_before is None (keep forever), compress is set.
        w = CleanupWindow(
            "ohlcv_1d",
            delete_before=None,
            compress_before=NOW - timedelta(days=14),
            archive_to=None,
        )
        out = partition_boundaries(w, chunk_days=7, now=NOW)
        assert len(out) == 2
        assert out[0][0] == NOW - timedelta(days=14)
        assert out[-1][1] == NOW

    def test_partial_final_chunk_clamped_to_now(self):
        # 10 days, chunk=7 → first 7-day, final 3-day clamped to now.
        w = CleanupWindow(
            "foo",
            delete_before=NOW - timedelta(days=10),
            compress_before=None,
            archive_to=None,
        )
        out = partition_boundaries(w, chunk_days=7, now=NOW)
        assert len(out) == 2
        assert out[0] == (NOW - timedelta(days=10), NOW - timedelta(days=3))
        assert out[1] == (NOW - timedelta(days=3), NOW)

    def test_future_cutoff_returns_empty(self):
        w = CleanupWindow(
            "foo",
            delete_before=NOW + timedelta(days=10),
            compress_before=None,
            archive_to=None,
        )
        assert partition_boundaries(w, now=NOW) == []

    def test_zero_chunk_rejected(self):
        w = CleanupWindow(
            "foo",
            delete_before=NOW - timedelta(days=1),
            compress_before=None,
            archive_to=None,
        )
        with pytest.raises(ValueError, match="chunk_days"):
            partition_boundaries(w, chunk_days=0, now=NOW)

    def test_negative_chunk_rejected(self):
        w = CleanupWindow(
            "foo",
            delete_before=NOW - timedelta(days=1),
            compress_before=None,
            archive_to=None,
        )
        with pytest.raises(ValueError, match="chunk_days"):
            partition_boundaries(w, chunk_days=-1, now=NOW)

    def test_chunks_cover_horizon_contiguously(self):
        w = CleanupWindow(
            "foo",
            delete_before=NOW - timedelta(days=30),
            compress_before=None,
            archive_to=None,
        )
        out = partition_boundaries(w, chunk_days=7, now=NOW)
        # Each chunk's end == next chunk's start (half-open intervals).
        for i in range(len(out) - 1):
            assert out[i][1] == out[i + 1][0]


# ---------------------------------------------------------------------------
# batch_iter
# ---------------------------------------------------------------------------


class TestBatchIter:
    def test_empty_input_no_yields(self):
        assert list(batch_iter([])) == []

    def test_single_full_batch(self):
        items = [f"id_{i}" for i in range(MIN_BATCH_SIZE)]
        out = list(batch_iter(items, batch_size=MIN_BATCH_SIZE))
        assert len(out) == 1
        assert out[0] == items

    def test_multiple_batches(self):
        items = [f"id_{i}" for i in range(250)]
        out = list(batch_iter(items, batch_size=100))
        assert len(out) == 3
        assert out[0] == items[:100]
        assert out[1] == items[100:200]
        assert out[2] == items[200:250]

    def test_partial_final_batch(self):
        items = [f"id_{i}" for i in range(105)]
        out = list(batch_iter(items, batch_size=100))
        assert len(out) == 2
        assert len(out[1]) == 5

    def test_too_small_batch_rejected(self):
        with pytest.raises(ValueError, match="batch_size must be >="):
            list(batch_iter(["a"], batch_size=MIN_BATCH_SIZE - 1))

    def test_too_large_batch_rejected(self):
        with pytest.raises(ValueError, match="batch_size must be <="):
            list(batch_iter(["a"], batch_size=MAX_BATCH_SIZE + 1))

    def test_default_batch_size(self):
        items = [f"id_{i}" for i in range(DEFAULT_BATCH_SIZE + 50)]
        out = list(batch_iter(items))
        assert len(out) == 2
        assert len(out[0]) == DEFAULT_BATCH_SIZE
        assert len(out[1]) == 50


# ---------------------------------------------------------------------------
# estimate_batches
# ---------------------------------------------------------------------------


class TestEstimateBatches:
    def test_zero_items_zero_batches(self):
        assert estimate_batches(0) == 0

    def test_exact_multiple(self):
        assert estimate_batches(20_000, batch_size=10_000) == 2

    def test_partial_final_batch_counted(self):
        assert estimate_batches(20_001, batch_size=10_000) == 3

    def test_smaller_than_batch(self):
        assert estimate_batches(50, batch_size=10_000) == 1

    def test_negative_count_rejected(self):
        with pytest.raises(ValueError, match="item_count"):
            estimate_batches(-1)

    def test_too_small_batch_rejected(self):
        with pytest.raises(ValueError, match="batch_size"):
            estimate_batches(100, batch_size=MIN_BATCH_SIZE - 1)

    def test_too_large_batch_rejected(self):
        with pytest.raises(ValueError, match="batch_size"):
            estimate_batches(100, batch_size=MAX_BATCH_SIZE + 1)

    def test_matches_batch_iter_count(self):
        # Sanity: estimate must match the actual yield count.
        items = [str(i) for i in range(12_345)]
        for bs in (100, 1000, 5000, 10_000):
            assert estimate_batches(len(items), batch_size=bs) == len(
                list(batch_iter(items, batch_size=bs))
            )
