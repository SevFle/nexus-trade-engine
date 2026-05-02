"""API key issuance and verification (gh#94).

Token format
------------
    nxs_<env>_<random>

- ``nxs_`` is a fixed prefix that lets us recognise an engine-issued
  key vs. a JWT or another credential at the auth boundary.
- ``<env>`` is operator-chosen text (e.g. ``live``, ``test``) and is
  baked into the token at issue time. Default: ``live``.
- ``<random>`` is 32 hex characters of cryptographically random data
  (~128 bits of entropy).

The first 12 characters of the issued token are stored in the DB as
``api_keys.prefix`` for human-readable identification (e.g.
``nxs_live_aB3``); the full token is bcrypt-hashed into
``api_keys.key_hash``. The full token is shown to the operator
exactly once, on creation.

Verification
------------
Given an inbound token, we look up the row by ``prefix`` (first 12
chars) and bcrypt-verify the full token against ``key_hash``. If the
row is revoked, expired, or missing, verification fails.

Scope strings
-------------
- ``read``  — GET-only access.
- ``trade`` — submit backtests, place orders.
- ``admin`` — everything; treat as equivalent to the operator role.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import bcrypt
from sqlalchemy import select

from engine.db.models import ApiKey

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


VALID_SCOPES: frozenset[str] = frozenset({"read", "trade", "admin"})

# Length of the random tail in hex characters (~128 bits of entropy).
_RANDOM_TAIL_BYTES = 16

# Number of characters we keep in plaintext for identification.
_PREFIX_DISPLAY_CHARS = 12


class ApiKeyError(ValueError):
    """Raised when an inbound token cannot be parsed or validated."""


def _is_engine_token(token: str) -> bool:
    return token.startswith("nxs_")


def generate_token(env: str = "live") -> str:
    """Issue a fresh API token. Caller must persist the hash before returning."""
    if not env or not env.replace("_", "").isalnum():
        raise ValueError("env must be a non-empty alphanumeric label")
    random_tail = secrets.token_hex(_RANDOM_TAIL_BYTES)
    return f"nxs_{env}_{random_tail}"


def split_token(token: str) -> tuple[str, str]:
    """Split a full token into its display prefix and the bcrypt input.

    The bcrypt input is the *full* token rather than just the random tail —
    that way an attacker who guesses a tail can't trivially reuse it across
    environments.
    """
    if not _is_engine_token(token) or len(token) < _PREFIX_DISPLAY_CHARS + 1:
        raise ApiKeyError("not an engine API key")
    prefix = token[:_PREFIX_DISPLAY_CHARS]
    return prefix, token


def hash_token(token: str) -> str:
    return bcrypt.hashpw(token.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_token(token: str, key_hash: str) -> bool:
    try:
        return bcrypt.checkpw(token.encode("utf-8"), key_hash.encode("utf-8"))
    except (TypeError, ValueError):
        return False


def normalise_scopes(scopes: list[str] | None) -> list[str]:
    if not scopes:
        return ["read"]
    out: list[str] = []
    for s in scopes:
        s_norm = (s or "").strip().lower()
        if s_norm not in VALID_SCOPES:
            raise ValueError(f"unknown scope: {s!r}")
        if s_norm not in out:
            out.append(s_norm)
    return out


async def issue_api_key(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    name: str,
    scopes: list[str] | None = None,
    expires_at: datetime | None = None,
    env: str = "live",
) -> tuple[ApiKey, str]:
    """Create a new API key. Returns (row, plaintext_token).

    The plaintext token is the only opportunity to surface the secret; do
    not log it and do not return it from any subsequent read endpoint.
    """
    if not name.strip():
        raise ValueError("API key name is required")

    token = generate_token(env=env)
    prefix, _ = split_token(token)

    row = ApiKey(
        user_id=user_id,
        name=name.strip(),
        prefix=prefix,
        key_hash=hash_token(token),
        scopes=normalise_scopes(scopes),
        expires_at=expires_at,
    )
    session.add(row)
    await session.flush()
    return row, token


async def find_active_by_token(session: AsyncSession, token: str) -> ApiKey | None:
    """Look up the row that matches ``token``. Returns None if no active row matches."""
    try:
        prefix, _ = split_token(token)
    except ApiKeyError:
        return None

    result = await session.execute(select(ApiKey).where(ApiKey.prefix == prefix))
    row = result.scalar_one_or_none()
    if row is None:
        return None
    if row.revoked_at is not None:
        return None
    if row.expires_at is not None and row.expires_at <= datetime.now(tz=UTC):
        return None
    if not verify_token(token, row.key_hash):
        return None
    return row


async def touch_last_used(session: AsyncSession, row: ApiKey) -> None:
    row.last_used_at = datetime.now(tz=UTC)
    await session.flush()


def is_engine_token(token: str) -> bool:
    """Public helper used by the auth dependency to dispatch on token shape."""
    return _is_engine_token(token)
