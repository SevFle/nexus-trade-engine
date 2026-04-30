"""Strategy versioning, rollback, and immutable deployments.

A :class:`StrategyVersion` is a frozen, content-addressed record of one
deploy: a SHA-256 hash of the strategy's code blob plus a hash of its
canonical-JSON config. The same code+config never produces two version
records — the registry deduplicates so re-deploys are idempotent.

Lifecycle:
- ``deploy``  — content-hash the blob, return existing record if seen,
                else create a new ``DRAFT`` version with a monotonic
                version number scoped per strategy.
- ``activate`` — promote a version to ``ACTIVE``, demoting the prior
                active version to ``RETIRED``. Exactly one active version
                per strategy at any time.
- ``rollback`` — flip back to the most recent prior active version,
                retiring the current active.
- ``retire``  — explicitly mark a version as ``RETIRED``.

All mutations are serialized through an ``asyncio.Lock`` so concurrent
``deploy``/``activate``/``rollback`` calls cannot corrupt the active-
version pointer or duplicate version numbers.

The store layer is a Protocol so the in-memory backend (single-pod,
tests) and a DB-backed backend (multi-pod, follow-up) share the same
surface.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Protocol


class VersionStatus(StrEnum):
    DRAFT = "draft"
    STAGING = "staging"
    ACTIVE = "active"
    RETIRED = "retired"


@dataclass(frozen=True)
class StrategyVersion:
    """One immutable deployment record. Frozen — registry replaces on update."""

    id: str
    strategy_id: str
    version_number: int
    code_hash: str
    config_hash: str
    status: VersionStatus
    created_at_epoch: float


class VersionNotFoundError(Exception):
    """Raised when a version id or strategy id does not exist."""


class VersionAlreadyExistsError(Exception):
    """Raised when re-deploying with content that doesn't match recorded record."""


class StrategyRegistry(Protocol):
    """Pluggable persistence for :class:`StrategyVersion` records."""

    async def get(self, version_id: str) -> StrategyVersion | None: ...
    async def save(self, version: StrategyVersion) -> None: ...
    async def list_for_strategy(
        self, strategy_id: str
    ) -> list[StrategyVersion]: ...
    async def find_by_hashes(
        self, strategy_id: str, code_hash: str, config_hash: str
    ) -> StrategyVersion | None: ...


class InMemoryStrategyRegistry:
    """Process-local registry. Single-pod / tests only."""

    def __init__(self) -> None:
        self._by_id: dict[str, StrategyVersion] = {}
        self._by_strategy: dict[str, list[str]] = defaultdict(list)

    async def get(self, version_id: str) -> StrategyVersion | None:
        return self._by_id.get(version_id)

    async def save(self, version: StrategyVersion) -> None:
        if version.id not in self._by_id:
            self._by_strategy[version.strategy_id].append(version.id)
        self._by_id[version.id] = version

    async def list_for_strategy(
        self, strategy_id: str
    ) -> list[StrategyVersion]:
        ids = self._by_strategy.get(strategy_id, ())
        return [self._by_id[i] for i in ids if i in self._by_id]

    async def find_by_hashes(
        self, strategy_id: str, code_hash: str, config_hash: str
    ) -> StrategyVersion | None:
        for v in await self.list_for_strategy(strategy_id):
            if v.code_hash == code_hash and v.config_hash == config_hash:
                return v
        return None


def _sha256(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _canonical_config_hash(config: dict[str, Any]) -> str:
    """Hash a config dict over its canonical JSON encoding so semantically
    identical configs (key order independent) hash identically."""
    payload = json.dumps(
        config, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return _sha256(payload.encode("utf-8"))


class StrategyVersionService:
    """Lifecycle operations on top of a :class:`StrategyRegistry`."""

    def __init__(self, registry: StrategyRegistry) -> None:
        self.registry = registry
        self._strategy_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def _lock(self, strategy_id: str) -> asyncio.Lock:
        return self._strategy_locks[strategy_id]

    async def deploy(
        self, strategy_id: str, code: bytes, config: dict[str, Any]
    ) -> StrategyVersion:
        if not code:
            msg = "code blob must not be empty"
            raise ValueError(msg)
        code_hash = _sha256(code)
        config_hash = _canonical_config_hash(config)
        async with self._lock(strategy_id):
            existing = await self.registry.find_by_hashes(
                strategy_id, code_hash, config_hash
            )
            if existing is not None:
                return existing
            siblings = await self.registry.list_for_strategy(strategy_id)
            version_number = (
                max((v.version_number for v in siblings), default=0) + 1
            )
            v = StrategyVersion(
                id=str(uuid.uuid4()),
                strategy_id=strategy_id,
                version_number=version_number,
                code_hash=code_hash,
                config_hash=config_hash,
                status=VersionStatus.DRAFT,
                created_at_epoch=time.time(),
            )
            await self.registry.save(v)
            return v

    async def get(self, version_id: str) -> StrategyVersion | None:
        return await self.registry.get(version_id)

    async def activate(self, version_id: str) -> StrategyVersion:
        target = await self.registry.get(version_id)
        if target is None:
            raise VersionNotFoundError(version_id)
        if target.status == VersionStatus.RETIRED:
            msg = f"version {version_id} is retired and cannot be activated"
            raise ValueError(msg)
        async with self._lock(target.strategy_id):
            current = await self.registry.get(version_id)
            if current is None:
                raise VersionNotFoundError(version_id)
            for sibling in await self.registry.list_for_strategy(
                current.strategy_id
            ):
                if (
                    sibling.id != current.id
                    and sibling.status == VersionStatus.ACTIVE
                ):
                    await self.registry.save(
                        replace(sibling, status=VersionStatus.RETIRED)
                    )
            updated = replace(current, status=VersionStatus.ACTIVE)
            await self.registry.save(updated)
            return updated

    async def rollback(self, strategy_id: str) -> StrategyVersion:
        """Flip to the most recent prior active version."""
        async with self._lock(strategy_id):
            siblings = await self.registry.list_for_strategy(strategy_id)
            current_active = next(
                (v for v in siblings if v.status == VersionStatus.ACTIVE),
                None,
            )
            retired = [
                v
                for v in siblings
                if v.status == VersionStatus.RETIRED
                and (current_active is None or v.id != current_active.id)
            ]
            if not retired:
                raise VersionNotFoundError(
                    f"no rollback candidate for strategy {strategy_id}"
                )
            previous = max(retired, key=lambda v: v.version_number)
            if current_active is not None:
                await self.registry.save(
                    replace(current_active, status=VersionStatus.RETIRED)
                )
            promoted = replace(previous, status=VersionStatus.ACTIVE)
            await self.registry.save(promoted)
            return promoted

    async def retire(self, version_id: str) -> StrategyVersion:
        target = await self.registry.get(version_id)
        if target is None:
            raise VersionNotFoundError(version_id)
        async with self._lock(target.strategy_id):
            current = await self.registry.get(version_id)
            if current is None:
                raise VersionNotFoundError(version_id)
            updated = replace(current, status=VersionStatus.RETIRED)
            await self.registry.save(updated)
            return updated

    async def list_for_strategy(
        self,
        strategy_id: str,
        *,
        status: VersionStatus | None = None,
    ) -> list[StrategyVersion]:
        out = await self.registry.list_for_strategy(strategy_id)
        out.sort(key=lambda v: v.version_number)
        if status is not None:
            out = [v for v in out if v.status == status]
        return out

    async def get_active(
        self, strategy_id: str
    ) -> StrategyVersion | None:
        for v in await self.registry.list_for_strategy(strategy_id):
            if v.status == VersionStatus.ACTIVE:
                return v
        return None


__all__ = [
    "InMemoryStrategyRegistry",
    "StrategyRegistry",
    "StrategyVersion",
    "StrategyVersionService",
    "VersionAlreadyExistsError",
    "VersionNotFoundError",
    "VersionStatus",
]
