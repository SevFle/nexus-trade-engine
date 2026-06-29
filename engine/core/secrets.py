"""Encrypted secret store with master-key rotation.

Values are encrypted at rest with Fernet (AES-128-CBC + HMAC-SHA-256,
authenticated) using a process-held master key. Rotation is supported by
holding both a current and an optional previous key in :class:`MasterKey`;
on rotation the operator calls :meth:`SecretsService.rotate_master_key`
and then :meth:`SecretsService.reencrypt_all` to migrate every stored
ciphertext to the new key.

Two layers:

- :class:`SecretStore` Protocol — pluggable persistence (in-memory
  ships here; DB / KMS-backed adapters arrive in follow-up issues).
- :class:`SecretsService` — wraps a store with put / get / delete /
  list / rotate / reencrypt semantics, and is the only layer that ever
  sees plaintext.

The service never logs plaintext or ciphertext, never includes secret
values or names in raised exceptions on the rotation path, and rejects
empty / whitespace-only names and empty values at the boundary so callers
cannot accidentally store sentinel-like data.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import time
from dataclasses import InitVar, dataclass, field
from typing import Protocol

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

_FERNET_KEY_BYTES = 32


class SecretsError(Exception):
    """Raised on malformed input or decrypt failure."""


def _validate_fernet_key(key: bytes) -> None:
    """Reject anything that is not a url-safe-base64 32-byte Fernet key."""
    if not key:
        msg = "Fernet key must be non-empty"
        raise SecretsError(msg)
    try:
        decoded = base64.urlsafe_b64decode(key)
    except (binascii.Error, ValueError) as exc:
        msg = "Fernet key must be url-safe base64"
        raise SecretsError(msg) from exc
    if len(decoded) != _FERNET_KEY_BYTES:
        msg = f"Fernet key must decode to exactly {_FERNET_KEY_BYTES} bytes"
        raise SecretsError(msg)


def _normalize_name(name: str) -> str:
    """Strip whitespace and reject empty or whitespace-only names."""
    stripped = name.strip()
    if not stripped:
        msg = "secret name must be non-empty / non-whitespace"
        raise SecretsError(msg)
    return stripped


def generate_master_key() -> bytes:
    """Generate a fresh 32-byte url-safe-base64 Fernet key."""
    return Fernet.generate_key()


@dataclass(frozen=True)
class MasterKey:
    """Wraps a current Fernet key and an optional previous one.

    The previous key allows decrypting records that were encrypted under
    the prior master before :meth:`SecretsService.reencrypt_all` has
    finished migrating them.
    """

    current: bytes
    previous: bytes | None = None

    def __post_init__(self) -> None:
        _validate_fernet_key(self.current)
        if self.previous is not None:
            _validate_fernet_key(self.previous)


@dataclass(frozen=True)
class SecretRecord:
    """One stored secret. Plaintext is *never* held here."""

    name: str
    ciphertext: bytes
    created_at_epoch: float
    updated_at_epoch: float


class SecretStore(Protocol):
    async def put_record(self, record: SecretRecord) -> None: ...
    async def get_record(self, name: str) -> SecretRecord | None: ...
    async def delete_record(self, name: str) -> None: ...
    async def list_names(self) -> list[str]: ...
    async def all_records(self) -> list[SecretRecord]: ...


class InMemorySecretStore:
    """Process-local store. Single-pod / tests. Concurrency-safe."""

    def __init__(self) -> None:
        self._records: dict[str, SecretRecord] = {}
        self._lock = asyncio.Lock()

    async def put_record(self, record: SecretRecord) -> None:
        async with self._lock:
            self._records[record.name] = record

    async def get_record(self, name: str) -> SecretRecord | None:
        async with self._lock:
            return self._records.get(name)

    async def delete_record(self, name: str) -> None:
        async with self._lock:
            self._records.pop(name, None)

    async def list_names(self) -> list[str]:
        async with self._lock:
            return sorted(self._records.keys())

    async def all_records(self) -> list[SecretRecord]:
        async with self._lock:
            return list(self._records.values())


def _build_fernet(master: MasterKey) -> MultiFernet:
    """Build a MultiFernet that encrypts with current and decrypts via either key."""
    keys = [Fernet(master.current)]
    if master.previous is not None:
        keys.append(Fernet(master.previous))
    return MultiFernet(keys)


@dataclass
class SecretsService:
    """High-level put / get / rotate on top of a :class:`SecretStore`."""

    store: SecretStore
    master_key: InitVar[MasterKey]
    _master_key: MasterKey = field(init=False, repr=False)

    def __post_init__(self, master_key: MasterKey) -> None:
        self._master_key = master_key

    def _fernet(self) -> MultiFernet:
        return _build_fernet(self._master_key)

    async def put(self, name: str, value: str) -> None:
        if not value:
            msg = "secret value must be non-empty"
            raise SecretsError(msg)
        canonical = _normalize_name(name)
        ct = self._fernet().encrypt(value.encode("utf-8"))
        now = time.time()
        existing = await self.store.get_record(canonical)
        created = existing.created_at_epoch if existing is not None else now
        await self.store.put_record(
            SecretRecord(
                name=canonical,
                ciphertext=ct,
                created_at_epoch=created,
                updated_at_epoch=now,
            )
        )

    async def get(self, name: str) -> str | None:
        canonical = _normalize_name(name)
        rec = await self.store.get_record(canonical)
        if rec is None:
            return None
        try:
            pt = self._fernet().decrypt(rec.ciphertext)
        except InvalidToken as exc:
            msg = f"failed to decrypt secret {canonical!r}: token invalid"
            raise SecretsError(msg) from exc
        return pt.decode("utf-8")

    async def delete(self, name: str) -> None:
        canonical = _normalize_name(name)
        await self.store.delete_record(canonical)

    async def list_names(self) -> list[str]:
        return await self.store.list_names()

    def rotate_master_key(self, *, new_current: bytes) -> None:
        """Promote current master to previous and install ``new_current``.

        Refuses to overwrite an existing previous key; that situation
        means a prior rotation never finished its :meth:`reencrypt_all`
        and silently dropping the old previous would render any records
        still encrypted under it permanently unreadable.

        After this call, run :meth:`reencrypt_all` to migrate stored
        ciphertext, then drop the previous key by constructing a fresh
        :class:`MasterKey` without ``previous`` (or call
        :meth:`drop_previous_key` once the migration verifies clean).
        """
        _validate_fernet_key(new_current)
        if self._master_key.previous is not None:
            msg = (
                "rotate_master_key called while a previous key is still "
                "held; run reencrypt_all to drain the previous slot first"
            )
            raise SecretsError(msg)
        self._master_key = MasterKey(current=new_current, previous=self._master_key.current)

    def drop_previous_key(self) -> None:
        """Forget the previous key. Call only after :meth:`reencrypt_all` succeeds."""
        self._master_key = MasterKey(current=self._master_key.current)

    async def reencrypt_all(self) -> int:
        """Re-encrypt every stored secret under the current master key.

        Two-phase to bound partial-failure blast radius:

        - Phase 1: decrypt every record into memory. If any record fails
          to decrypt under either current or previous, abort with
          :class:`SecretsError` *before any writes* — the store is left
          untouched, the operator must keep the previous key installed,
          and the count of failures is reported (names are not, to
          avoid leaking sensitive identifiers).
        - Phase 2: encrypt under current + write each record.

        Phase 2 is not transactional: on a process crash mid-flush some
        records will be on the new key and others on the old. The
        previous key MUST remain installed until a full run completes
        without raising; only then is :meth:`drop_previous_key` safe.

        Returns the number of records migrated.
        """
        records = await self.store.all_records()
        f = self._fernet()
        plaintexts: list[tuple[SecretRecord, bytes]] = []
        failure_count = 0
        for rec in records:
            try:
                pt = f.decrypt(rec.ciphertext)
            except InvalidToken:
                failure_count += 1
                continue
            plaintexts.append((rec, pt))
        if failure_count:
            msg = (
                f"reencrypt_all aborted: {failure_count} record(s) failed "
                "to decrypt under either current or previous master key. "
                "No records were modified. Keep the previous key installed "
                "and investigate."
            )
            raise SecretsError(msg)
        now = time.time()
        for rec, pt in plaintexts:
            ct = f.encrypt(pt)
            await self.store.put_record(
                SecretRecord(
                    name=rec.name,
                    ciphertext=ct,
                    created_at_epoch=rec.created_at_epoch,
                    updated_at_epoch=now,
                )
            )
        return len(plaintexts)


__all__ = [
    "InMemorySecretStore",
    "MasterKey",
    "SecretRecord",
    "SecretStore",
    "SecretsError",
    "SecretsService",
    "generate_master_key",
]
