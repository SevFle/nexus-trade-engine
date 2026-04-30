"""Session lifecycle management.

Models a user session as a typed entity with idle and absolute timeouts,
concurrent-session caps with oldest-first eviction, and explicit revoke.
The store layer is a Protocol so the in-memory backend (suitable for
tests + single-pod) and a Valkey-backed backend (multi-pod, follow-up)
share the same surface.

Privacy:
- ``ip`` and ``user_agent`` are hashed (HMAC-SHA-256 with a runtime
  salt) before persistence so an exfiltrated session row cannot be
  correlated back to a client without the salt.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid  # noqa: TC003 - referenced as uuid.UUID at runtime via dataclass
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterable


def _now() -> datetime:
    return datetime.now(UTC)


def hash_ip(ip: str, *, salt: str) -> str:
    """HMAC-SHA-256 of an IP under a server-side salt."""
    return hmac.new(salt.encode("utf-8"), ip.encode("utf-8"), hashlib.sha256).hexdigest()


def hash_user_agent(ua: str, *, salt: str) -> str:
    """HMAC-SHA-256 of a User-Agent string under a server-side salt."""
    return hmac.new(salt.encode("utf-8"), ua.encode("utf-8"), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class Session:
    """One session entity. Frozen — the store replaces records on update."""

    id: str
    user_id: uuid.UUID
    device_label: str
    ip_hash: str
    ua_hash: str
    created_at: datetime
    last_active_at: datetime
    revoked: bool = False


@dataclass(frozen=True)
class SessionConfig:
    """Idle / absolute timeouts plus concurrent-session cap."""

    idle_timeout_sec: int = 900  # 15 min
    absolute_timeout_sec: int = 86_400  # 24 h
    max_concurrent: int = 5
    privacy_salt: str = field(default_factory=lambda: secrets.token_hex(16))


class SessionExpiredError(Exception):
    """Raised when a session has exceeded its idle or absolute timeout."""


class SessionRevokedError(Exception):
    """Raised when an operation targets a revoked session."""


class SessionStore(Protocol):
    """Pluggable persistence layer for sessions."""

    async def get(self, session_id: str) -> Session | None: ...
    async def save(self, session: Session) -> None: ...
    async def list_for_user(self, user_id: uuid.UUID) -> list[Session]: ...


class InMemorySessionStore:
    """Process-local store. Not for multi-pod — use a Valkey backend."""

    def __init__(self) -> None:
        self._by_id: dict[str, Session] = {}

    async def get(self, session_id: str) -> Session | None:
        return self._by_id.get(session_id)

    async def save(self, session: Session) -> None:
        self._by_id[session.id] = session

    async def list_for_user(self, user_id: uuid.UUID) -> list[Session]:
        return [s for s in self._by_id.values() if s.user_id == user_id]


class SessionService:
    """Lifecycle operations on top of a SessionStore."""

    def __init__(self, store: SessionStore, config: SessionConfig) -> None:
        self.store = store
        self.config = config

    async def create(
        self,
        user_id: uuid.UUID,
        device_label: str,
        ip: str,
        user_agent: str,
    ) -> Session:
        """Create a new session, enforcing the concurrent-session cap.

        When the user already has ``max_concurrent`` active sessions, the
        oldest one is revoked first.
        """
        await self._evict_to_make_room(user_id)
        salt = self.config.privacy_salt
        now = _now()
        sess = Session(
            id=secrets.token_urlsafe(32),
            user_id=user_id,
            device_label=device_label,
            ip_hash=hash_ip(ip, salt=salt),
            ua_hash=hash_user_agent(user_agent, salt=salt),
            created_at=now,
            last_active_at=now,
            revoked=False,
        )
        await self.store.save(sess)
        return sess

    async def get(self, session_id: str) -> Session | None:
        return await self.store.get(session_id)

    async def touch(self, session_id: str) -> Session:
        """Mark a session as recently active. Raises if expired or revoked."""
        s = await self.store.get(session_id)
        if s is None:
            msg = f"unknown session {session_id}"
            raise SessionRevokedError(msg)
        if s.revoked:
            raise SessionRevokedError(s.id)
        now = _now()
        if (now - s.last_active_at).total_seconds() > self.config.idle_timeout_sec:
            raise SessionExpiredError(s.id)
        if (now - s.created_at).total_seconds() > self.config.absolute_timeout_sec:
            raise SessionExpiredError(s.id)
        updated = replace(s, last_active_at=now)
        await self.store.save(updated)
        return updated

    async def revoke(self, session_id: str) -> None:
        s = await self.store.get(session_id)
        if s is None:
            return
        await self.store.save(replace(s, revoked=True))

    async def revoke_all_for_user(self, user_id: uuid.UUID) -> int:
        sessions = await self.store.list_for_user(user_id)
        count = 0
        for s in sessions:
            if not s.revoked:
                await self.store.save(replace(s, revoked=True))
                count += 1
        return count

    async def list_active_for_user(self, user_id: uuid.UUID) -> list[Session]:
        all_for_user = await self.store.list_for_user(user_id)
        return [s for s in all_for_user if not s.revoked]

    async def _evict_to_make_room(self, user_id: uuid.UUID) -> None:
        """If at the cap, revoke the oldest active session."""
        active = await self.list_active_for_user(user_id)
        if len(active) < self.config.max_concurrent:
            return
        oldest = min(active, key=lambda s: s.created_at)
        await self.store.save(replace(oldest, revoked=True))


__all__ = [
    "InMemorySessionStore",
    "Session",
    "SessionConfig",
    "SessionExpiredError",
    "SessionRevokedError",
    "SessionService",
    "SessionStore",
    "hash_ip",
    "hash_user_agent",
]


def _iter_active(sessions: Iterable[Session]) -> Iterable[Session]:
    """Helper retained as a future hook for the Valkey backend."""
    return (s for s in sessions if not s.revoked)
