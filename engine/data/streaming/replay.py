"""Bounded replay log — recent-history catch-up for new subscribers (gh#133).

A producer can ``record`` every message; a new consumer that joins
mid-stream can call ``since_seq`` to fetch the messages it missed,
bounded by the configured retention.

This is **not** a durable log. Restarts lose history; messages
beyond the retention window are forgotten silently. For durable
event sourcing use the database directly.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class _Entry(Generic[T]):
    seq: int
    payload: T


class ReplayLog(Generic[T]):
    """Bounded recent-history log keyed by a monotonic sequence number."""

    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._capacity = capacity
        self._items: deque[_Entry[T]] = deque(maxlen=capacity)
        self._next_seq = 0
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def next_seq(self) -> int:
        with self._lock:
            return self._next_seq

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def record(self, payload: T) -> int:
        """Append ``payload`` and return the assigned sequence number."""
        with self._lock:
            seq = self._next_seq
            self._items.append(_Entry(seq=seq, payload=payload))
            self._next_seq += 1
            return seq

    def since_seq(self, after: int) -> list[T]:
        """Return all payloads strictly after sequence number ``after``.

        If ``after`` is older than the retained window, the caller
        gets the oldest retained payloads onwards — the resubscribe
        is best-effort and silently truncated. Caller can detect
        this via :meth:`oldest_seq` and decide whether to do a full
        snapshot fetch.
        """
        with self._lock:
            return [e.payload for e in self._items if e.seq > after]

    def oldest_seq(self) -> int | None:
        """Lowest sequence number still retained, or ``None`` if empty."""
        with self._lock:
            return self._items[0].seq if self._items else None

    def latest(self, n: int = 1) -> list[T]:
        """Return the most recent ``n`` payloads (oldest-first)."""
        if n <= 0:
            return []
        with self._lock:
            length = len(self._items)
            start = max(length - n, 0)
            return [self._items[i].payload for i in range(start, length)]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            # Keep next_seq monotonic so consumers' cursors don't
            # collide with reused sequence numbers after a reset.
