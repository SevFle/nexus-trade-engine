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
values in raised exceptions, and rejects empty names or values at the
boundary so callers cannot accidentally store sentinel-like data.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class SecretsError(Exception):
    """Raised on malformed input or decrypt failure."""


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
        if not self.current:
            msg = "MasterKey.current must be non-empty"
            raise SecretsError(msg)


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
    """Process-local store. Single-pod / tests."""

    def __init__(self) -> None:
        self._records: dict[str, SecretRecord] = {}

    async def put_record(self, record: SecretRecord) -> None:
        self._records[record.name] = record

    async def get_record(self, name: str) -> SecretRecord | None:
        return self._records.get(name)

    async def delete_record(self, name: str) -> None:
        self._records.pop(name, None)

    async def list_names(self) -> list[str]:
        return sorted(self._records.keys())

    async def all_records(self) -> list[SecretRecord]:
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
    master_key: MasterKey
    _master_key: MasterKey = field(init=False)

    def __post_init__(self) -> None:
        self._master_key = self.master_key

    def _fernet(self) -> MultiFernet:
        return _build_fernet(self._master_key)

    async def put(self, name: str, value: str) -> None:
        if not name.strip():
            msg = "secret name must be non-empty"
            raise SecretsError(msg)
        if not value:
            msg = "secret value must be non-empty"
            raise SecretsError(msg)
        ct = self._fernet().encrypt(value.encode("utf-8"))
        now = time.time()
        existing = await self.store.get_record(name)
        created = existing.created_at_epoch if existing is not None else now
        await self.store.put_record(
            SecretRecord(
                name=name,
                ciphertext=ct,
                created_at_epoch=created,
                updated_at_epoch=now,
            )
        )

    async def get(self, name: str) -> str | None:
        rec = await self.store.get_record(name)
        if rec is None:
            return None
        try:
            pt = self._fernet().decrypt(rec.ciphertext)
        except InvalidToken as exc:
            msg = f"failed to decrypt secret {name!r}: token invalid"
            raise SecretsError(msg) from exc
        return pt.decode("utf-8")

    async def delete(self, name: str) -> None:
        await self.store.delete_record(name)

    async def list_names(self) -> list[str]:
        return await self.store.list_names()

    def rotate_master_key(self, *, new_current: bytes) -> None:
        """Promote current master to previous and install new_current.

        After this call, run :meth:`reencrypt_all` to migrate stored
        ciphertext, then drop the previous key by constructing a fresh
        :class:`MasterKey` without ``previous``.
        """
        if not new_current:
            msg = "new_current must be non-empty"
            raise SecretsError(msg)
        self._master_key = MasterKey(
            current=new_current, previous=self._master_key.current
        )

    async def reencrypt_all(self) -> int:
        """Re-encrypt every stored secret under the current master key.

        Returns the number of records migrated. Records that fail to
        decrypt with either current or previous are left in place and
        a :class:`SecretsError` is raised after the pass completes so
        the operator can investigate.
        """
        records = await self.store.all_records()
        f = self._fernet()
        failures: list[str] = []
        migrated = 0
        for rec in records:
            try:
                pt = f.decrypt(rec.ciphertext)
            except InvalidToken:
                failures.append(rec.name)
                continue
            ct = f.encrypt(pt)
            await self.store.put_record(
                SecretRecord(
                    name=rec.name,
                    ciphertext=ct,
                    created_at_epoch=rec.created_at_epoch,
                    updated_at_epoch=time.time(),
                )
            )
            migrated += 1
        if failures:
            msg = (
                f"reencrypt_all completed with {len(failures)} undecryptable "
                f"records: {sorted(failures)!r}"
            )
            raise SecretsError(msg)
        return migrated


__all__ = [
    "InMemorySecretStore",
    "MasterKey",
    "SecretRecord",
    "SecretStore",
    "SecretsError",
    "SecretsService",
    "generate_master_key",
]
