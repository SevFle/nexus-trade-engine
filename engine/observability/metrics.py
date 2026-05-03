"""Pluggable metrics backend (gh#34).

Engine code emits metrics through a thin :class:`MetricsBackend`
Protocol so the operator decides at deploy time which exporter (or
none) to wire up. The default is :class:`NullBackend` — every call is
a no-op — so importing this module does not pull in any monitoring
dependency.

Backend choices:

- :class:`NullBackend` — production default until an exporter is
  configured. Zero-cost.
- :class:`RecordingBackend` — in-memory test double. Records counters,
  gauges, and histograms with their tag tuples so unit tests can assert
  on emitted metrics directly. Never used in production paths.

Future backends (deferred to follow-up PRs once the call sites have
been instrumented):

- ``OTelBackend`` — delegates to ``opentelemetry.metrics`` Meter.
- ``StatsDBackend`` — for installations with a StatsD relay.
- ``PrometheusBackend`` — pull-mode exporter via ``prometheus_client``.

Naming convention: ``namespace.metric_name`` (lowercase, dots between
levels). Tags are arbitrary string -> string. Tag-order is normalised
before aggregation so callers don't have to thread a canonical tag
order through their code.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections import defaultdict
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping


@runtime_checkable
class MetricsBackend(Protocol):
    """Pluggable metrics surface. All methods are fire-and-forget."""

    def counter(
        self,
        name: str,
        value: float = 1.0,
        tags: Mapping[str, str] | None = None,
    ) -> None: ...

    def gauge(
        self,
        name: str,
        value: float,
        tags: Mapping[str, str] | None = None,
    ) -> None: ...

    def histogram(
        self,
        name: str,
        value: float,
        tags: Mapping[str, str] | None = None,
    ) -> None: ...

    def timer(
        self,
        name: str,
        tags: Mapping[str, str] | None = None,
    ) -> contextlib.AbstractContextManager[None]: ...


def _check_name(name: str) -> None:
    if not name or not name.strip():
        raise ValueError("metric name must be non-empty")


def _canonical_tags(tags: Mapping[str, str] | None) -> tuple[tuple[str, str], ...]:
    """Sort tags by key so equivalent tag sets share an aggregation key."""
    if not tags:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in tags.items()))


class NullBackend:
    """Zero-cost no-op backend. Used when no exporter is configured."""

    def counter(
        self,
        name: str,
        value: float = 1.0,
        tags: Mapping[str, str] | None = None,
    ) -> None:
        _check_name(name)

    def gauge(
        self,
        name: str,
        value: float,
        tags: Mapping[str, str] | None = None,
    ) -> None:
        _check_name(name)

    def histogram(
        self,
        name: str,
        value: float,
        tags: Mapping[str, str] | None = None,
    ) -> None:
        _check_name(name)

    @contextlib.contextmanager
    def timer(
        self,
        name: str,
        tags: Mapping[str, str] | None = None,
    ) -> Iterator[None]:
        _check_name(name)
        yield


class RecordingBackend:
    """In-memory test double. Aggregates counters, captures every gauge
    write (last-write-wins), and stores every histogram observation."""

    def __init__(self) -> None:
        self.counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self.gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self.histograms: dict[tuple[str, tuple[tuple[str, str], ...]], list[float]] = defaultdict(
            list
        )
        self._lock = threading.Lock()

    def counter(
        self,
        name: str,
        value: float = 1.0,
        tags: Mapping[str, str] | None = None,
    ) -> None:
        _check_name(name)
        key = (name, _canonical_tags(tags))
        with self._lock:
            self.counters[key] += float(value)

    def gauge(
        self,
        name: str,
        value: float,
        tags: Mapping[str, str] | None = None,
    ) -> None:
        _check_name(name)
        key = (name, _canonical_tags(tags))
        with self._lock:
            self.gauges[key] = float(value)

    def histogram(
        self,
        name: str,
        value: float,
        tags: Mapping[str, str] | None = None,
    ) -> None:
        _check_name(name)
        key = (name, _canonical_tags(tags))
        with self._lock:
            self.histograms[key].append(float(value))

    @contextlib.contextmanager
    def timer(
        self,
        name: str,
        tags: Mapping[str, str] | None = None,
    ) -> Iterator[None]:
        _check_name(name)
        start = time.monotonic()
        try:
            yield
        finally:
            elapsed_ms = (time.monotonic() - start) * 1000.0
            self.histogram(name, elapsed_ms, tags)


# ---------------------------------------------------------------------------
# Process-singleton getter / setter.
# ---------------------------------------------------------------------------

_BACKEND: MetricsBackend = NullBackend()
_BACKEND_LOCK = threading.Lock()


def get_metrics() -> MetricsBackend:
    """Return the active metrics backend."""
    return _BACKEND


def set_metrics(backend: MetricsBackend) -> None:
    """Install ``backend`` as the active singleton. Wired once at app
    startup based on ``settings.metrics_backend`` (when that's added);
    tests use this to swap in a :class:`RecordingBackend`."""
    global _BACKEND  # noqa: PLW0603 - process-wide singleton
    with _BACKEND_LOCK:
        _BACKEND = backend


__all__ = [
    "MetricsBackend",
    "NullBackend",
    "RecordingBackend",
    "get_metrics",
    "set_metrics",
]
