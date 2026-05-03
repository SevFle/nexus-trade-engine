"""Tests for data retention policy helpers (gh#90)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from engine.data.retention import (
    DEFAULT_POLICIES,
    RetentionAction,
    RetentionPolicy,
    decide_action,
    is_compressible,
    is_expired,
    partition_by_action,
)

UTC = timezone.utc
NOW = datetime(2026, 5, 3, 12, 0, tzinfo=UTC)


def _ago(days: float) -> datetime:
    return NOW - timedelta(days=days)


# ---------------------------------------------------------------------------
# RetentionPolicy validation
# ---------------------------------------------------------------------------


class TestRetentionPolicy:
    def test_minimum_valid_policy(self):
        p = RetentionPolicy("foo", retain_days=30)
        assert p.dataset == "foo"
        assert p.retain_days == 30
        assert p.compress_after_days is None
        assert p.archive_to is None

    def test_full_valid_policy(self):
        p = RetentionPolicy(
            "foo",
            retain_days=365,
            compress_after_days=30,
            archive_to="s3://bucket/archive/",
        )
        assert p.compress_after_days == 30
        assert p.archive_to == "s3://bucket/archive/"

    def test_keep_forever_policy(self):
        p = RetentionPolicy("foo", retain_days=None, compress_after_days=365)
        assert p.retain_days is None

    def test_empty_dataset_rejected(self):
        with pytest.raises(ValueError, match="dataset"):
            RetentionPolicy("", retain_days=30)

    def test_negative_retain_days_rejected(self):
        with pytest.raises(ValueError, match="retain_days"):
            RetentionPolicy("foo", retain_days=-1)

    def test_negative_compress_days_rejected(self):
        with pytest.raises(ValueError, match="compress_after_days"):
            RetentionPolicy("foo", retain_days=30, compress_after_days=-1)

    def test_compress_after_retain_rejected(self):
        # Cannot compress data that has been deleted.
        with pytest.raises(ValueError, match="cannot exceed retain_days"):
            RetentionPolicy("foo", retain_days=30, compress_after_days=60)

    def test_compress_equal_retain_allowed(self):
        # Edge case: compress_after == retain — degenerate but legal.
        p = RetentionPolicy("foo", retain_days=30, compress_after_days=30)
        assert p.compress_after_days == 30


# ---------------------------------------------------------------------------
# DEFAULT_POLICIES (pin to gh#90 spec)
# ---------------------------------------------------------------------------


class TestDefaultPolicies:
    def test_ohlcv_1m(self):
        p = DEFAULT_POLICIES["ohlcv_1m"]
        assert p.retain_days == 90
        assert p.compress_after_days == 30

    def test_ohlcv_1d_keeps_forever(self):
        p = DEFAULT_POLICIES["ohlcv_1d"]
        assert p.retain_days is None
        assert p.compress_after_days == 365

    def test_backtest_results_keeps_forever(self):
        p = DEFAULT_POLICIES["backtest_results"]
        assert p.retain_days is None
        assert p.compress_after_days is None

    def test_trade_log_keeps_forever_for_tax(self):
        # Critical: tax compliance — must never auto-delete.
        p = DEFAULT_POLICIES["trade_log"]
        assert p.retain_days is None

    def test_portfolio_snapshots(self):
        p = DEFAULT_POLICIES["portfolio_snapshots"]
        assert p.retain_days == 365
        assert p.compress_after_days == 90

    def test_webhook_deliveries(self):
        p = DEFAULT_POLICIES["webhook_deliveries"]
        assert p.retain_days == 30
        assert p.compress_after_days is None

    def test_evaluation_log(self):
        p = DEFAULT_POLICIES["evaluation_log"]
        assert p.retain_days == 90
        assert p.compress_after_days == 30


# ---------------------------------------------------------------------------
# is_expired
# ---------------------------------------------------------------------------


class TestIsExpired:
    def test_keep_forever_never_expires(self):
        p = RetentionPolicy("foo", retain_days=None)
        assert is_expired(_ago(10_000), p, now=NOW) is False

    def test_within_window_not_expired(self):
        p = RetentionPolicy("foo", retain_days=30)
        assert is_expired(_ago(15), p, now=NOW) is False

    def test_at_boundary_not_expired(self):
        p = RetentionPolicy("foo", retain_days=30)
        # Exactly retain_days old — strictly *older than* expires it.
        assert is_expired(_ago(30), p, now=NOW) is False

    def test_past_boundary_expired(self):
        p = RetentionPolicy("foo", retain_days=30)
        assert is_expired(_ago(31), p, now=NOW) is True

    def test_future_record_not_expired(self):
        p = RetentionPolicy("foo", retain_days=30)
        future = NOW + timedelta(days=10)
        assert is_expired(future, p, now=NOW) is False

    def test_naive_record_ts_rejected(self):
        p = RetentionPolicy("foo", retain_days=30)
        naive = datetime(2026, 1, 1)
        with pytest.raises(ValueError, match="timezone-aware"):
            is_expired(naive, p, now=NOW)

    def test_default_now_uses_utc(self):
        # Smoke: omitting `now` should not raise.
        p = RetentionPolicy("foo", retain_days=30)
        assert is_expired(datetime.now(UTC), p) is False


# ---------------------------------------------------------------------------
# is_compressible
# ---------------------------------------------------------------------------


class TestIsCompressible:
    def test_no_compress_setting_never_compressible(self):
        p = RetentionPolicy("foo", retain_days=30)
        assert is_compressible(_ago(20), p, now=NOW) is False

    def test_too_recent_not_compressible(self):
        p = RetentionPolicy("foo", retain_days=90, compress_after_days=30)
        assert is_compressible(_ago(15), p, now=NOW) is False

    def test_past_compress_threshold_compressible(self):
        p = RetentionPolicy("foo", retain_days=90, compress_after_days=30)
        assert is_compressible(_ago(45), p, now=NOW) is True

    def test_expired_records_not_compressible(self):
        # Once expired, the record is queued for delete, not compress.
        p = RetentionPolicy("foo", retain_days=90, compress_after_days=30)
        assert is_compressible(_ago(120), p, now=NOW) is False

    def test_keep_forever_with_compress_threshold(self):
        # ohlcv_1d shape: never expires, but old data still gets compressed.
        p = RetentionPolicy("foo", retain_days=None, compress_after_days=365)
        assert is_compressible(_ago(400), p, now=NOW) is True


# ---------------------------------------------------------------------------
# decide_action precedence
# ---------------------------------------------------------------------------


class TestDecideAction:
    def test_recent_record_keep(self):
        p = RetentionPolicy("foo", retain_days=90, compress_after_days=30)
        assert decide_action(_ago(5), p, now=NOW) is RetentionAction.KEEP

    def test_older_than_compress_returns_compress(self):
        p = RetentionPolicy("foo", retain_days=90, compress_after_days=30)
        assert decide_action(_ago(45), p, now=NOW) is RetentionAction.COMPRESS

    def test_older_than_retain_returns_delete(self):
        p = RetentionPolicy("foo", retain_days=90, compress_after_days=30)
        assert decide_action(_ago(120), p, now=NOW) is RetentionAction.DELETE

    def test_delete_takes_precedence_over_compress(self):
        # 100 days old: past compress threshold (30) AND past retain (90) → DELETE.
        p = RetentionPolicy("foo", retain_days=90, compress_after_days=30)
        action = decide_action(_ago(100), p, now=NOW)
        assert action is RetentionAction.DELETE

    def test_keep_forever_never_deletes(self):
        p = RetentionPolicy("trade_log", retain_days=None)
        # 10 years old — must still be KEEP.
        assert decide_action(_ago(3650), p, now=NOW) is RetentionAction.KEEP

    def test_keep_forever_with_compress_returns_compress_when_old(self):
        p = RetentionPolicy("ohlcv_1d", retain_days=None, compress_after_days=365)
        assert (
            decide_action(_ago(400), p, now=NOW) is RetentionAction.COMPRESS
        )


# ---------------------------------------------------------------------------
# partition_by_action
# ---------------------------------------------------------------------------


class TestPartitionByAction:
    def test_empty_records_returns_three_empty_buckets(self):
        p = RetentionPolicy("foo", retain_days=30)
        out = partition_by_action([], p, now=NOW)
        assert out == {
            RetentionAction.KEEP: [],
            RetentionAction.COMPRESS: [],
            RetentionAction.DELETE: [],
        }

    def test_mixed_records_partition_correctly(self):
        p = RetentionPolicy("foo", retain_days=90, compress_after_days=30)
        records = [
            ("a", _ago(5)),  # keep
            ("b", _ago(45)),  # compress
            ("c", _ago(60)),  # compress
            ("d", _ago(120)),  # delete
            ("e", _ago(15)),  # keep
        ]
        out = partition_by_action(records, p, now=NOW)
        assert out[RetentionAction.KEEP] == ["a", "e"]
        assert out[RetentionAction.COMPRESS] == ["b", "c"]
        assert out[RetentionAction.DELETE] == ["d"]

    def test_preserves_input_order_within_bucket(self):
        # Important for downstream batch ops that need stable ordering.
        p = RetentionPolicy("foo", retain_days=30)
        records = [
            ("z", _ago(5)),
            ("y", _ago(10)),
            ("x", _ago(2)),
        ]
        out = partition_by_action(records, p, now=NOW)
        assert out[RetentionAction.KEEP] == ["z", "y", "x"]

    def test_keep_forever_bucket_never_deletes(self):
        p = DEFAULT_POLICIES["trade_log"]
        records = [(f"trade_{i}", _ago(i * 365)) for i in range(5)]
        out = partition_by_action(records, p, now=NOW)
        assert len(out[RetentionAction.KEEP]) == 5
        assert out[RetentionAction.DELETE] == []
        assert out[RetentionAction.COMPRESS] == []
