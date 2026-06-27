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
    ACTIVE = "active"
    RETIRED = "retired"
    # STAGING intentionally omitted — promotion gates land in #124. Add
    # the enum value back when the staging transition is wired with
    # explicit guards.


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
    # Wall-clock epoch of the last time this version was activated;
    # `None` for versions that were never activated. Used as the
    # tiebreaker for `rollback` so the previously-live version wins
    # over a higher-numbered version that was deployed-then-retired
    # without ever serving live traffic.
    last_activated_at_epoch: float | None = None


class VersionNotFoundError(Exception):
    """Raised when a version id or strategy id does not exist."""


class VersionInvalidConfigError(ValueError):
    """Raised when ``deploy`` cannot serialize the supplied config."""


# Hard cap on the size of a single code blob. Plugins should be small;
# a 5 MB ceiling catches operator footguns (passing a tarball or a
# bundled artifact) without restricting any realistic strategy.
_MAX_CODE_SIZE_BYTES = 5 * 1024 * 1024


class StrategyRegistry(Protocol):
    """Pluggable persistence for :class:`StrategyVersion` records."""

    async def get(self, version_id: str) -> StrategyVersion | None: ...
    async def save(self, version: StrategyVersion) -> None: ...
    async def list_for_strategy(self, strategy_id: str) -> list[StrategyVersion]: ...
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

    async def list_for_strategy(self, strategy_id: str) -> list[StrategyVersion]:
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
    """Hash a config dict over its canonical JSON encoding.

    `sort_keys=True` recursively canonicalizes dict key ordering. Lists
    keep their insertion order — list-typed config values are treated
    as ordered sequences (changing order produces a different hash).

    `allow_nan=False` so NaN/Infinity float values raise
    :class:`VersionInvalidConfigError` rather than embedding non-standard
    JSON tokens that would silently hash inconsistently.
    """
    try:
        payload = json.dumps(
            config,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        msg = (
            "config must be JSON-serializable with finite floats; "
            f"got {type(config).__name__} with error: {exc}"
        )
        raise VersionInvalidConfigError(msg) from exc
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
        if len(code) > _MAX_CODE_SIZE_BYTES:
            msg = f"code blob exceeds {_MAX_CODE_SIZE_BYTES} bytes (got {len(code)})"
            raise ValueError(msg)
        code_hash = _sha256(code)
        config_hash = _canonical_config_hash(config)
        # NOTE: the in-memory registry serializes deploy via the per-
        # strategy asyncio.Lock below. A DB-backed registry MUST use a
        # strongly consistent read of `list_for_strategy` or a DB-level
        # sequence to allocate `version_number`, otherwise two pods can
        # independently compute the same number and double-write.
        async with self._lock(strategy_id):
            existing = await self.registry.find_by_hashes(strategy_id, code_hash, config_hash)
            if existing is not None:
                return existing
            siblings = await self.registry.list_for_strategy(strategy_id)
            version_number = max((v.version_number for v in siblings), default=0) + 1
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
        async with self._lock(target.strategy_id):
            # Re-fetch + re-check inside the lock so a concurrent
            # `retire` between the entry check and the save cannot be
            # silently overwritten.
            current = await self.registry.get(version_id)
            if current is None:
                raise VersionNotFoundError(version_id)
            if current.status == VersionStatus.RETIRED:
                msg = f"version {version_id} is retired and cannot be activated"
                raise ValueError(msg)
            for sibling in await self.registry.list_for_strategy(current.strategy_id):
                if sibling.id != current.id and sibling.status == VersionStatus.ACTIVE:
                    await self.registry.save(replace(sibling, status=VersionStatus.RETIRED))
            updated = replace(
                current,
                status=VersionStatus.ACTIVE,
                last_activated_at_epoch=time.time(),
            )
            await self.registry.save(updated)
            return updated

    async def rollback(self, strategy_id: str) -> StrategyVersion:
        """Flip to the most recently-active prior version.

        The candidate is the previously-live version (largest
        `last_activated_at_epoch` among RETIRED), not the highest-
        numbered RETIRED version. A version that was deployed-then-
        retired without ever serving live traffic is *not* a rollback
        target.
        """
        async with self._lock(strategy_id):
            siblings = await self.registry.list_for_strategy(strategy_id)
            current_active = next(
                (v for v in siblings if v.status == VersionStatus.ACTIVE),
                None,
            )
            previously_active = [
                v
                for v in siblings
                if v.status == VersionStatus.RETIRED
                and v.last_activated_at_epoch is not None
                and (current_active is None or v.id != current_active.id)
            ]
            if not previously_active:
                raise VersionNotFoundError(f"no rollback candidate for strategy {strategy_id}")
            previous = max(
                previously_active,
                key=lambda v: v.last_activated_at_epoch or 0.0,
            )
            if current_active is not None:
                await self.registry.save(replace(current_active, status=VersionStatus.RETIRED))
            promoted = replace(
                previous,
                status=VersionStatus.ACTIVE,
                last_activated_at_epoch=time.time(),
            )
            await self.registry.save(promoted)
            return promoted

    async def retire(
        self,
        version_id: str,
        *,
        allow_retire_active: bool = False,
    ) -> StrategyVersion:
        """Retire a version.

        Retiring the *currently ACTIVE* version leaves the strategy with
        no live deployment. Callers that intentionally take a strategy
        offline must pass ``allow_retire_active=True`` so the silent
        stop is explicit at the call site.
        """
        target = await self.registry.get(version_id)
        if target is None:
            raise VersionNotFoundError(version_id)
        async with self._lock(target.strategy_id):
            current = await self.registry.get(version_id)
            if current is None:
                raise VersionNotFoundError(version_id)
            if current.status == VersionStatus.ACTIVE and not allow_retire_active:
                msg = (
                    f"version {version_id} is the active deployment; "
                    "pass allow_retire_active=True to take the strategy "
                    "offline, or activate a successor first"
                )
                raise ValueError(msg)
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

    async def get_active(self, strategy_id: str) -> StrategyVersion | None:
        for v in await self.registry.list_for_strategy(strategy_id):
            if v.status == VersionStatus.ACTIVE:
                return v
        return None


__all__ = [
    "InMemoryStrategyRegistry",
    "StrategyRegistry",
    "StrategyVersion",
    "StrategyVersionService",
    "VersionInvalidConfigError",
    "VersionNotFoundError",
    "VersionStatus",
]
