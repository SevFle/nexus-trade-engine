from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger()


# Maximum length, in characters, of a single sanitized role value emitted
# in log records. Anything beyond this is truncated to bound log size and
# to defeat log-injection via arbitrarily long role names.
_SANITIZED_ROLE_MAX_LENGTH = 128

# Pattern matching ASCII control characters (0x00-0x1F and 0x7F) plus the
# C1 control range (0x80-0x9F). These are stripped before logging to
# prevent log-injection / terminal escape-sequence attacks.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def sanitize_role(role: str) -> str:
    """Sanitize a single external role string before it is logged.

    This performs two transformations:

    1. **Strip control characters.** ASCII control characters (including
       ``\\r``, ``\\n``, ``\\t``, ``BEL``, terminal escape introducers,
       and the C1 control range) are removed. Without this, a malicious
       or misconfigured upstream IdP could embed line breaks / escape
       sequences in role names and forge log lines or affect terminals
       rendering the log stream (CWE-117 / CWE-93).

    2. **Cap length.** The result is truncated to
       :data:`_SANITIZED_ROLE_MAX_LENGTH` characters. This bounds the
       size of any single log payload and defeats trivial
       log-blowing-by-INPUT attacks where an attacker submits a
       multi-megabyte role name.

    The original ``role`` value is never used as-is in log output; the
    sanitized form should always be used for the ``unrecognized`` list
    in :meth:`IAuthProvider.map_roles`.
    """
    if not isinstance(role, str):
        # Defensive: callers should pass strings, but coerce anything
        # else via repr to avoid crashing the auth path on bad input.
        role = str(role)
    stripped = _CONTROL_CHARS_RE.sub("", role)
    if len(stripped) > _SANITIZED_ROLE_MAX_LENGTH:
        return stripped[:_SANITIZED_ROLE_MAX_LENGTH]
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

        Security notes:

        * **No implicit promotion** of unrecognized roles. Upstream
          Identity-Provider (IdP) roles are reflected faithfully: only
          roles that are explicitly listed in ``role_priority`` are
          eligible to become the user's role; anything else is dropped
          and a warning is emitted so operators can detect
          misconfigurations. Previously ``viewer`` was silently promoted
          to ``user`` and ``quant_dev`` to ``developer``, which
          constituted a silent privilege escalation (SEV-741).

        * **Least-privilege fallback.** When no recognized role is
          present (empty input, all-unrecognized, or whitespace-only
          inputs), the function falls back to ``viewer`` — the lowest
          privileged internal role. Previously the fallback was
          ``user`` which grants trading privileges by default; an
          unrecognized role claim should not silently confer trading
          capabilities (defense-in-depth, SEV-741 follow-up).
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
                unrecognized.append(role)
        # Broaden the warning: fire on ANY unrecognized external role, not
        # only when the entire set is unrecognized. This surfaces partial
        # misconfigurations (e.g. one stale group name alongside valid ones).
        if unrecognized:
            logger.warning(
                "auth.map_roles.unrecognized_roles",
                provider=self.name,
                unrecognized=[sanitize_role(r) for r in unrecognized],
                recognized=recognized,
                mapped=best if best is not None else "viewer",
            )
        return best if best is not None else "viewer"
