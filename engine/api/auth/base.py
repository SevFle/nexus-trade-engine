from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger()

# Maximum length, in characters, of a single unrecognized role string that
# we will write to the warning log.  External Identity-Provider (IdP) role
# names should be short group/claim strings; anything longer is almost
# certainly garbage (e.g. an entire JWT pasted as a role) and would risk
# blowing up log aggregation pipelines or hiding more useful context.
_UNRECOGNIZED_ROLE_MAX_LEN: int = 128

# Strip ASCII control characters (0x00-0x1F and 0x7F) plus the Unicode
# "other" control category.  This prevents log-injection / terminal escape
# sequence attacks when a misconfigured IdP pushes e.g. ANSI escape codes
# or newline characters inside a role claim.
_CONTROL_CHAR_RE: re.Pattern[str] = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u2028\u2029]"
)


def _sanitize_role_for_log(role: Any) -> str:
    """Return a safe string representation of an unrecognized role for
    inclusion in log payloads.

    * Non-string inputs are stringified via ``str()``.
    * ASCII / Unicode control characters are stripped.
    * The result is truncated to ``_UNRECOGNIZED_ROLE_MAX_LEN`` characters
      to bound log line length.
    """
    text = role if isinstance(role, str) else str(role)
    text = _CONTROL_CHAR_RE.sub("", text).strip()
    if len(text) > _UNRECOGNIZED_ROLE_MAX_LEN:
        text = text[:_UNRECOGNIZED_ROLE_MAX_LEN]
    return text


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

        Defense-in-depth: when *no* recognized role is present (empty list
        or only-unrecognized input), we return ``"viewer"`` — the
        **lowest-privilege** internal role — rather than ``"user"``.  This
        prevents a misconfigured IdP that emits no role claim (or only
        garbage claims) from accidentally granting ordinary-user rights.
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
                unrecognized.append(role)
        # Broaden the warning: fire on ANY unrecognized external role, not
        # only when the entire set is unrecognized. This surfaces partial
        # misconfigurations (e.g. one stale group name alongside valid ones).
        if unrecognized:
            # Sanitize before logging: strip control characters and cap
            # length so a misbehaving IdP cannot break log pipelines or
            # perform log-injection via crafted role names.
            sanitized_unrecognized = [
                _sanitize_role_for_log(r) for r in unrecognized
            ]
            fallback_role = best if best is not None else "viewer"
            logger.warning(
                "auth.map_roles.unrecognized_roles",
                provider=self.name,
                unrecognized=sanitized_unrecognized,
                recognized=recognized,
                mapped=fallback_role,
            )
        return best if best is not None else "viewer"
