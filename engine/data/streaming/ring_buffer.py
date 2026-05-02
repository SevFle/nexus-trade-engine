"""Bounded buffer with explicit drop policy (gh#133).

Why this is here
----------------
A real-time data feed (market quotes, fills, alerts) produces
messages faster than any single consumer can handle, especially
during volatile periods. Without backpressure the choices are bad:

- Block the producer (latency spikes, can lose the network slot).
- Grow an unbounded queue (memory blows up, GC pauses worsen).

This buffer makes the trade-off explicit: it has a fixed capacity
and a documented behaviour when full. Drop counters surface the
backpressure event to monitoring (the SLOs in
``docs/operations/slos.md`` will reference these once the
:class:`MetricsBackend` exporter lands).
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Iterator
from enum import Enum
from typing import Generic, TypeVar

T = TypeVar("T")


class DropPolicy(str, Enum):
    """How a full :class:`BoundedBuffer` reacts to a new ``put``."""

    # Push out the oldest item to make room — preserves the freshest
    # data. Default for live feeds where stale prices are useless.
    DROP_OLDEST = "drop_oldest"
    # Reject the new item — preserves the historical window. Use for
    # event streams where every event must be considered in order
    # until the queue catches up (e.g., audit / alert pipelines that
    # are OK to fail rather than silently lose history).
    DROP_NEWEST = "drop_newest"


class BoundedBuffer(Generic[T]):
    """Thread-safe bounded buffer with a documented drop policy.

    The buffer is a ring with capacity ``maxsize``. ``put`` returns
    ``True`` if the item was accepted, ``False`` if it was dropped
    (after also bumping the appropriate counter).

    Single-process and synchronous on purpose. The async equivalent
    is one of the well-tested asyncio queues; this primitive is for
    the boundary between sync producers and async consumers (and
    vice-versa) where neither stdlib option fits cleanly.
    """

    def __init__(
        self,
        maxsize: int,
        *,
        policy: DropPolicy = DropPolicy.DROP_OLDEST,
    ) -> None:
        if maxsize <= 0:
            raise ValueError("maxsize must be positive")
        self._maxsize = maxsize
        self._policy = policy
        self._items: deque[T] = deque()
        self._lock = threading.Lock()
        self._dropped_oldest = 0
        self._dropped_newest = 0

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def maxsize(self) -> int:
        return self._maxsize

    @property
    def policy(self) -> DropPolicy:
        return self._policy

    @property
    def dropped_oldest(self) -> int:
        return self._dropped_oldest

    @property
    def dropped_newest(self) -> int:
        return self._dropped_newest

    @property
    def dropped_total(self) -> int:
        return self._dropped_oldest + self._dropped_newest

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def is_full(self) -> bool:
        with self._lock:
            return len(self._items) >= self._maxsize

    def is_empty(self) -> bool:
        with self._lock:
            return not self._items

    # ------------------------------------------------------------------
    # Producer / consumer
    # ------------------------------------------------------------------

    def put(self, item: T) -> bool:
        """Add ``item``. Returns True if accepted, False if dropped."""
        with self._lock:
            if len(self._items) < self._maxsize:
                self._items.append(item)
                return True
            if self._policy == DropPolicy.DROP_OLDEST:
                self._items.popleft()
                self._items.append(item)
                self._dropped_oldest += 1
                return True
            # DROP_NEWEST
            self._dropped_newest += 1
            return False

    def get(self) -> T:
        """Remove and return the oldest item. Raises ``IndexError`` if empty."""
        with self._lock:
            return self._items.popleft()

    def get_nowait_or(self, default: T) -> T:
        """Non-raising alternative to :meth:`get`."""
        with self._lock:
            return self._items.popleft() if self._items else default

    def drain(self) -> list[T]:
        """Remove and return all queued items (oldest-first)."""
        with self._lock:
            out = list(self._items)
            self._items.clear()
            return out

    def snapshot(self) -> list[T]:
        """Return a copy of the current contents without removing them."""
        with self._lock:
            return list(self._items)

    # ------------------------------------------------------------------
    # Iteration
    # ------------------------------------------------------------------

    def __iter__(self) -> Iterator[T]:
        # Snapshot iteration so a consumer can walk the buffer without
        # blocking concurrent producers. Mutations after the snapshot
        # are not visible.
        return iter(self.snapshot())

    # ------------------------------------------------------------------
    # Reset (mostly for tests + admin)
    # ------------------------------------------------------------------

    def reset_drop_counters(self) -> None:
        with self._lock:
            self._dropped_oldest = 0
            self._dropped_newest = 0
