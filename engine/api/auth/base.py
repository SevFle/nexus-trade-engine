from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger()

# Least-privilege fallback role used when no external role can be
# recognized. Previously this was ``user``, which granted write access
# to a freshly-created or unrecognized account — a violation of
# least-privilege. ``viewer`` is read-only (SEV-741 follow-up).
DEFAULT_FALLBACK_ROLE: str = "viewer"

# Maximum length of a sanitized role string emitted to logs.  IdP
# role/group payloads are occasionally multi-KB free-form strings
# (e.g. ``memberOf`` DNs).  Capping the length keeps log lines bounded
# and denies log-injection / log-flooding via crafted claims.
_MAX_LOG_ROLE_LENGTH: int = 128

# Pattern matching ASCII control characters (Cx and C0 ranges), used
# to strip CRLF and similar control bytes before logging raw external
# role strings.  Log injection via newline/carriage-return characters
# is a known class of attack against structured loggers.
_CONTROL_CHARS_RE: re.Pattern[str] = re.compile(r"[\x00-\x1f\x7f]")


def sanitize_role_for_log(role: Any) -> str:
    """Sanitize a raw external role string for safe inclusion in log records.

    Performs three transformations:

    1. Coerce non-string input (e.g. bytes, ints) to ``str`` so the
       caller cannot trip up the regex substitution.
    2. Strip ASCII control characters (``\\x00``-``\\x1f``, ``\\x7f``)
       to defeat log-injection via embedded CRLF / NUL / BEL.
    3. Cap the string at ``_MAX_LOG_ROLE_LENGTH`` characters, replacing
       the tail with an ellipsis marker so operators can still see that
       truncation happened.
    """

    text = role if isinstance(role, str) else str(role)
    cleaned = _CONTROL_CHARS_RE.sub("", text)
    if len(cleaned) > _MAX_LOG_ROLE_LENGTH:
        cleaned = cleaned[:_MAX_LOG_ROLE_LENGTH] + "...[truncated]"
    return cleaned


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

        Least-privilege fallback: when *no* role in the input set
        matches a recognized internal role, the function falls back to
        ``viewer`` (read-only) — never ``user`` — and emits a distinct
        warning so the silent privilege grant is visible to operators.
        Previously the fallback was ``user``, which grants write access
        to accounts whose IdP sends only unrecognized / empty claim
        payloads (SEV-741 follow-up).

        All raw external role strings are sanitized (control characters
        stripped, length capped) before being emitted to logs to defeat
        log-injection via crafted IdP claims.
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
        # Sanitize every raw role string before it lands in a log record
        # — IdP role/group payloads are occasionally multi-KB free-form
        # strings and may contain control characters that could be used
        # for log injection (CRLF injection, terminal escape sequences).
        sanitized_unrecognized = [sanitize_role_for_log(r) for r in unrecognized]

        # Broaden the warning: fire on ANY unrecognized external role, not
        # only when the entire set is unrecognized. This surfaces partial
        # misconfigurations (e.g. one stale group name alongside valid ones).
        if sanitized_unrecognized:
            logger.warning(
                "auth.map_roles.unrecognized_roles",
                provider=self.name,
                unrecognized=sanitized_unrecognized,
                recognized=recognized,
                mapped=best if best is not None else DEFAULT_FALLBACK_ROLE,
            )

        if best is not None:
            return best

        # Distinct event for the fallback path so operators can alert
        # on it independently from the "partial unrecognized" case.
        # Empty input and all-unrecognized input both land here.
        logger.warning(
            "auth.map_roles.fallback_to_least_privilege",
            provider=self.name,
            fallback_role=DEFAULT_FALLBACK_ROLE,
            unrecognized=sanitized_unrecognized,
            empty_input=not external_roles,
        )
        return DEFAULT_FALLBACK_ROLE
