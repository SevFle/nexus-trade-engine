from __future__ import annotations

import re
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class UserInfo:
    external_id: str | None = None
    email: str = ""
    display_name: str = ""
    provider: str = "local"
    roles: list[str] = field(default_factory=lambda: ["user"])
    raw_claims: dict[str, Any] = field(default_factory=dict)


@dataclass
class AuthResult:
    success: bool = False
    user_info: UserInfo | None = None
    error: str | None = None


# External-to-internal role aliases.
#
# External identity providers (OIDC, LDAP, etc.) sometimes emit role names
# that overlap with names in our internal hierarchy but carry a different
# privilege intent. For example, Azure AD's default "viewer" group is closer
# to our internal ``user`` than to our (unused-but-reserved) ``viewer`` role.
# This map is applied *before* the priority comparison so the priority dict
# represents a pure internal-only hierarchy.
#
# Every value here MUST be a key in :data:`_ROLE_PRIORITY` and MUST have a
# strictly higher priority than its key (upward-only mapping). The
# ``test_external_aliases_only_promote_upward`` test enforces that invariant.
_EXTERNAL_ROLE_ALIASES: dict[str, str] = {
    "viewer": "user",
    "quant_dev": "developer",
}


# Internal role hierarchy. Lower number = lower privilege.
_ROLE_PRIORITY: dict[str, int] = {
    "viewer": 0,
    "user": 1,
    "retail_trader": 2,
    "quant_dev": 3,
    "developer": 4,
    "portfolio_manager": 5,
    "admin": 6,
}


# Roles that an untrusted upstream Identity Provider (IdP) may legitimately
# assert for a single role string. This is the *strict* allowlist used by
# :func:`_sanitize_role`; anything outside it collapses to ``"user"``.
ALLOWED_ROLES: frozenset[str] = frozenset(
    {"user", "viewer", "developer", "portfolio_manager", "admin"}
)

# Role names are short ASCII identifiers. Anything longer is either garbage
# or a hostile payload; reject it *before* running any regex so a huge input
# cannot trigger pathological backtracking (regex DoS hardening).
_MAX_ROLE_LENGTH = 128

# C0 (\x00-\x1f) and C1 (\x7f-\x9f) control characters. Stripped so they
# cannot smuggle a role past a naive ``in`` check (e.g. ``"admin\x00"``).
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _sanitize_role(role: str | None) -> str:
    """Validate a single role string received from an untrusted source
    (an upstream Identity Provider) against :data:`ALLOWED_ROLES`.

    The pipeline is ordered for both safety and DoS resistance:

    1. **Type / length guard** -- non-strings and oversize inputs collapse to
       ``"user"`` immediately, *before* any regex work, defeating regex-based
       denial of service.
    2. **NFKC normalization** -- canonicalizes Unicode so look-alike sequences
       become detectable.
    3. **Control-character strip** -- removes C0/C1 control codes.
    4. **Spoofing guard** -- if normalization or stripping altered the input
       it was a look-alike spoof (e.g. a fullwidth-Unicode ``admin`` that NFKC
       would otherwise turn into the real ``admin``); collapse to
       ``"user"`` rather than accept it.
    5. **Allowlist membership** -- only the exact strings in
       :data:`ALLOWED_ROLES` survive; everything else collapses to ``"user"``.

    This guarantees a hostile or misconfigured IdP cannot inject an arbitrary
    or look-alike role (e.g. a spoofed ``admin``) into a local user account.
    """
    if not isinstance(role, str) or len(role) > _MAX_ROLE_LENGTH:
        return "user"
    normalized = unicodedata.normalize("NFKC", role)
    cleaned = _CONTROL_CHARS_RE.sub("", normalized)
    # Anti-spoofing: a legitimate role name is already in canonical form.
    # If NFKC or control-char stripping changed the value, the caller sent a
    # look-alike (e.g. fullwidth-Unicode "admin") -- never trust it with
    # elevated privileges.
    if cleaned != role:
        return "user"
    if cleaned in ALLOWED_ROLES:
        return cleaned
    return "user"


class IAuthProvider(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    async def authenticate(self, **kwargs: Any) -> AuthResult: ...

    async def get_user_info(self, _external_id: str) -> UserInfo | None:
        return None

    async def create_user(self, _user_info: UserInfo, **_kwargs: Any) -> AuthResult:
        return AuthResult(success=False, error=f"User creation not supported by {self.name}")

    def map_roles(self, external_roles: list[str]) -> str:
        """Reduce a list of IdP-supplied role names to a single internal role.

        Two-phase resolution keeps external naming concerns decoupled from
        the internal privilege hierarchy:

        1. **Alias resolution** — each input role is lowercased, stripped, and
           translated through :data:`_EXTERNAL_ROLE_ALIASES` to its canonical
           internal name. Unrecognized names are dropped.
        2. **Hierarchy selection** — the highest-priority canonical name wins.
           When the input contains no recognized role, ``"user"`` is returned
           as a safe default.
        """
        best = "user"
        best_priority = _ROLE_PRIORITY[best]
        for role in external_roles:
            normalized = role.lower().strip()
            canonical = _EXTERNAL_ROLE_ALIASES.get(normalized, normalized)
            priority = _ROLE_PRIORITY.get(canonical)
            if priority is None:
                continue
            if priority > best_priority:
                best = canonical
                best_priority = priority
        return best
