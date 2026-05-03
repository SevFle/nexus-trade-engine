"""Immutable hash-chained audit log.

Every state change worth auditing (login, order submit, risk override,
kill-switch trip, …) appends one :class:`AuditEvent`. Each event
carries the SHA-256 hash of its own canonical payload plus the hash
of the previous event, forming a tamper-evident chain — any later
mutation to a payload, sequence number, or prev_hash invalidates the
entire downstream chain.

Two layers:

- :class:`AuditLog` Protocol — pluggable persistence (in-memory ships
  here; DB-backed in a follow-up).
- :class:`AuditService` — wraps a log with append + verify_chain
  semantics. Runs every payload through the redaction processor at
  :func:`engine.observability.redact.redact_processor` so secrets in
  caller payloads never reach disk.

Genesis prev_hash is the all-zero 64-char sha256 sentinel.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from engine.observability.redact import redact_processor

_GENESIS_PREV_HASH = "0" * 64


class AuditLogError(Exception):
    """Raised on malformed audit input or chain integrity failure."""


@dataclass(frozen=True)
class AuditEvent:
    """One immutable audit record."""

    id: str
    sequence: int
    event_type: str
    actor_id: str
    payload: dict[str, Any]
    prev_hash: str
    hash: str
    created_at_epoch: float = 0.0


class AuditLog(Protocol):
    async def append(self, event: AuditEvent) -> None: ...
    async def list(self, *, actor_id: str | None = None) -> list[AuditEvent]: ...
    async def get_by_sequence(self, sequence: int) -> AuditEvent | None: ...
    async def last(self) -> AuditEvent | None: ...
    async def all(self) -> list[AuditEvent]: ...


class InMemoryAuditLog:
    """Process-local log. Single-pod / tests."""

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []

    async def append(self, event: AuditEvent) -> None:
        self._events.append(event)

    async def list(self, *, actor_id: str | None = None) -> list[AuditEvent]:
        if actor_id is None:
            return list(self._events)
        return [e for e in self._events if e.actor_id == actor_id]

    async def get_by_sequence(self, sequence: int) -> AuditEvent | None:
        for e in self._events:
            if e.sequence == sequence:
                return e
        return None

    async def last(self) -> AuditEvent | None:
        return self._events[-1] if self._events else None

    async def all(self) -> list[AuditEvent]:
        return list(self._events)


def _canonical_bytes(
    *,
    sequence: int,
    event_type: str,
    actor_id: str,
    payload: dict[str, Any],
    prev_hash: str,
    created_at_epoch: float,
) -> bytes:
    body = {
        "sequence": sequence,
        "event_type": event_type,
        "actor_id": actor_id,
        "payload": payload,
        "prev_hash": prev_hash,
        "created_at_epoch": created_at_epoch,
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _hash_event(
    *,
    sequence: int,
    event_type: str,
    actor_id: str,
    payload: dict[str, Any],
    prev_hash: str,
    created_at_epoch: float,
) -> str:
    return hashlib.sha256(
        _canonical_bytes(
            sequence=sequence,
            event_type=event_type,
            actor_id=actor_id,
            payload=payload,
            prev_hash=prev_hash,
            created_at_epoch=created_at_epoch,
        )
    ).hexdigest()


@dataclass
class AuditService:
    """High-level append + verify on top of an :class:`AuditLog`."""

    log: AuditLog
    _log: AuditLog = field(init=False)

    def __post_init__(self) -> None:
        self._log = self.log

    async def append(
        self,
        event_type: str,
        actor_id: str,
        payload: dict[str, Any],
    ) -> AuditEvent:
        if not event_type.strip():
            msg = "event_type must be non-empty"
            raise AuditLogError(msg)
        if not actor_id.strip():
            msg = "actor_id must be non-empty"
            raise AuditLogError(msg)
        redacted: dict[str, Any] = dict(redact_processor(None, "audit", dict(payload)))
        last = await self._log.last()
        sequence = (last.sequence + 1) if last is not None else 1
        prev_hash = last.hash if last is not None else _GENESIS_PREV_HASH
        created_at_epoch = time.time()
        digest = _hash_event(
            sequence=sequence,
            event_type=event_type,
            actor_id=actor_id,
            payload=redacted,
            prev_hash=prev_hash,
            created_at_epoch=created_at_epoch,
        )
        event = AuditEvent(
            id=str(uuid.uuid4()),
            sequence=sequence,
            event_type=event_type,
            actor_id=actor_id,
            payload=redacted,
            prev_hash=prev_hash,
            hash=digest,
            created_at_epoch=created_at_epoch,
        )
        await self._log.append(event)
        return event

    async def verify_chain(self) -> bool:
        events = await self._log.all()
        expected_prev = _GENESIS_PREV_HASH
        expected_seq = 1
        for ev in events:
            if ev.sequence != expected_seq:
                return False
            if ev.prev_hash != expected_prev:
                return False
            expected = _hash_event(
                sequence=ev.sequence,
                event_type=ev.event_type,
                actor_id=ev.actor_id,
                payload=ev.payload,
                prev_hash=ev.prev_hash,
                created_at_epoch=ev.created_at_epoch,
            )
            if expected != ev.hash:
                return False
            expected_prev = ev.hash
            expected_seq += 1
        return True

    async def list_events(self, *, actor_id: str | None = None) -> list[AuditEvent]:
        return await self._log.list(actor_id=actor_id)

    async def get_by_sequence(self, sequence: int) -> AuditEvent | None:
        return await self._log.get_by_sequence(sequence)


__all__ = [
    "AuditEvent",
    "AuditLog",
    "AuditLogError",
    "AuditService",
    "InMemoryAuditLog",
]
