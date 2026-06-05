from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Role priority table — single source of truth for the auth subsystem.
#
# Lower number = lower privilege. The ordering must be contiguous integers
# starting at 0 so that ``max()``-by-priority is well-defined and so that
# ``ROLE_PRIORITY[higher] > ROLE_PRIORITY[lower]`` is a stable privilege
# comparison.
# ---------------------------------------------------------------------------
ROLE_PRIORITY: dict[str, int] = {
    "viewer": 0,
    "user": 1,
    "retail_trader": 2,
    "quant_dev": 3,
    "developer": 4,
    "portfolio_manager": 5,
    "admin": 6,
}

# Default role when a principal arrives with no recognized role. Used by
# :meth:`IAuthProvider.map_roles` (the convenience wrapper) — *not* by
# :meth:`IAuthProvider.map_roles_detailed`, which returns ``None`` to
# force callers to handle the denial explicitly.
ROLE_FLOOR: str = "user"

# Set of roles that must always be present. Kept as a module-level
# constant so tests can introspect it.
_REQUIRED_ROLES: frozenset[str] = frozenset(ROLE_PRIORITY.keys())


def _validate_role_table() -> None:
    """Runtime-validate the ROLE_PRIORITY table at import time.

    A module-level ``assert`` would be stripped by ``python -O``,
    silently allowing a misconfigured priority table into production.
    This function instead raises :class:`RuntimeError`, which is
    preserved under optimization. The check is intentionally cheap so
    that calling it at import time has no measurable startup cost.

    Verified invariants
    -------------------
    * ``ROLE_PRIORITY`` is a non-empty ``dict``.
    * Every role in :data:`_REQUIRED_ROLES` is present.
    * Priorities are contiguous integers starting at 0 (so privilege
      ordering is total and ``min``/``max`` are well-defined).
    * No two roles share the same priority.

    Raises
    ------
    RuntimeError
        If any invariant is violated.
    """
    if not isinstance(ROLE_PRIORITY, dict):
        # NOTE: RuntimeError (not TypeError) is intentional — the
        # whole point of this validator is to survive ``python -O``,
        # and TypeError would do the same, but we keep a single
        # exception family for clarity in operator logs.
        raise RuntimeError("ROLE_PRIORITY must be a dict")  # noqa: TRY004

    if not ROLE_PRIORITY:
        raise RuntimeError("ROLE_PRIORITY must not be empty")

    missing = sorted(_REQUIRED_ROLES - set(ROLE_PRIORITY.keys()))
    if missing:
        raise RuntimeError(
            f"ROLE_PRIORITY missing required roles: {missing}"
        )

    priorities = list(ROLE_PRIORITY.values())
    expected = list(range(len(ROLE_PRIORITY)))
    if sorted(priorities) != expected:
        raise RuntimeError(
            f"ROLE_PRIORITY priorities must be contiguous integers "
            f"starting at 0; got {sorted(priorities)} "
            f"(expected {expected}). This fires for both gaps and "
            f"duplicates — sorted priorities must equal [0..n-1]."
        )


_validate_role_table()


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

    def map_roles_detailed(self, external_roles: list[str]) -> str | None:
        """Map an external IdP role list to a single internal role.

        Returns ``None`` when **no** external role is recognized, instead
        of silently falling back to :data:`ROLE_FLOOR`. Returning ``None``
        forces the caller to make an explicit policy decision about how
        to handle the case ("deny login", "keep previous role", "assign
        default") rather than burying that decision in a default. See
        the SEV-741 follow-up.

        Parameters
        ----------
        external_roles:
            Raw role strings as asserted by the upstream Identity
            Provider (IdP). Each entry is independently normalized
            (lower-cased + stripped) before lookup.

        Returns
        -------
        str | None
            The highest-priority **recognized** role, verbatim — no
            implicit promotion (SEV-741). ``None`` if every external
            role was unrecognized (including when ``external_roles`` is
            empty).

        Side effects
        ------------
        Emits ``auth.map_roles.unrecognized_roles`` via structlog for
        **any** unrecognized role — including when recognized roles are
        present alongside — so partial misconfigurations surface in
        operator dashboards.
        """
        recognized: list[str] = []
        unrecognized: list[str] = []
        best: str | None = None
        for role in external_roles:
            normalized = role.lower().strip()
            if normalized in ROLE_PRIORITY:
                recognized.append(normalized)
                if best is None or ROLE_PRIORITY[normalized] > ROLE_PRIORITY[best]:
                    best = normalized
            else:
                unrecognized.append(role)
        # Broaden the warning: fire on ANY unrecognized external role, not
        # only when the entire set is unrecognized. This surfaces partial
        # misconfigurations (e.g. one stale group name alongside valid ones).
        if unrecognized:
            logger.warning(
                "auth.map_roles.unrecognized_roles",
                provider=self.name,
                unrecognized=unrecognized,
                recognized=recognized,
                mapped=best if best is not None else "user",
            )
        return best

    def map_roles(self, external_roles: list[str]) -> str:
        """Map an external IdP role list to a single internal role.

        Backward-compatible convenience wrapper around
        :meth:`map_roles_detailed` that falls back to :data:`ROLE_FLOOR`
        (``"user"``) when no recognized role is found. New callers that
        need to distinguish "no role asserted" from "explicit user"
        should call :meth:`map_roles_detailed` directly.

        Security note: this function performs **no implicit promotion**
        of unrecognized roles. Upstream Identity-Provider (IdP) roles
        are reflected faithfully: only roles that are explicitly listed
        in :data:`ROLE_PRIORITY` are eligible to become the user's role;
        anything else is dropped and a warning is emitted so operators
        can detect misconfigurations. Previously ``viewer`` was silently
        promoted to ``user`` and ``quant_dev`` to ``developer``, which
        constituted a silent privilege escalation (SEV-741).
        """
        return self.map_roles_detailed(external_roles) or ROLE_FLOOR
