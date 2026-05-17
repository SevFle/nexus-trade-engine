from __future__ import annotations

from enum import Enum
from typing import Any


class TrustLevel(Enum):
    TRUSTED_FULL = "trusted_full"
    TRUSTED_LIMITED = "trusted_limited"
    UNTRUSTED = "untrusted"


_CAPABILITY_SETS: dict[TrustLevel, frozenset[str]] = {
    TrustLevel.TRUSTED_FULL: frozenset({
        "network",
        "filesystem_read",
        "filesystem_write",
        "threads",
        "subprocess",
        "environment",
        "dynamic_import",
        "introspection_basic",
    }),
    TrustLevel.TRUSTED_LIMITED: frozenset({
        "network",
        "filesystem_read",
        "filesystem_write",
        "threads",
        "introspection_basic",
    }),
    TrustLevel.UNTRUSTED: frozenset({
        "network",
        "filesystem_read",
    }),
}

_TRUST_POLICIES: dict[TrustLevel, dict[str, Any]] = {
    TrustLevel.TRUSTED_FULL: {
        "import_restriction": "relaxed",
        "network": "manifest_only",
        "resource_multiplier": 4.0,
        "filesystem": "workspace",
        "introspection": "basic",
        "allowed_capabilities": _CAPABILITY_SETS[TrustLevel.TRUSTED_FULL],
    },
    TrustLevel.TRUSTED_LIMITED: {
        "import_restriction": "standard",
        "network": "manifest_only",
        "resource_multiplier": 2.0,
        "filesystem": "isolated_rw",
        "introspection": "standard",
        "allowed_capabilities": _CAPABILITY_SETS[TrustLevel.TRUSTED_LIMITED],
    },
    TrustLevel.UNTRUSTED: {
        "import_restriction": "strict",
        "network": "manifest_only",
        "resource_multiplier": 1.0,
        "filesystem": "isolated_ro",
        "introspection": "strict",
        "allowed_capabilities": _CAPABILITY_SETS[TrustLevel.UNTRUSTED],
    },
}


def get_trust_level(manifest: Any) -> TrustLevel:
    trust_str = getattr(manifest, "trust_level", None) or "untrusted"
    try:
        return TrustLevel(trust_str)
    except ValueError:
        return TrustLevel.UNTRUSTED


def get_trust_policy(trust_level: TrustLevel) -> dict[str, Any]:
    return _TRUST_POLICIES.get(trust_level, _TRUST_POLICIES[TrustLevel.UNTRUSTED])


def get_allowed_capabilities(trust_level: TrustLevel) -> frozenset[str]:
    return _CAPABILITY_SETS.get(trust_level, _CAPABILITY_SETS[TrustLevel.UNTRUSTED])


def validate_capabilities(trust_level: TrustLevel, required: set[str]) -> bool:
    allowed = get_allowed_capabilities(trust_level)
    return required.issubset(allowed)


def enforce_no_escalation(current: TrustLevel, requested: TrustLevel) -> bool:
    _ORDER = {
        TrustLevel.UNTRUSTED: 0,
        TrustLevel.TRUSTED_LIMITED: 1,
        TrustLevel.TRUSTED_FULL: 2,
    }
    return _ORDER.get(requested, 0) <= _ORDER.get(current, 0)
