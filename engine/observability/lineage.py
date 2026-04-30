"""Data lineage DAG.

Tracks the chain of transformations from raw provider feeds through
bars, signals, backtests, and into final reports. Each transformation
is a directed edge between two :class:`LineageNode` records; the graph
is enforced acyclic at insert time so traversal is always finite.

Three abstractions:

- :class:`NodeKind` — typed identity for the kind of artifact a node
  represents (provider / bar / signal / backtest / report).
- :class:`LineageStore` Protocol — pluggable persistence layer.
- :class:`InMemoryLineageStore` — process-local backend; single-pod /
  tests. DB-backed implementation lands in a follow-up.
- :class:`LineageService` — register_node / link / ancestors /
  descendants / list_by_kind / get.

The service rejects cycles (self-loops + back-edges) at link time so
``ancestors`` and ``descendants`` are guaranteed to terminate.
"""

from __future__ import annotations

import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


class NodeKind(StrEnum):
    PROVIDER = "provider"
    BAR = "bar"
    SIGNAL = "signal"
    BACKTEST = "backtest"
    REPORT = "report"


class LineageError(Exception):
    """Raised on malformed lineage operations (bad ids, cycles, empty inputs)."""


@dataclass(frozen=True)
class LineageNode:
    id: str
    kind: NodeKind
    name: str
    attributes: dict[str, Any] = field(default_factory=dict)
    created_at_epoch: float = 0.0


@dataclass(frozen=True)
class LineageEdge:
    parent_id: str
    child_id: str
    relation: str
    created_at_epoch: float = 0.0


class LineageStore(Protocol):
    async def save_node(self, node: LineageNode) -> None: ...
    async def get_node(self, node_id: str) -> LineageNode | None: ...
    async def add_edge(self, edge: LineageEdge) -> None: ...
    async def children_of(self, node_id: str) -> list[str]: ...
    async def parents_of(self, node_id: str) -> list[str]: ...
    async def list_by_kind(self, kind: NodeKind) -> list[LineageNode]: ...


class InMemoryLineageStore:
    """Process-local store. Single-pod / tests only."""

    def __init__(self) -> None:
        self._nodes: dict[str, LineageNode] = {}
        self._children: dict[str, list[str]] = defaultdict(list)
        self._parents: dict[str, list[str]] = defaultdict(list)

    async def save_node(self, node: LineageNode) -> None:
        self._nodes[node.id] = node

    async def get_node(self, node_id: str) -> LineageNode | None:
        return self._nodes.get(node_id)

    async def add_edge(self, edge: LineageEdge) -> None:
        self._children[edge.parent_id].append(edge.child_id)
        self._parents[edge.child_id].append(edge.parent_id)

    async def children_of(self, node_id: str) -> list[str]:
        return list(self._children.get(node_id, ()))

    async def parents_of(self, node_id: str) -> list[str]:
        return list(self._parents.get(node_id, ()))

    async def list_by_kind(self, kind: NodeKind) -> list[LineageNode]:
        return [n for n in self._nodes.values() if n.kind == kind]


class LineageService:
    """High-level lineage operations on top of a :class:`LineageStore`."""

    def __init__(self, store: LineageStore) -> None:
        self.store = store

    async def register_node(
        self,
        kind: NodeKind,
        name: str,
        attributes: dict[str, Any],
    ) -> LineageNode:
        if not name.strip():
            msg = "name must be non-empty"
            raise LineageError(msg)
        node = LineageNode(
            id=str(uuid.uuid4()),
            kind=kind,
            name=name,
            attributes=dict(attributes),
            created_at_epoch=time.time(),
        )
        await self.store.save_node(node)
        return node

    async def get(self, node_id: str) -> LineageNode | None:
        return await self.store.get_node(node_id)

    async def link(
        self, *, parent_id: str, child_id: str, relation: str
    ) -> LineageEdge:
        if not relation.strip():
            msg = "relation must be non-empty"
            raise LineageError(msg)
        if parent_id == child_id:
            msg = f"link would create a self-loop cycle on {parent_id}"
            raise LineageError(msg)
        parent = await self.store.get_node(parent_id)
        child = await self.store.get_node(child_id)
        if parent is None:
            msg = f"unknown parent_id {parent_id}"
            raise LineageError(msg)
        if child is None:
            msg = f"unknown child_id {child_id}"
            raise LineageError(msg)
        if await self._is_reachable(start=child_id, target=parent_id):
            msg = (
                f"link {parent_id} -> {child_id} would create a cycle "
                f"(parent already reachable from child)"
            )
            raise LineageError(msg)
        edge = LineageEdge(
            parent_id=parent_id,
            child_id=child_id,
            relation=relation,
            created_at_epoch=time.time(),
        )
        await self.store.add_edge(edge)
        return edge

    async def _is_reachable(self, *, start: str, target: str) -> bool:
        if start == target:
            return True
        seen: set[str] = {start}
        frontier: list[str] = [start]
        while frontier:
            current = frontier.pop(0)
            for child_id in await self.store.children_of(current):
                if child_id == target:
                    return True
                if child_id not in seen:
                    seen.add(child_id)
                    frontier.append(child_id)
        return False

    async def ancestors(self, node_id: str) -> list[LineageNode]:
        if await self.store.get_node(node_id) is None:
            return []
        seen: set[str] = set()
        frontier: list[str] = [node_id]
        while frontier:
            current = frontier.pop(0)
            for parent_id in await self.store.parents_of(current):
                if parent_id not in seen:
                    seen.add(parent_id)
                    frontier.append(parent_id)
        out: list[LineageNode] = []
        for i in seen:
            n = await self.store.get_node(i)
            if n is not None:
                out.append(n)
        return out

    async def descendants(self, node_id: str) -> list[LineageNode]:
        if await self.store.get_node(node_id) is None:
            return []
        seen: set[str] = set()
        frontier: list[str] = [node_id]
        while frontier:
            current = frontier.pop(0)
            for child_id in await self.store.children_of(current):
                if child_id not in seen:
                    seen.add(child_id)
                    frontier.append(child_id)
        out: list[LineageNode] = []
        for i in seen:
            n = await self.store.get_node(i)
            if n is not None:
                out.append(n)
        return out

    async def list_by_kind(self, kind: NodeKind) -> list[LineageNode]:
        return await self.store.list_by_kind(kind)


__all__ = [
    "InMemoryLineageStore",
    "LineageEdge",
    "LineageError",
    "LineageNode",
    "LineageService",
    "LineageStore",
    "NodeKind",
]
