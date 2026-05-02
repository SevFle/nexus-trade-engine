"""Broker adapter registry (gh#136).

Operators register a :class:`BrokerAdapter` once at startup under a
stable name (typically the broker's slug); the live-trading loop and
operator-facing routes look one up at runtime via configuration.
"""

from __future__ import annotations

import threading

from engine.core.brokers.base import BrokerAdapter

_REGISTRY: dict[str, BrokerAdapter] = {}
_LOCK = threading.Lock()


def register_broker(adapter: BrokerAdapter) -> None:
    """Register ``adapter`` under its own ``name``.

    Re-registering an existing name overwrites the prior entry. That
    is intentional — operators may want to swap adapter
    implementations at startup (e.g., paper vs. live) without
    sub-classing.
    """
    if not isinstance(adapter, BrokerAdapter):
        raise TypeError(
            "argument must implement BrokerAdapter Protocol "
            f"(got {type(adapter).__name__})"
        )
    name = adapter.name
    if not name or not name.strip():
        raise ValueError("broker adapter name must be non-empty")
    if name != name.lower():
        raise ValueError(f"broker adapter name must be lower-case (got {name!r})")
    with _LOCK:
        _REGISTRY[name] = adapter


def get_broker(name: str) -> BrokerAdapter:
    """Look up a broker adapter by name. Raises :class:`KeyError` if absent."""
    with _LOCK:
        try:
            return _REGISTRY[name]
        except KeyError as exc:
            raise KeyError(
                f"unknown broker: {name!r}. "
                f"registered: {sorted(_REGISTRY.keys())}"
            ) from exc


def list_brokers() -> list[str]:
    """Return registered broker names, sorted."""
    with _LOCK:
        return sorted(_REGISTRY.keys())


def _reset_for_tests() -> None:
    """Test-only: clear the registry so each test starts fresh."""
    with _LOCK:
        _REGISTRY.clear()
