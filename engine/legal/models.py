"""SQLAlchemy model for Legal Gate acceptance tracking.

This is the lean persistence layer for the Legal Gate / Legal surfaces
*acceptance-tracking* vertical slice. It records **who** accepted **which
document version**, **when**, and from **which IP address** — the minimal
audit facts required to prove a user consented to a given legal version.

Scope is deliberately tight:

* No document body / markdown storage (that lives in the broader
  ``legal_documents`` surface backed by
  :class:`engine.db.models.LegalDocument`).
* No revocation modelling — rows are append-only; the "current acceptance"
  for a user is derived as the most recent row (see
  :func:`engine.legal.repository.get_latest_acceptance`).
* ``user_id`` is a free-form string so the slice does not couple to the
  ``users`` table, mirroring the in-memory gate store in
  :mod:`engine.api.legal` which keys on ``str(user.id)``.

The model maps to its own table (``legal_gate_acceptances``) so it never
collides with the document-management ``legal_acceptances`` table already
defined on the shared :class:`~engine.db.models.Base`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from engine.db.models import Base


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


class UTCDateTime(TypeDecorator):
    """``DateTime`` column that always round-trips as timezone-aware UTC.

    SQLite has no native timezone support: even with ``DateTime(timezone=True)``
    a value comes back *naive* after a ``flush``/``refresh`` or ``SELECT``
    (the offset is dropped on store), while Postgres preserves it. That
    asymmetry previously broke callers that compared ``accepted_at`` against
    an aware timestamp (``TypeError: can't compare offset-naive and
    offset-aware datetimes``) and leaked naive values through to the API.

    This wrapper normalises both ends of the round trip so
    ``LegalAcceptance.accepted_at`` is *always* a timezone-aware UTC
    ``datetime`` regardless of backend or read path:

    * **Bind** — naive input is assumed UTC and stamped; aware input is
      converted to UTC.
    * **Result** — naive values returned by SQLite are stamped with UTC;
      aware values are converted to UTC.
    """

    impl = DateTime
    cache_ok = True

    def __init__(self, *args: object, **kwargs: object) -> None:
        # Preserve timezone semantics on backends (Postgres) that honour it.
        kwargs.setdefault("timezone", True)
        super().__init__(*args, **kwargs)

    @staticmethod
    def _to_aware_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_bind_param(
        self, value: datetime | None, dialect: object  # noqa: ARG002
    ) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return self._to_aware_utc(value)
        return value

    def process_result_value(
        self, value: datetime | None, dialect: object  # noqa: ARG002
    ) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return self._to_aware_utc(value)
        return value


class LegalAcceptance(Base):
    """Append-only audit row recording a user's acceptance of a legal version.

    Attributes
    ----------
    id:
        Server-generated primary key (UUID).
    user_id:
        Stable identifier of the accepting user (stored as ``str(user.id)``).
    document_version:
        Version string of the legal document that was accepted.
    accepted_at:
        Timezone-aware UTC timestamp at which the acceptance was recorded.
    ip_address:
        Client IP captured at acceptance time (audit / provenance).
    """

    __tablename__ = "legal_gate_acceptances"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    document_version: Mapped[str] = mapped_column(String(32), nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )
    ip_address: Mapped[str] = mapped_column(String(45), nullable=False)

    __table_args__ = (
        # Most common query is "latest acceptance for this user (optionally at
        # a given version)", so compound indexes cover both access patterns.
        Index("ix_legal_gate_user_version", "user_id", "document_version"),
        Index("ix_legal_gate_user_time", "user_id", "accepted_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return (
            "LegalAcceptance("
            f"id={self.id!r}, user_id={self.user_id!r}, "
            f"document_version={self.document_version!r}, "
            f"accepted_at={self.accepted_at!r}, ip_address={self.ip_address!r})"
        )
