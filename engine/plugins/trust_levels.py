from __future__ import annotations

from enum import Enum
from typing import Any


class TrustLevel(Enum):
    TRUSTED_FULL = "trusted_full"
    TRUSTED_LIMITED = "trusted_limited"
    UNTRUSTED = "untrusted"


_TRUST_POLICIES: dict[TrustLevel, dict[str, Any]] = {
    TrustLevel.TRUSTED_FULL: {
        "import_restriction": "relaxed",
        "network": "manifest_only",
        "resource_multiplier": 4.0,
        "filesystem": "workspace",
        "introspection": "basic",
    },
    TrustLevel.TRUSTED_LIMITED: {
        "import_restriction": "standard",
        "network": "manifest_only",
        "resource_multiplier": 2.0,
        "filesystem": "isolated_rw",
        "introspection": "standard",
    },
    TrustLevel.UNTRUSTED: {
        "import_restriction": "strict",
        "network": "manifest_only",
        "resource_multiplier": 1.0,
        "filesystem": "isolated_ro",
        "introspection": "strict",
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
