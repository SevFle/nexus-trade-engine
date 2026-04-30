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
- ``privacy_salt`` must be supplied explicitly in production via
  configuration so all pods share the same salt and IP / UA hashes
  remain comparable. ``SessionConfig`` only generates a random salt as
  a development convenience.

Concurrency:
- ``SessionService`` serializes per-user mutations via an
  ``asyncio.Lock`` so concurrent ``create``/``touch``/``revoke`` calls
  never lose updates or bypass the concurrent-session cap.

Caller responsibilities:
- On privilege elevation (login, role change, MFA pass) the caller MUST
  invoke ``revoke_all_for_user`` *before* ``create`` to defeat session
  fixation. This module does not enforce that policy itself.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import uuid  # noqa: TC003 - referenced as uuid.UUID at runtime via dataclass
from collections import defaultdict
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


_MAX_DEVICE_LABEL_LEN = 256
_MAX_IP_LEN = 64  # IPv6 + brackets fit; longer strings are rejected
_MAX_UA_LEN = 512


@dataclass(frozen=True)
class SessionConfig:
    """Idle / absolute timeouts plus concurrent-session cap.

    ``privacy_salt`` defaults to a per-process random value for
    development convenience. Production deployments must supply a
    stable salt via configuration so IP / UA hashes are consistent
    across pods.
    """

    idle_timeout_sec: int = 900  # 15 min
    absolute_timeout_sec: int = 86_400  # 24 h
    # 32 bytes = 256 bits of CSPRNG entropy in the encoded session id.
    max_concurrent: int = 5
    # Total stored sessions per user (active + revoked). Prevents
    # unbounded growth from long-lived users that accumulate revoked
    # records over time.
    max_total_per_user: int = 1_000
    privacy_salt: str = field(default_factory=lambda: secrets.token_hex(16))


class SessionExpiredError(Exception):
    """Raised when a session has exceeded its idle or absolute timeout."""


class SessionRevokedError(Exception):
    """Raised when an operation targets a revoked session."""


class SessionNotFoundError(Exception):
    """Raised when a referenced session does not exist."""


class SessionStore(Protocol):
    """Pluggable persistence layer for sessions."""

    async def get(self, session_id: str) -> Session | None: ...
    async def save(self, session: Session) -> None: ...
    async def list_for_user(self, user_id: uuid.UUID) -> list[Session]: ...


class InMemorySessionStore:
    """Process-local store. Not for multi-pod — use a Valkey backend."""

    def __init__(self, max_total_per_user: int = 1_000) -> None:
        self._by_id: dict[str, Session] = {}
        # Secondary index so list_for_user is O(user-sessions) instead
        # of O(global-sessions). Append-only oldest-first; eviction is
        # the responsibility of the SessionService caller.
        self._by_user: dict[uuid.UUID, list[str]] = defaultdict(list)
        self._max_total_per_user = max_total_per_user

    async def get(self, session_id: str) -> Session | None:
        return self._by_id.get(session_id)

    async def save(self, session: Session) -> None:
        if session.id not in self._by_id:
            ids = self._by_user[session.user_id]
            ids.append(session.id)
            # Hard cap on total stored records per user to bound memory.
            while len(ids) > self._max_total_per_user:
                drop = ids.pop(0)
                self._by_id.pop(drop, None)
        self._by_id[session.id] = session

    async def list_for_user(self, user_id: uuid.UUID) -> list[Session]:
        ids = self._by_user.get(user_id, ())
        return [self._by_id[i] for i in ids if i in self._by_id]


class SessionService:
    """Lifecycle operations on top of a SessionStore.

    Per-user mutations are serialized through an ``asyncio.Lock`` to
    prevent the concurrent-create race that would otherwise let a user
    exceed ``max_concurrent``, and the touch/revoke lost-update race
    that would resurrect a revoked session.
    """

    def __init__(self, store: SessionStore, config: SessionConfig) -> None:
        self.store = store
        self.config = config
        self._user_locks: dict[uuid.UUID, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._global_lock = asyncio.Lock()

    def _user_lock(self, user_id: uuid.UUID) -> asyncio.Lock:
        return self._user_locks[user_id]

    @staticmethod
    def _validate_input(device_label: str, ip: str, user_agent: str) -> None:
        if len(device_label) > _MAX_DEVICE_LABEL_LEN:
            msg = f"device_label exceeds {_MAX_DEVICE_LABEL_LEN} chars"
            raise ValueError(msg)
        if len(ip) > _MAX_IP_LEN:
            msg = f"ip exceeds {_MAX_IP_LEN} chars"
            raise ValueError(msg)
        if len(user_agent) > _MAX_UA_LEN:
            msg = f"user_agent exceeds {_MAX_UA_LEN} chars"
            raise ValueError(msg)

    async def create(
        self,
        user_id: uuid.UUID,
        device_label: str,
        ip: str,
        user_agent: str,
    ) -> Session:
        """Create a new session, enforcing the concurrent-session cap.

        Caller must invoke ``revoke_all_for_user`` first when the user
        is re-authenticating (password reset, MFA pass, etc.) to defeat
        session-fixation attacks.
        """
        self._validate_input(device_label, ip, user_agent)
        async with self._user_lock(user_id):
            await self._evict_to_make_room_locked(user_id)
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
            raise SessionNotFoundError("session not found")
        async with self._user_lock(s.user_id):
            # Re-fetch under the lock so a concurrent revoke between
            # the initial read and the save cannot be silently
            # overwritten.
            s = await self.store.get(session_id)
            if s is None:
                raise SessionNotFoundError("session not found")
            if s.revoked:
                raise SessionRevokedError("session is revoked")
            now = _now()
            if (now - s.last_active_at).total_seconds() > self.config.idle_timeout_sec:
                raise SessionExpiredError("session has expired")
            if (now - s.created_at).total_seconds() > self.config.absolute_timeout_sec:
                raise SessionExpiredError("session has expired")
            updated = replace(s, last_active_at=now)
            await self.store.save(updated)
            return updated

    async def revoke(self, session_id: str) -> None:
        s = await self.store.get(session_id)
        if s is None:
            return
        async with self._user_lock(s.user_id):
            current = await self.store.get(session_id)
            if current is None or current.revoked:
                return
            await self.store.save(replace(current, revoked=True))

    async def revoke_all_for_user(self, user_id: uuid.UUID) -> int:
        async with self._user_lock(user_id):
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

    async def _evict_to_make_room_locked(self, user_id: uuid.UUID) -> None:
        """If at the cap, revoke the oldest active session.

        Must be called while holding the per-user lock.
        """
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
    "SessionNotFoundError",
    "SessionRevokedError",
    "SessionService",
    "SessionStore",
    "hash_ip",
    "hash_user_agent",
]


def _iter_active(sessions: Iterable[Session]) -> Iterable[Session]:
    """Helper retained as a future hook for the Valkey backend."""
    return (s for s in sessions if not s.revoked)
