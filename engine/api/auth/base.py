from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger()


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


# ---------------------------------------------------------------------------
# Role-priority table
# ---------------------------------------------------------------------------
#
# ``_ROLE_PRIORITY`` is the single source of truth for the internal role
# hierarchy used by :meth:`IAuthProvider.map_roles`. The keys **must** be
# lowercase because :meth:`map_roles` normalizes incoming IdP role strings
# via ``str.lower().strip()`` before lookup; mixing cases here would
# silently break that normalization. The module-level ``assert`` below
# enforces this invariant at import time so a future contributor adding
# a role cannot accidentally introduce a key that would never match.
#
# ``viewer`` is the lowest-privilege role and is also used as the
# "floor" (default fallback) when no recognized role is supplied —
# returning the lowest possible privilege is the safer default than
# the historical ``user`` fallback, which granted more capability than
# appropriate for an unrecognized identity.
_ROLE_PRIORITY: dict[str, int] = {
    "viewer": 0,
    "user": 1,
    "retail_trader": 2,
    "quant_dev": 3,
    "developer": 4,
    "portfolio_manager": 5,
    "admin": 6,
}
# Future-proofing guard: every key in the priority table must be
# lowercase. Without this, map_roles' lower().strip() normalization
# would silently fail to match any upper-case entry, causing every
# caller to fall through to the ``viewer`` floor and masking the bug.
assert all(k == k.lower() for k in _ROLE_PRIORITY), (
    "_ROLE_PRIORITY keys must be lowercase; map_roles normalizes "
    "incoming roles via str.lower() and would never match an "
    "upper-/mixed-case key."
)
# ``_ROLE_FLOOR`` is the role returned when no recognized role is
# supplied. It is intentionally the lowest-privilege entry in
# ``_ROLE_PRIORITY``.
_ROLE_FLOOR: str = min(_ROLE_PRIORITY, key=_ROLE_PRIORITY.get)  # type: ignore[arg-type]
assert _ROLE_PRIORITY[_ROLE_FLOOR] == 0, (
    "_ROLE_FLOOR must be the lowest-privilege role (priority 0)."
)


@dataclass(frozen=True)
class RoleMappingResult:
    """Structured result of :meth:`IAuthProvider.map_roles_detailed`.

    Callers that need to make policy decisions based on which upstream
    roles were unrecognized (e.g. alerting, audit logging, access
    denial on partial misconfiguration) should use
    :meth:`map_roles_detailed` instead of :meth:`map_roles`.

    Attributes:
        role: The highest-priority recognized role from
            ``external_roles``. If no role was recognized this is
            ``"viewer"`` — the lowest-privilege floor.
        recognized: Every recognized role that appeared in the input,
            normalized to lowercase, in the order it was supplied.
        unrecognized: Every unrecognized raw role string that appeared
            in the input, in the order it was supplied. Useful for
            surfacing misconfigurations to operators.
    """

    role: str
    recognized: list[str]
    unrecognized: list[str]


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
        """Map an external IdP role list to a single internal role.

        Returns the highest-priority **recognized** role as-is, with no
        implicit promotion. If no role in ``external_roles`` is
        recognized (including the empty-list case) the function
        returns ``"viewer"`` — the lowest-privilege floor — rather
        than ``"user"`` (the historical default, which granted more
        capability than appropriate for an unrecognized identity).

        Security note: this function performs **no implicit promotion**
        of unrecognized roles. Upstream Identity-Provider (IdP) roles
        are reflected faithfully: only roles that are explicitly
        listed in :data:`_ROLE_PRIORITY` are eligible to become the
        user's role; anything else is dropped and a warning is emitted
        so operators can detect misconfigurations. Previously
        ``viewer`` was silently promoted to ``user`` and ``quant_dev``
        to ``developer``, which constituted a silent privilege
        escalation (SEV-741).

        Callers that need visibility into which roles were
        unrecognized should call :meth:`map_roles_detailed` instead.
        """
        return self.map_roles_detailed(external_roles).role

    def map_roles_detailed(self, external_roles: list[str]) -> RoleMappingResult:
        """Structured variant of :meth:`map_roles`.

        Returns a :class:`RoleMappingResult` containing the mapped
        role plus the full lists of recognized and unrecognized input
        roles, so callers can make policy decisions (e.g. deny login
        when the unrecognized set is non-empty, audit-log the raw IdP
        group names, alert on drift, etc.).

        See :meth:`map_roles` for normalization and security
        semantics; both methods share the same implementation.
        """
        recognized: list[str] = []
        unrecognized: list[str] = []
        best: str | None = None
        for role in external_roles:
            normalized = role.lower().strip()
            if normalized in _ROLE_PRIORITY:
                recognized.append(normalized)
                if best is None or _ROLE_PRIORITY[normalized] > _ROLE_PRIORITY[best]:
                    best = normalized
            else:
                unrecognized.append(role)
        # Broaden the warning: fire on ANY unrecognized external role,
        # not only when the entire set is unrecognized. This surfaces
        # partial misconfigurations (e.g. one stale group name
        # alongside valid ones).
        if unrecognized:
            logger.warning(
                "auth.map_roles.unrecognized_roles",
                provider=self.name,
                unrecognized=unrecognized,
                recognized=recognized,
                mapped=best if best is not None else _ROLE_FLOOR,
            )
        return RoleMappingResult(
            role=best if best is not None else _ROLE_FLOOR,
            recognized=recognized,
            unrecognized=unrecognized,
        )
