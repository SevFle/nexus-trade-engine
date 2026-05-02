"""Unit tests for the DSR registry constants and helpers — gh#157."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from engine.privacy.deletion import DELETION_GRACE_DAYS, remaining_grace
from engine.privacy.dsr import DSR_KINDS, DSR_TERMINAL_STATUSES, SLA_DEFAULT_DAYS


class TestRegistryConstants:
    def test_documented_kinds(self):
        assert DSR_KINDS == frozenset(
            {"export", "delete", "rectify", "restrict", "object"}
        )

    def test_terminal_statuses(self):
        assert DSR_TERMINAL_STATUSES == frozenset(
            {"completed", "failed", "cancelled"}
        )

    def test_gdpr_one_month_default(self):
        # GDPR Art. 12 obliges responding within one month. Default must
        # be at least 30 days.
        assert SLA_DEFAULT_DAYS == 30


class TestDeletionConstants:
    def test_grace_thirty_days(self):
        assert DELETION_GRACE_DAYS == 30


class TestRemainingGrace:
    def test_in_window_returns_positive(self):
        now = datetime(2026, 5, 3, tzinfo=UTC)
        due = now + timedelta(days=10)
        assert remaining_grace(now, due) == timedelta(days=10)

    def test_past_due_returns_zero(self):
        now = datetime(2026, 5, 3, tzinfo=UTC)
        due = now - timedelta(days=1)
        assert remaining_grace(now, due) == timedelta(0)

    def test_exact_due_returns_zero(self):
        now = datetime(2026, 5, 3, tzinfo=UTC)
        assert remaining_grace(now, now) == timedelta(0)
