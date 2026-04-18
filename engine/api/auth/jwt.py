from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from jose import JWTError, jwt

from engine.config import settings

ALGORITHM = "HS256"


def _get_secret_keys() -> list[str]:
    keys = [settings.secret_key]
    if settings.secret_key_previous:
        keys.append(settings.secret_key_previous)
    return keys


def create_access_token(
    user_id: str,
    email: str,
    role: str,
    provider: str = "local",
) -> str:
    now = datetime.now(tz=UTC)
    expire = now + timedelta(minutes=settings.jwt_access_token_expire_minutes)
    payload: dict[str, Any] = {
        "sub": user_id,
        "email": email,
        "role": role,
        "provider": provider,
        "iat": now,
        "exp": expire,
        "type": "access",
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any] | None:
    for key in _get_secret_keys():
        try:
            payload = jwt.decode(token, key, algorithms=[ALGORITHM])
            if payload.get("type") != "access":
                return None
            return payload
        except JWTError:
            continue
    return None
