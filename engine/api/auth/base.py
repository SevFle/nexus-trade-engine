from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger()


# Log-injection defense (SEV-508).  External role strings surface in
# structlog payloads via ``map_roles``; an upstream IdP that asserts a
# role containing newlines, ANSI escapes, or other control characters
# could otherwise forge fake log lines / fake terminal output when
# operators view aggregated logs.  ``_sanitize_for_log`` neutralizes
# every C0 control character (and DEL): whitespace-like controls
# (``\n``, ``\r``, ``\t``) are replaced with a single ASCII space so
# that adjacent words are not jammed together; all other controls are
# stripped outright.  Internal whitespace runs are then collapsed and
# the result is truncated to defeat log-flooding via pathologically
# long role names.
_LOG_NEWLINE_TO_SPACE = re.compile(r"[\r\n\t]")
_LOG_OTHER_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_LOG_WHITESPACE = re.compile(r"\s+")
_LOG_MAX_LENGTH = 128


def _sanitize_for_log(value: str) -> str:
    """Return a log-safe representation of *value*.

    - Replaces ``\\n``, ``\\r``, ``\\t`` with a single space (so word
      boundaries survive sanitization).
    - Strips every other C0 control character and DEL.
    - Collapses internal whitespace runs to a single space.
    - Truncates overly long strings to bound log line size.
    """
    if value is None:
        return ""
    cleaned = _LOG_NEWLINE_TO_SPACE.sub(" ", str(value))
    cleaned = _LOG_OTHER_CONTROL.sub("", cleaned)
    cleaned = _LOG_WHITESPACE.sub(" ", cleaned).strip()
    if len(cleaned) > _LOG_MAX_LENGTH:
        cleaned = cleaned[:_LOG_MAX_LENGTH].rstrip() + "..."
    return cleaned


def _sanitize_role_list(roles: list[str]) -> list[str]:
    """Apply :func:`_sanitize_for_log` to every entry of *roles*.

    Returns a new list — the caller's list is not mutated.
    """
    return [_sanitize_for_log(r) for r in roles]


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
            # Sanitize every role string before it crosses into the
            # log subsystem to neutralize log-injection via crafted
            # upstream IdP role claims (SEV-508).
            logger.warning(
                "auth.map_roles.unrecognized_roles",
                provider=self.name,
                unrecognized=_sanitize_role_list(unrecognized),
                recognized=_sanitize_role_list(recognized),
                mapped=_sanitize_for_log(best if best is not None else "user"),
            )
        return best if best is not None else "user"
