"""Tests for engine.core.strategy_versioning — versioned strategy lifecycle."""

from __future__ import annotations

import uuid

import pytest

from engine.core.strategy_versioning import (
    InMemoryStrategyRegistry,
    StrategyVersion,
    StrategyVersionService,
    VersionAlreadyExistsError,
    VersionNotFoundError,
    VersionStatus,
)


@pytest.fixture
def registry():
    return InMemoryStrategyRegistry()


@pytest.fixture
def service(registry):
    return StrategyVersionService(registry=registry)


def _strategy() -> str:
    return f"strat-{uuid.uuid4().hex[:8]}"


CODE_A = b"def evaluate(): return 1\n"
CODE_B = b"def evaluate(): return 2\n"


class TestDeploy:
    @pytest.mark.asyncio
    async def test_deploy_creates_draft_version(self, service):
        sid = _strategy()
        v = await service.deploy(strategy_id=sid, code=CODE_A, config={})
        assert v.strategy_id == sid
        assert v.status == VersionStatus.DRAFT
        assert v.code_hash
        assert len(v.code_hash) >= 32

    @pytest.mark.asyncio
    async def test_deploy_assigns_monotonic_versions(self, service):
        sid = _strategy()
        a = await service.deploy(strategy_id=sid, code=CODE_A, config={})
        b = await service.deploy(strategy_id=sid, code=CODE_B, config={})
        assert b.version_number > a.version_number

    @pytest.mark.asyncio
    async def test_deploy_with_identical_blob_returns_existing(self, service):
        sid = _strategy()
        a = await service.deploy(strategy_id=sid, code=CODE_A, config={})
        b = await service.deploy(strategy_id=sid, code=CODE_A, config={})
        assert a.id == b.id

    @pytest.mark.asyncio
    async def test_deploy_with_changed_config_creates_new_version(self, service):
        sid = _strategy()
        a = await service.deploy(strategy_id=sid, code=CODE_A, config={"x": 1})
        b = await service.deploy(strategy_id=sid, code=CODE_A, config={"x": 2})
        assert a.id != b.id


class TestActivate:
    @pytest.mark.asyncio
    async def test_activate_promotes_version_to_active(self, service):
        sid = _strategy()
        v = await service.deploy(sid, CODE_A, {})
        out = await service.activate(version_id=v.id)
        assert out.status == VersionStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_activate_demotes_prior_active(self, service):
        sid = _strategy()
        a = await service.deploy(sid, CODE_A, {})
        b = await service.deploy(sid, CODE_B, {})
        await service.activate(version_id=a.id)
        await service.activate(version_id=b.id)
        a_out = await service.get(a.id)
        b_out = await service.get(b.id)
        assert a_out.status == VersionStatus.RETIRED
        assert b_out.status == VersionStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_only_one_active_version_per_strategy(self, service):
        sid = _strategy()
        a = await service.deploy(sid, CODE_A, {})
        b = await service.deploy(sid, CODE_B, {})
        await service.activate(version_id=a.id)
        await service.activate(version_id=b.id)
        active = await service.get_active(strategy_id=sid)
        assert active is not None
        assert active.id == b.id


class TestRollback:
    @pytest.mark.asyncio
    async def test_rollback_restores_previous_active(self, service):
        sid = _strategy()
        a = await service.deploy(sid, CODE_A, {})
        b = await service.deploy(sid, CODE_B, {})
        await service.activate(version_id=a.id)
        await service.activate(version_id=b.id)
        rolled = await service.rollback(strategy_id=sid)
        assert rolled.id == a.id
        assert rolled.status == VersionStatus.ACTIVE
        b_out = await service.get(b.id)
        assert b_out.status == VersionStatus.RETIRED

    @pytest.mark.asyncio
    async def test_rollback_fails_when_no_prior_version(self, service):
        sid = _strategy()
        a = await service.deploy(sid, CODE_A, {})
        await service.activate(version_id=a.id)
        with pytest.raises(VersionNotFoundError):
            await service.rollback(strategy_id=sid)


class TestRetire:
    @pytest.mark.asyncio
    async def test_retired_version_cannot_be_activated(self, service):
        sid = _strategy()
        v = await service.deploy(sid, CODE_A, {})
        await service.retire(version_id=v.id)
        with pytest.raises(ValueError, match="retired"):
            await service.activate(version_id=v.id)

    @pytest.mark.asyncio
    async def test_retire_active_version_clears_active_pointer(self, service):
        sid = _strategy()
        v = await service.deploy(sid, CODE_A, {})
        await service.activate(version_id=v.id)
        await service.retire(version_id=v.id)
        active = await service.get_active(strategy_id=sid)
        assert active is None


class TestImmutability:
    @pytest.mark.asyncio
    async def test_version_record_is_frozen(self, service):
        sid = _strategy()
        v = await service.deploy(sid, CODE_A, {})
        with pytest.raises((AttributeError, TypeError)):
            v.code_hash = "tampered"  # type: ignore[misc]

    @pytest.mark.asyncio
    async def test_version_id_is_uuid(self, service):
        sid = _strategy()
        v = await service.deploy(sid, CODE_A, {})
        parsed = uuid.UUID(v.id)
        assert parsed.version == 4


class TestListing:
    @pytest.mark.asyncio
    async def test_list_versions_ordered_oldest_first(self, service):
        sid = _strategy()
        a = await service.deploy(sid, CODE_A, {})
        b = await service.deploy(sid, CODE_B, {})
        out = await service.list_for_strategy(strategy_id=sid)
        assert [v.id for v in out] == [a.id, b.id]

    @pytest.mark.asyncio
    async def test_list_filters_by_status(self, service):
        sid = _strategy()
        a = await service.deploy(sid, CODE_A, {})
        await service.deploy(sid, CODE_B, {})
        await service.activate(version_id=a.id)
        active_only = await service.list_for_strategy(
            strategy_id=sid, status=VersionStatus.ACTIVE
        )
        assert len(active_only) == 1
        assert active_only[0].id == a.id


class TestErrorPaths:
    @pytest.mark.asyncio
    async def test_get_unknown_returns_none(self, service):
        out = await service.get("definitely-not-real")
        assert out is None

    @pytest.mark.asyncio
    async def test_activate_unknown_raises(self, service):
        with pytest.raises(VersionNotFoundError):
            await service.activate(version_id="nope")

    @pytest.mark.asyncio
    async def test_deploy_rejects_empty_code(self, service):
        sid = _strategy()
        with pytest.raises(ValueError, match="code"):
            await service.deploy(strategy_id=sid, code=b"", config={})


class TestEntity:
    def test_strategy_version_dataclass_shape(self):
        sid = _strategy()
        v = StrategyVersion(
            id=str(uuid.uuid4()),
            strategy_id=sid,
            version_number=1,
            code_hash="a" * 64,
            config_hash="b" * 64,
            status=VersionStatus.DRAFT,
            created_at_epoch=1.0,
        )
        assert v.strategy_id == sid

    def test_already_exists_error_class(self):
        assert issubclass(VersionAlreadyExistsError, Exception)
