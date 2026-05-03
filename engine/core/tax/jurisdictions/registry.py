"""Tax-jurisdiction registry (gh#81).

Operators register a :class:`TaxJurisdiction` implementation under a
stable string key (typically the ISO code) and look one up at runtime
via configuration. Replaces hard-coded references to a single
jurisdiction throughout the engine.
"""

from __future__ import annotations

import threading

from engine.core.tax.jurisdictions.base import TaxJurisdiction

_REGISTRY: dict[str, TaxJurisdiction] = {}
_LOCK = threading.Lock()


def register_jurisdiction(jurisdiction: TaxJurisdiction) -> None:
    """Register ``jurisdiction`` under its own ``code``.

    Re-registering an existing code overwrites the prior entry. That
    is intentional — operators may want to tweak a built-in
    jurisdiction at startup without subclassing.
    """
    if not isinstance(jurisdiction, TaxJurisdiction):
        raise TypeError(
            f"argument must implement TaxJurisdiction Protocol (got {type(jurisdiction).__name__})"
        )
    code = jurisdiction.code
    if not code or not code.strip():
        raise ValueError("jurisdiction code must be non-empty")
    with _LOCK:
        _REGISTRY[code] = jurisdiction


def get_jurisdiction(code: str) -> TaxJurisdiction:
    """Look up a jurisdiction by code. Raises :class:`KeyError` if absent."""
    with _LOCK:
        try:
            return _REGISTRY[code]
        except KeyError as exc:
            raise KeyError(
                f"unknown tax jurisdiction: {code!r}. registered: {sorted(_REGISTRY.keys())}"
            ) from exc


def list_jurisdictions() -> list[str]:
    """Return registered jurisdiction codes, sorted."""
    with _LOCK:
        return sorted(_REGISTRY.keys())


def _reset_for_tests() -> None:
    """Test-only: clear the registry so each test starts fresh."""
    with _LOCK:
        _REGISTRY.clear()
