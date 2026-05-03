"""Sliding-window cleanup helpers for the retention engine (gh#90).

Builds on the per-record decision helpers in ``engine.data.retention``.
This module computes the *boundaries* that a cron-driven cleanup task
will hand to bulk SQL ``DELETE`` / TimescaleDB ``compress_chunk`` calls.

A cleanup pass for one policy resolves to two cutoff timestamps:

- ``delete_before`` — every record older than this should be deleted
  (or archived first if ``policy.archive_to`` is set).
- ``compress_before`` — every record older than this but newer than
  ``delete_before`` should be compressed.

The actual SQL execution lives in the deferred cron worker; this module
returns plain DTOs so the cron implementation stays mockable and the
math stays trivially testable.

Out of scope:
- Celery beat / cron registration.
- Bulk DELETE / compress_chunk SQL.
- S3 archival writers.
- Idempotency keys / lock-free retry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from engine.data.retention import RetentionPolicy

DEFAULT_BATCH_SIZE = 10_000
MAX_BATCH_SIZE = 100_000
MIN_BATCH_SIZE = 100


@dataclass(frozen=True)
class CleanupWindow:
    """Resolved cutoff timestamps for one cleanup pass.

    ``delete_before`` is ``None`` when the policy keeps records forever.
    ``compress_before`` is ``None`` when the policy has no compression
    threshold. Both being ``None`` means the cleanup pass is a no-op
    (e.g. ``backtest_results``).
    """

    dataset: str
    delete_before: datetime | None
    compress_before: datetime | None
    archive_to: str | None

    @property
    def is_noop(self) -> bool:
        return self.delete_before is None and self.compress_before is None


def compute_cleanup_window(
    policy: RetentionPolicy, *, now: datetime | None = None
) -> CleanupWindow:
    """Resolve cutoff timestamps for one policy.

    Returns ``CleanupWindow`` with ``delete_before`` /
    ``compress_before`` set to ``now - retain_days`` /
    ``now - compress_after_days`` respectively, or ``None`` when the
    corresponding setting is unset on the policy.
    """
    now = now if now is not None else datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    delete_before = (
        now - timedelta(days=policy.retain_days) if policy.retain_days is not None else None
    )
    compress_before = (
        now - timedelta(days=policy.compress_after_days)
        if policy.compress_after_days is not None
        else None
    )
    return CleanupWindow(
        dataset=policy.dataset,
        delete_before=delete_before,
        compress_before=compress_before,
        archive_to=policy.archive_to,
    )


def partition_boundaries(
    window: CleanupWindow,
    *,
    chunk_days: int = 7,
    now: datetime | None = None,
) -> list[tuple[datetime, datetime]]:
    """Slice the cleanup horizon into fixed-width windows.

    Returns a list of ``(start, end)`` half-open intervals walking from
    the oldest cutoff up to ``now``. ``chunk_days`` controls width.
    The result is empty for a no-op window or a non-positive horizon.

    The cron worker calls one bulk SQL query per slice so a single
    pass cannot lock the table for an unbounded duration.
    """
    if chunk_days <= 0:
        raise ValueError("chunk_days must be > 0")
    now = now if now is not None else datetime.now(UTC)
    if window.is_noop:
        return []
    earliest = window.delete_before or window.compress_before
    if earliest is None or earliest >= now:
        return []
    chunk = timedelta(days=chunk_days)
    out: list[tuple[datetime, datetime]] = []
    cursor = earliest
    while cursor < now:
        end = min(cursor + chunk, now)
        out.append((cursor, end))
        cursor = end
    return out


def batch_iter(items: list[str], *, batch_size: int = DEFAULT_BATCH_SIZE) -> Iterator[list[str]]:
    """Yield successive ``batch_size`` slices of ``items``.

    Used by the cron worker to chunk record-id lists for bulk DELETE
    calls. Validates ``batch_size`` against operator-friendly bounds.
    """
    if batch_size < MIN_BATCH_SIZE:
        raise ValueError(f"batch_size must be >= {MIN_BATCH_SIZE}, got {batch_size}")
    if batch_size > MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be <= {MAX_BATCH_SIZE}, got {batch_size}")
    for i in range(0, len(items), batch_size):
        yield items[i : i + batch_size]


def estimate_batches(item_count: int, *, batch_size: int = DEFAULT_BATCH_SIZE) -> int:
    """Number of batches ``batch_iter`` will yield for ``item_count`` items."""
    if item_count < 0:
        raise ValueError("item_count must be >= 0")
    if batch_size < MIN_BATCH_SIZE:
        raise ValueError(f"batch_size must be >= {MIN_BATCH_SIZE}")
    if batch_size > MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be <= {MAX_BATCH_SIZE}")
    if item_count == 0:
        return 0
    return (item_count + batch_size - 1) // batch_size


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "MAX_BATCH_SIZE",
    "MIN_BATCH_SIZE",
    "CleanupWindow",
    "batch_iter",
    "compute_cleanup_window",
    "estimate_batches",
    "partition_boundaries",
]
