"""Data export collector — GDPR Art. 15 / 20 right of access & portability (gh#157).

Builds a single in-memory dict of everything the engine knows about a
user, ready to be JSON-serialised for download.

What this module does *not* do (explicit follow-ups):
- Async generation. Today's ``collect_user_data`` runs in the request.
  For users with thousands of fills, the operator will want this in a
  TaskIQ job with a signed-URL download.
- Tarball / CSV side-by-side packaging. JSON is the canonical export
  format here. The packaging step is a follow-up.
- Field redaction. The export is for the user; nothing is masked.

Sensitive fields that we deliberately *exclude*:
- ``users.password_hash`` — bcrypt hash, no value to the user.
- ``users.mfa_secret_encrypted`` — no value to the user; would defeat
  the purpose of MFA storage if exported.
- ``users.mfa_backup_codes`` — same reasoning.
- ``api_keys.key_hash`` — secret material the engine never re-derives.
- ``webhook_configs.signing_secret`` — same.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from engine.db.models import (
    ApiKey,
    BacktestResult,
    DSRequest,
    LegalAcceptance,
    Portfolio,
    User,
    WebhookConfig,
)

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


_PII_DENYLIST: frozenset[str] = frozenset(
    {
        "password_hash",
        "mfa_secret_encrypted",
        "mfa_backup_codes",
    }
)

_API_KEY_DENYLIST: frozenset[str] = frozenset({"key_hash"})


async def collect_user_data(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> dict[str, Any]:
    """Return a JSON-serialisable dict of everything we know about ``user_id``.

    Raises ``LookupError`` if the user does not exist.
    """
    user = (
        await session.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None:
        raise LookupError(f"user not found: {user_id}")

    portfolios = (
        await session.execute(select(Portfolio).where(Portfolio.user_id == user_id))
    ).scalars().all()
    backtests = (
        await session.execute(
            select(BacktestResult)
            .join(Portfolio, BacktestResult.portfolio_id == Portfolio.id)
            .where(Portfolio.user_id == user_id)
        )
    ).scalars().all()
    webhooks = (
        await session.execute(select(WebhookConfig).where(WebhookConfig.user_id == user_id))
    ).scalars().all()
    api_keys = (
        await session.execute(select(ApiKey).where(ApiKey.user_id == user_id))
    ).scalars().all()
    dsr_rows = (
        await session.execute(select(DSRequest).where(DSRequest.user_id == user_id))
    ).scalars().all()
    legal_rows = (
        await session.execute(
            select(LegalAcceptance).where(LegalAcceptance.user_id == user_id)
        )
    ).scalars().all()

    return {
        "schema_version": 1,
        "exported_at": datetime.now(tz=UTC).isoformat(),
        "user_id": str(user_id),
        "user": _row_to_dict(user, deny=_PII_DENYLIST),
        "portfolios": [_row_to_dict(p) for p in portfolios],
        "backtests": [_row_to_dict(b) for b in backtests],
        "webhooks": [_row_to_dict(w, deny=frozenset({"signing_secret"})) for w in webhooks],
        "api_keys": [_row_to_dict(k, deny=_API_KEY_DENYLIST) for k in api_keys],
        "dsr_history": [_row_to_dict(d) for d in dsr_rows],
        "legal_acceptances": [_row_to_dict(la) for la in legal_rows],
    }


def _row_to_dict(row: Any, *, deny: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Best-effort SQLAlchemy row -> JSON-friendly dict.

    Skips columns in ``deny``. Stringifies non-JSON-native types
    (UUID, datetime, Decimal). The export schema version is bumped if
    this representation changes in a non-backwards-compatible way.
    """
    out: dict[str, Any] = {}
    for column in row.__table__.columns:
        name = column.name
        if name in deny:
            continue
        value = getattr(row, name, None)
        out[name] = _jsonify(value)
    return out


def _jsonify(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_jsonify(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonify(v) for k, v in value.items()}
    return str(value)
