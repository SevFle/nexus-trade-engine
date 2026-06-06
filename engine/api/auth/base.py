from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger()

# Maximum length, in characters, accepted for a single external role
# string. Anything longer is truncated before being logged or compared
# against the recognized-role list. This bounds log-line size and
# prevents pathological IdP payloads from polluting telemetry.
_MAX_ROLE_LENGTH: int = 64

# Regex matching ASCII control characters (C0 + DEL). We strip these
# from external role strings before any further processing so that
# log-injection / terminal-escape attacks cannot be smuggled through
# the unrecognized-role warning payload.
_CONTROL_CHARS_RE: re.Pattern[str] = re.compile(r"[\x00-\x1f\x7f]")


def _sanitize_role(role: str) -> str:
    """Strip control characters and cap length on a raw external role.

    External role strings come from upstream Identity Providers (IdP)
    and may contain:

    * ASCII control characters (newlines, tabs, terminal escapes,
      NULs) that, if echoed back into a log line, can be used for
      log forging / terminal escape attacks.
    * Arbitrarily long blobs that, if recorded verbatim, can blow up
      log aggregators and alert deduplicators.

    This helper normalizes a single raw role string before it is
    appended to the ``unrecognized`` list and emitted in the warning
    payload. The transformation is purely cosmetic — it does **not**
    affect which role is ultimately mapped.

    Args:
        role: A raw role string as received from the IdP.

    Returns:
        A string of at most :data:`_MAX_ROLE_LENGTH` characters with
        all ASCII control characters removed. Empty results (e.g.
        when the input is empty or contained only control chars)
        are returned as the empty string.
    """
    if not isinstance(role, str):
        return ""
    stripped = _CONTROL_CHARS_RE.sub("", role)
    if len(stripped) > _MAX_ROLE_LENGTH:
        return stripped[:_MAX_ROLE_LENGTH]
    return stripped


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

    def map_roles(self, external_roles: list[str]) -> str:
        """Map an external IdP role list to a single internal role.

        Security note: this function performs **no implicit promotion** of
        unrecognized roles. Upstream Identity-Provider (IdP) roles are
        reflected faithfully: only roles that are explicitly listed in
        ``role_priority`` are eligible to become the user's role; anything
        else is dropped and a warning is emitted so operators can detect
        misconfigurations. Previously ``viewer`` was silently promoted to
        ``user`` and ``quant_dev`` to ``developer``, which constituted a
        silent privilege escalation (SEV-741).

        When **no** recognized role is found in the input (either the
        list is empty, or every entry is unrecognized) the function
        falls back to the least-privileged internal role: ``viewer``.
        A dedicated ``auth.map_roles.fallback_to_viewer`` warning is
        emitted whenever this fallback fires, so operators can alert
        on the silent privilege floor being applied.
        """
        role_priority: dict[str, int] = {
            "viewer": 0,
            "user": 1,
            "retail_trader": 2,
            "quant_dev": 3,
            "developer": 4,
            "portfolio_manager": 5,
            "admin": 6,
        }
        recognized: list[str] = []
        unrecognized: list[str] = []
        best: str | None = None
        for role in external_roles:
            normalized = role.lower().strip() if isinstance(role, str) else ""
            if normalized in role_priority:
                recognized.append(normalized)
                if best is None or role_priority[normalized] > role_priority[best]:
                    best = normalized
            else:
                unrecognized.append(_sanitize_role(role))
        # Broaden the warning: fire on ANY unrecognized external role, not
        # only when the entire set is unrecognized. This surfaces partial
        # misconfigurations (e.g. one stale group name alongside valid ones).
        if unrecognized:
            logger.warning(
                "auth.map_roles.unrecognized_roles",
                provider=self.name,
                unrecognized=unrecognized,
                recognized=recognized,
                mapped=best if best is not None else "viewer",
            )
        # Separate, dedicated event when the least-privilege fallback
        # actually fires. Operators alert on this to detect upstream
        # IdP misconfigurations that yield no usable role claim.
        if best is None:
            logger.warning(
                "auth.map_roles.fallback_to_viewer",
                provider=self.name,
                external_roles=[
                    _sanitize_role(r) for r in external_roles if isinstance(r, str)
                ],
            )
            return "viewer"
        return best
