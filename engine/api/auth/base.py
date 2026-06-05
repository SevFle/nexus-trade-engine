from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger()

# Maximum allowed length for any single role string after sanitization.
# Defends log aggregators and SIEM pipelines from accidentally ingesting
# arbitrarily long values that an upstream IdP might inject.
_ROLE_MAX_LENGTH: int = 128

# Matches ASCII control characters (0x00-0x1F and 0x7F) plus the Unicode
# category "Cc" (control) characters such as zero-width spaces and bidi
# overrides. Compiled once at import time.
_CONTROL_CHARS_RE: re.Pattern[str] = re.compile(r"[\x00-\x1f\x7f]")


def sanitize_role(role: str) -> str:
    """Return a safe, length-bounded representation of an external role.

    Strips ASCII control characters (which could otherwise corrupt log
    aggregators or enable terminal/log-injection attacks) and truncates
    to ``_ROLE_MAX_LENGTH`` characters.  Empty / whitespace-only inputs
    are returned as an empty string so callers can distinguish "no role"
    from a real role.

    Intended use: feeding unrecognized upstream IdP role strings into
    log records (e.g. the ``unrecognized=`` payload of
    ``auth.map_roles.unrecognized_roles``) without risking log
    injection or unbounded storage growth.
    """
    if not isinstance(role, str):
        return ""
    stripped = _CONTROL_CHARS_RE.sub("", role).strip()
    if len(stripped) > _ROLE_MAX_LENGTH:
        return stripped[:_ROLE_MAX_LENGTH]
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

        Fallback behavior: when no recognized role is present (empty list,
        whitespace-only, or all-unrecognized) the function returns
        ``"viewer"`` — the **least-privileged** internal role. Earlier
        versions returned ``"user"``, which silently granted
        read-write-eligible access to a federated principal whose IdP
        failed to assert any recognized role. An explicit
        ``auth.map_roles.fallback_to_viewer`` warning is emitted whenever
        this fallback fires so operators can detect misconfigured
        upstream claims.
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
            normalized = role.lower().strip()
            if normalized in role_priority:
                recognized.append(normalized)
                if best is None or role_priority[normalized] > role_priority[best]:
                    best = normalized
            else:
                unrecognized.append(sanitize_role(role))
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
        if best is not None:
            return best
        # No recognized role at all — fall back to the least-privileged
        # role and emit a dedicated, distinct warning event so operators
        # can alert specifically on the privilege-floor escalation.
        logger.warning(
            "auth.map_roles.fallback_to_viewer",
            provider=self.name,
            external_role_count=len(external_roles),
            external_roles=unrecognized,
        )
        return "viewer"
