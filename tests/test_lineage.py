"""Tests for engine.observability.lineage — data lineage DAG."""

from __future__ import annotations

import pytest

from engine.observability.lineage import (
    InMemoryLineageStore,
    LineageEdge,
    LineageError,
    LineageNode,
    LineageService,
    NodeKind,
)


@pytest.fixture
def service():
    return LineageService(store=InMemoryLineageStore())


class TestNodeKind:
    def test_node_kinds(self):
        assert NodeKind.PROVIDER.value == "provider"
        assert NodeKind.BAR.value == "bar"
        assert NodeKind.BACKTEST.value == "backtest"
        assert NodeKind.REPORT.value == "report"
        assert NodeKind.SIGNAL.value == "signal"


class TestNodeAndEdge:
    @pytest.mark.asyncio
    async def test_register_node(self, service):
        n = await service.register_node(
            kind=NodeKind.PROVIDER, name="yahoo", attributes={"asset": "equity"}
        )
        assert n.id
        assert n.kind == NodeKind.PROVIDER

    @pytest.mark.asyncio
    async def test_link_creates_edge(self, service):
        a = await service.register_node(NodeKind.PROVIDER, "yahoo", {})
        b = await service.register_node(NodeKind.BAR, "AAPL_2024_01_02", {})
        e = await service.link(parent_id=a.id, child_id=b.id, relation="produced")
        assert isinstance(e, LineageEdge)
        assert e.parent_id == a.id
        assert e.child_id == b.id

    @pytest.mark.asyncio
    async def test_link_rejects_unknown_node(self, service):
        a = await service.register_node(NodeKind.PROVIDER, "yahoo", {})
        with pytest.raises(LineageError):
            await service.link(parent_id=a.id, child_id="nope", relation="x")


class TestTraversal:
    @pytest.mark.asyncio
    async def test_ancestors_of_report(self, service):
        p = await service.register_node(NodeKind.PROVIDER, "yahoo", {})
        bar = await service.register_node(NodeKind.BAR, "AAPL", {})
        bt = await service.register_node(NodeKind.BACKTEST, "bt-1", {})
        rpt = await service.register_node(NodeKind.REPORT, "r-1", {})
        await service.link(parent_id=p.id, child_id=bar.id, relation="produced")
        await service.link(parent_id=bar.id, child_id=bt.id, relation="consumed")
        await service.link(parent_id=bt.id, child_id=rpt.id, relation="generated")
        ancestors = await service.ancestors(rpt.id)
        ids = {n.id for n in ancestors}
        assert ids == {p.id, bar.id, bt.id}

    @pytest.mark.asyncio
    async def test_descendants_of_provider(self, service):
        p = await service.register_node(NodeKind.PROVIDER, "yahoo", {})
        bar = await service.register_node(NodeKind.BAR, "AAPL", {})
        bt = await service.register_node(NodeKind.BACKTEST, "bt-1", {})
        await service.link(parent_id=p.id, child_id=bar.id, relation="produced")
        await service.link(parent_id=bar.id, child_id=bt.id, relation="consumed")
        descendants = await service.descendants(p.id)
        ids = {n.id for n in descendants}
        assert ids == {bar.id, bt.id}

    @pytest.mark.asyncio
    async def test_ancestors_of_unknown_returns_empty(self, service):
        out = await service.ancestors("does-not-exist")
        assert out == []


class TestCycleDetection:
    @pytest.mark.asyncio
    async def test_self_loop_rejected(self, service):
        a = await service.register_node(NodeKind.BAR, "x", {})
        with pytest.raises(LineageError, match="cycle"):
            await service.link(parent_id=a.id, child_id=a.id, relation="x")

    @pytest.mark.asyncio
    async def test_back_edge_rejected(self, service):
        a = await service.register_node(NodeKind.PROVIDER, "p", {})
        b = await service.register_node(NodeKind.BAR, "b", {})
        await service.link(parent_id=a.id, child_id=b.id, relation="x")
        with pytest.raises(LineageError, match="cycle"):
            await service.link(parent_id=b.id, child_id=a.id, relation="x")


class TestQueries:
    @pytest.mark.asyncio
    async def test_list_nodes_by_kind(self, service):
        await service.register_node(NodeKind.PROVIDER, "yahoo", {})
        await service.register_node(NodeKind.PROVIDER, "polygon", {})
        await service.register_node(NodeKind.BAR, "AAPL", {})
        providers = await service.list_by_kind(NodeKind.PROVIDER)
        assert len(providers) == 2

    @pytest.mark.asyncio
    async def test_get_returns_node(self, service):
        a = await service.register_node(NodeKind.BAR, "x", {"src": "test"})
        out = await service.get(a.id)
        assert out is not None
        assert out.attributes["src"] == "test"


class TestEntities:
    def test_node_dataclass_fields(self):
        n = LineageNode(
            id="x",
            kind=NodeKind.BAR,
            name="AAPL",
            attributes={"k": "v"},
            created_at_epoch=1.0,
        )
        assert n.attributes["k"] == "v"

    def test_edge_dataclass_fields(self):
        e = LineageEdge(
            parent_id="p",
            child_id="c",
            relation="produced",
            created_at_epoch=1.0,
        )
        assert e.relation == "produced"


class TestValidation:
    @pytest.mark.asyncio
    async def test_register_empty_name_rejected(self, service):
        with pytest.raises(LineageError):
            await service.register_node(NodeKind.BAR, "", {})

    @pytest.mark.asyncio
    async def test_link_empty_relation_rejected(self, service):
        a = await service.register_node(NodeKind.BAR, "a", {})
        b = await service.register_node(NodeKind.BAR, "b", {})
        with pytest.raises(LineageError):
            await service.link(parent_id=a.id, child_id=b.id, relation="")
