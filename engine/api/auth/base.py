from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

import structlog

logger = structlog.get_logger()

# Lowest-privilege role used as the fallback when no recognized role can
# be derived from the upstream IdP assertion. Centralizing the literal
# here makes it easy to audit and avoids drift between call sites.
LOWEST_PRIVILEGE_ROLE: str = "viewer"


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
    # Free-form metadata bag. Used today to surface unrecognized upstream
    # IdP roles so that callers / auditors can detect misconfigurations
    # without scraping log lines (medium-severity follow-up to SEV-741).
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RoleMappingResult:
    """Detailed outcome of mapping an external IdP role list.

    ``role`` is the single internal role to assign (always one of the
    keys in ``IAuthProvider.ROLE_PRIORITY`` — falling back to
    :data:`LOWEST_PRIVILEGE_ROLE` when nothing recognized is present).
    ``recognized`` and ``unrecognized`` carry the raw partitioning of
    the input for audit / metadata purposes.
    """

    role: str
    recognized: list[str] = field(default_factory=list)
    unrecognized: list[str] = field(default_factory=list)


class IAuthProvider(ABC):
    # Public so tests / callers can introspect the recognized set without
    # re-implementing the priority table.
    ROLE_PRIORITY: ClassVar[dict[str, int]] = {
        "viewer": 0,
        "user": 1,
        "retail_trader": 2,
        "quant_dev": 3,
        "developer": 4,
        "portfolio_manager": 5,
        "admin": 6,
    }

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    async def authenticate(self, **kwargs: Any) -> AuthResult: ...

    async def get_user_info(self, _external_id: str) -> UserInfo | None:
        return None

    async def create_user(self, _user_info: UserInfo, **_kwargs: Any) -> AuthResult:
        return AuthResult(success=False, error=f"User creation not supported by {self.name}")

    def map_roles_with_metadata(
        self, external_roles: list[str]
    ) -> RoleMappingResult:
        """Map an external IdP role list to a :class:`RoleMappingResult`.

        Security note: this function performs **no implicit promotion** of
        unrecognized roles. Upstream Identity-Provider (IdP) roles are
        reflected faithfully: only roles that are explicitly listed in
        :attr:`ROLE_PRIORITY` are eligible to become the user's role; anything
        else is dropped and a warning is emitted so operators can detect
        misconfigurations. Previously ``viewer`` was silently promoted to
        ``user`` and ``quant_dev`` to ``developer``, which constituted a
        silent privilege escalation (SEV-741).

        Empty input — including the case where every entry is filtered out
        as unrecognized — falls back to :data:`LOWEST_PRIVILEGE_ROLE`
        (``"viewer"``) rather than ``"user"``, adhering to a least-privilege
        default. The partitioning (``recognized`` / ``unrecognized``) is
        returned alongside the chosen role so callers can attach it to
        :attr:`AuthResult.metadata` for auditability.
        """
        role_priority = self.ROLE_PRIORITY
        recognized: list[str] = []
        unrecognized: list[str] = []
        best: str | None = None
        for role in external_roles:
            normalized = role.lower().strip()
            if normalized in role_priority:
                recognized.append(normalized)
                if best is None or role_priority[normalized] > role_priority[best]:
                    best = normalized
            else:
                unrecognized.append(role)

        # Explicit empty/empty-after-filtering handling: when nothing
        # recognized survives, assign the lowest-privilege role rather
        # than implicitly granting "user".
        mapped = LOWEST_PRIVILEGE_ROLE if best is None else best

        # Broaden the warning: fire on ANY unrecognized external role, not
        # only when the entire set is unrecognized. This surfaces partial
        # misconfigurations (e.g. one stale group name alongside valid ones).
        if unrecognized:
            logger.warning(
                "auth.map_roles.unrecognized_roles",
                provider=self.name,
                unrecognized=unrecognized,
                recognized=recognized,
                mapped=mapped,
            )
        return RoleMappingResult(
            role=mapped,
            recognized=recognized,
            unrecognized=unrecognized,
        )

    def map_roles(self, external_roles: list[str]) -> str:
        """Map an external IdP role list to a single internal role.

        Convenience wrapper around :meth:`map_roles_with_metadata` that
        returns only the chosen role string. Returns
        :data:`LOWEST_PRIVILEGE_ROLE` (``"viewer"``) when the input is
        empty or contains no recognized roles — see the security note on
        :meth:`map_roles_with_metadata`.
        """
        return self.map_roles_with_metadata(external_roles).role
