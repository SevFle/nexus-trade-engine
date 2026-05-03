"""Data retention policy helpers (gh#90).

Pure-Python policy model + decision helpers. Cron, API, S3 archival
and TimescaleDB compression are deferred follow-ups (this slice ships
only the in-memory policy engine that downstream pieces will call).

Per-dataset defaults follow gh#90's spec literally:

- ``ohlcv_1m``: retain 90 d, compress after 30 d
- ``ohlcv_1d``: keep forever, compress after 365 d
- ``backtest_results``: keep forever
- ``trade_log``: keep forever (tax compliance)
- ``portfolio_snapshots``: retain 365 d, compress after 90 d
- ``webhook_deliveries``: retain 30 d
- ``evaluation_log``: retain 90 d, compress after 30 d

Out of scope:
- Celery beat scheduling / cron registration.
- S3 archival writers.
- TimescaleDB ``compress_chunk`` wiring.
- ``GET/PUT /api/v1/settings/retention`` REST surface.
- Storage-usage reporting per dataset.
- YAML config ingestion.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping


class RetentionAction(StrEnum):
    """What to do with a record under a policy."""

    KEEP = "keep"
    COMPRESS = "compress"
    DELETE = "delete"


@dataclass(frozen=True)
class RetentionPolicy:
    """Retention rules for one dataset.

    ``retain_days = None`` means *keep forever*. ``compress_after_days``
    must not exceed ``retain_days`` (you cannot compress data that has
    already been deleted). ``archive_to = None`` means hard-delete on
    expiry; a non-null path means archive there before delete (the
    archive writer itself is out of scope for this slice).
    """

    dataset: str
    retain_days: int | None
    compress_after_days: int | None = None
    archive_to: str | None = None

    def __post_init__(self) -> None:
        if not self.dataset:
            raise ValueError("dataset name required")
        if self.retain_days is not None and self.retain_days < 0:
            raise ValueError("retain_days must be >= 0 or None")
        if self.compress_after_days is not None and self.compress_after_days < 0:
            raise ValueError("compress_after_days must be >= 0 or None")
        if (
            self.retain_days is not None
            and self.compress_after_days is not None
            and self.compress_after_days > self.retain_days
        ):
            raise ValueError(
                "compress_after_days cannot exceed retain_days "
                "(would compress already-deleted data)"
            )


DEFAULT_POLICIES: Mapping[str, RetentionPolicy] = {
    "ohlcv_1m": RetentionPolicy("ohlcv_1m", retain_days=90, compress_after_days=30),
    "ohlcv_1d": RetentionPolicy("ohlcv_1d", retain_days=None, compress_after_days=365),
    "backtest_results": RetentionPolicy("backtest_results", retain_days=None),
    "trade_log": RetentionPolicy("trade_log", retain_days=None),
    "portfolio_snapshots": RetentionPolicy(
        "portfolio_snapshots", retain_days=365, compress_after_days=90
    ),
    "webhook_deliveries": RetentionPolicy("webhook_deliveries", retain_days=30),
    "evaluation_log": RetentionPolicy("evaluation_log", retain_days=90, compress_after_days=30),
}


def _ensure_aware(dt: datetime, label: str) -> datetime:
    if dt.tzinfo is None:
        raise ValueError(f"{label} must be timezone-aware")
    return dt


def _age_days(record_ts: datetime, *, now: datetime) -> float:
    """Age of a record in fractional days (negative if record is in the future)."""
    _ensure_aware(record_ts, "record_ts")
    _ensure_aware(now, "now")
    delta = now - record_ts
    return delta.total_seconds() / 86_400.0


def is_expired(
    record_ts: datetime, policy: RetentionPolicy, *, now: datetime | None = None
) -> bool:
    """``True`` when the record is older than ``policy.retain_days``.

    ``retain_days = None`` ⇒ never expired. Future-dated records are
    treated as not expired (negative age).
    """
    if policy.retain_days is None:
        return False
    now = now if now is not None else datetime.now(UTC)
    age = _age_days(record_ts, now=now)
    return age > policy.retain_days


def is_compressible(
    record_ts: datetime, policy: RetentionPolicy, *, now: datetime | None = None
) -> bool:
    """``True`` when the record is old enough to compress but not yet expired."""
    if policy.compress_after_days is None:
        return False
    now = now if now is not None else datetime.now(UTC)
    age = _age_days(record_ts, now=now)
    if age <= policy.compress_after_days:
        return False
    return not is_expired(record_ts, policy, now=now)


def decide_action(
    record_ts: datetime, policy: RetentionPolicy, *, now: datetime | None = None
) -> RetentionAction:
    """Resolve ``DELETE`` > ``COMPRESS`` > ``KEEP`` for one record."""
    now = now if now is not None else datetime.now(UTC)
    if is_expired(record_ts, policy, now=now):
        return RetentionAction.DELETE
    if is_compressible(record_ts, policy, now=now):
        return RetentionAction.COMPRESS
    return RetentionAction.KEEP


def partition_by_action(
    records: Iterable[tuple[str, datetime]],
    policy: RetentionPolicy,
    *,
    now: datetime | None = None,
) -> dict[RetentionAction, list[str]]:
    """Bucket ``(record_id, timestamp)`` pairs by required action.

    Always returns all three keys (possibly empty lists). The caller
    supplies record IDs of any string-coercible shape; this helper does
    no I/O.
    """
    now = now if now is not None else datetime.now(UTC)
    buckets: dict[RetentionAction, list[str]] = {
        RetentionAction.KEEP: [],
        RetentionAction.COMPRESS: [],
        RetentionAction.DELETE: [],
    }
    for record_id, ts in records:
        buckets[decide_action(ts, policy, now=now)].append(record_id)
    return buckets


__all__ = [
    "DEFAULT_POLICIES",
    "RetentionAction",
    "RetentionPolicy",
    "decide_action",
    "is_compressible",
    "is_expired",
    "partition_by_action",
]
