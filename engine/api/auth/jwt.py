from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from jwt.exceptions import InvalidTokenError as JWTError

from engine.config import settings

ALGORITHM = "HS256"


def _get_secret_keys() -> list[str]:
    keys = [settings.secret_key]
    if settings.secret_key_previous:
        keys.append(settings.secret_key_previous)
    return keys


def create_access_token(
    sub: str,
    email: str,
    role: str,
    provider: str = "local",
    expires_delta: timedelta | None = None,
) -> str:
    expire = datetime.now(tz=UTC) + (
        expires_delta or timedelta(minutes=settings.jwt_access_token_expire_minutes)
    )
    payload: dict[str, Any] = {
        "sub": sub,
        "email": email,
        "role": role,
        "provider": provider,
        "type": "access",
        "iat": datetime.now(tz=UTC),
        "exp": expire,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_token(token: str) -> dict[str, Any] | None:
    for key in _get_secret_keys():
        try:
            payload = jwt.decode(token, key, algorithms=[ALGORITHM])
            if payload.get("type") != "access":
                return None
            return payload
        except JWTError:
            continue
    return None


def generate_refresh_token() -> str:
    return secrets.token_hex(32)


def hash_token(token: str) -> str:
    # SECURITY NOTE: SHA-256 is intentionally used here instead of bcrypt/argon2.
    # Refresh tokens carry 256 bits of entropy (secrets.token_hex(32)), making
    # brute-force inversion of the hash computationally infeasible even with a
    # fast hash. The primary defense is the token entropy, not the hash cost.
    # Using bcrypt/argon2 would add unnecessary latency on every refresh without
    # meaningful security gain at this entropy level.
    return hashlib.sha256(token.encode()).hexdigest()


def get_refresh_token_expiry() -> datetime:
    return datetime.now(tz=UTC) + timedelta(days=settings.jwt_refresh_token_expire_days)
